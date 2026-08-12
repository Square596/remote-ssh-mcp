from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from remote_ssh_mcp import server
from remote_ssh_mcp.files import EditResult, FileOpError


def tool_fn(tool):
    return getattr(tool, "fn", tool)


class OperationSessions:
    @asynccontextmanager
    async def operation(self, conn, name):
        async with conn.lock:
            yield conn


@pytest.mark.asyncio
async def test_mcp_registers_expected_tools() -> None:
    tools = await server.mcp.list_tools()

    assert {tool.name for tool in tools} == {
        "remote_connect",
        "remote_disconnect",
        "remote_edit",
        "remote_glob",
        "remote_grep",
        "remote_read",
        "remote_run",
        "remote_status",
        "remote_write",
    }


@pytest.mark.asyncio
async def test_remote_run_rejects_empty_command() -> None:
    result = await tool_fn(server.remote_run)(connection_id="unused", cmd=" \t")

    assert result["ok"] is False
    assert "empty commands" in result["error"]


@pytest.mark.asyncio
async def test_remote_run_rejects_nul_before_connection_lookup() -> None:
    result = await tool_fn(server.remote_run)(
        connection_id="unused", cmd="printf before\x00printf after"
    )

    assert result["ok"] is False
    assert "NUL" in result["error"]


@pytest.mark.asyncio
async def test_remote_run_accepts_multiline_command(monkeypatch) -> None:
    command = 'for item in a b; do\n  echo "$item"\ndone\n'
    captured: dict[str, object] = {}

    class FakeSessions:
        def get(self, connection_id):
            assert connection_id == "conn"
            return SimpleNamespace(pane_id="%1", lock=asyncio.Lock())

        async def run_command(self, conn, cmd, timeout):
            captured.update(pane_id=conn.pane_id, cmd=cmd, timeout=timeout)
            return SimpleNamespace(
                stdout="a\nb",
                exit_code=0,
                duration_ms=12,
                timed_out=False,
                pane_recovered=None,
            )

    monkeypatch.setattr(server, "sessions", FakeSessions())

    result = await tool_fn(server.remote_run)(
        connection_id="conn", cmd=command, timeout=15
    )

    assert captured == {"pane_id": "%1", "cmd": command, "timeout": 15.0}
    assert result == {
        "ok": True,
        "stdout": "a\nb",
        "exit_code": 0,
        "duration_ms": 12,
        "timed_out": False,
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_remote_grep_caps_rg_output_with_head(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class FakeSessions:
        def get(self, connection_id):
            assert connection_id == "conn"
            return SimpleNamespace(pane_id="%1", lock=asyncio.Lock())

        async def run_command(self, conn, cmd, timeout, operation_name):
            assert conn.pane_id == "%1"
            assert timeout == 120
            assert operation_name == "grep"
            captured["cmd"] = cmd
            return SimpleNamespace(
                stdout="a\nb\nc\n",
                timed_out=False,
            )

    monkeypatch.setattr(server, "sessions", FakeSessions())

    result = await tool_fn(server.remote_grep)(
        connection_id="conn",
        pattern="needle",
        path=".",
        max_results=3,
    )

    assert result["ok"] is True
    assert result["count"] == 3
    assert result["truncated"] is True
    assert "| head -n 3" in captured["cmd"]
    assert " -m 3 " not in captured["cmd"]


def test_run_output_truncation_is_utf8_safe_and_exactly_bounded() -> None:
    text = "start:" + ("✨" * 100) + ":end"

    result, truncated = server._truncate_output(text, limit=80)

    assert truncated is True
    assert len(result.encode("utf-8")) <= 80
    assert result.startswith("start:")
    assert result.endswith(":end")
    assert "remote output truncated" in result


def test_run_output_below_limit_is_unchanged() -> None:
    result, truncated = server._truncate_output("unchanged", limit=20)

    assert result == "unchanged"
    assert truncated is False


@pytest.mark.asyncio
async def test_remote_grep_rejects_non_positive_max_results(monkeypatch) -> None:
    class FakeSessions(OperationSessions):
        def get(self, connection_id):
            return SimpleNamespace(pane_id="%1", lock=asyncio.Lock())

    monkeypatch.setattr(server, "sessions", FakeSessions())

    result = await tool_fn(server.remote_grep)(
        connection_id="conn",
        pattern="needle",
        max_results=0,
    )

    assert result["ok"] is False
    assert "max_results must be > 0" in result["error"]


@pytest.mark.asyncio
async def test_remote_write_success_response_stays_stable(monkeypatch) -> None:
    class FakeSessions(OperationSessions):
        def get(self, connection_id):
            assert connection_id == "conn"
            return SimpleNamespace(pane_id="%1", lock=asyncio.Lock())

    async def fake_write(pane_id, path, content):
        assert (pane_id, path, content) == ("%1", "file.txt", b"hello")
        return len(content)

    monkeypatch.setattr(server, "sessions", FakeSessions())
    monkeypatch.setattr(server, "write_remote_file", fake_write)

    result = await tool_fn(server.remote_write)(
        connection_id="conn", path="file.txt", content="hello"
    )

    assert result == {"ok": True, "path": "file.txt", "bytes_written": 5}


@pytest.mark.asyncio
async def test_remote_write_exposes_diagnostics_only_on_failure(monkeypatch) -> None:
    class FakeSessions(OperationSessions):
        def get(self, connection_id):
            return SimpleNamespace(pane_id="%1", lock=asyncio.Lock())

    async def fake_write(pane_id, path, content):
        raise FileOpError(
            "write verification failed",
            stage="verify",
            verified=False,
            destination_state="unchanged",
            pane_recovered=True,
            expected_bytes=5,
            actual_bytes=4,
        )

    monkeypatch.setattr(server, "sessions", FakeSessions())
    monkeypatch.setattr(server, "write_remote_file", fake_write)

    result = await tool_fn(server.remote_write)(
        connection_id="conn", path="file.txt", content="hello"
    )

    assert result == {
        "ok": False,
        "error": "write verification failed",
        "stage": "verify",
        "verified": False,
        "destination_state": "unchanged",
        "pane_recovered": True,
        "expected_bytes": 5,
        "actual_bytes": 4,
    }


@pytest.mark.asyncio
async def test_remote_edit_success_response_stays_stable(monkeypatch) -> None:
    class FakeSessions(OperationSessions):
        def get(self, connection_id):
            return SimpleNamespace(pane_id="%1", lock=asyncio.Lock())

    async def fake_edit(pane_id, path, old, new, replace_all):
        return EditResult(path=path, occurrences_replaced=1, bytes_after=8)

    monkeypatch.setattr(server, "sessions", FakeSessions())
    monkeypatch.setattr(server, "edit_remote_file", fake_edit)

    result = await tool_fn(server.remote_edit)(
        connection_id="conn",
        path="file.txt",
        old="old",
        new="new",
    )

    assert result == {
        "ok": True,
        "path": "file.txt",
        "occurrences_replaced": 1,
        "bytes_after": 8,
    }
