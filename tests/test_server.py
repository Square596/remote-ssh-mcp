from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from remote_ssh_mcp import server
from remote_ssh_mcp.files import EditResult, FileOpError


def tool_fn(tool):
    return getattr(tool, "fn", tool)


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

    async def fake_run_in_pane(pane_id, cmd, timeout=60):
        captured.update(pane_id=pane_id, cmd=cmd, timeout=timeout)
        return SimpleNamespace(
            stdout="a\nb",
            exit_code=0,
            duration_ms=12,
            timed_out=False,
        )

    monkeypatch.setattr(server, "sessions", FakeSessions())
    monkeypatch.setattr(server, "run_in_pane", fake_run_in_pane)

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
    }


@pytest.mark.asyncio
async def test_remote_grep_caps_rg_output_with_head(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class FakeSessions:
        def get(self, connection_id):
            assert connection_id == "conn"
            return SimpleNamespace(pane_id="%1", lock=asyncio.Lock())

    async def fake_run_in_pane(pane_id, cmd, timeout=60):
        assert pane_id == "%1"
        captured["cmd"] = cmd
        return SimpleNamespace(
            stdout="a\nb\nc\n",
            timed_out=False,
        )

    monkeypatch.setattr(server, "sessions", FakeSessions())
    monkeypatch.setattr(server, "run_in_pane", fake_run_in_pane)

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


@pytest.mark.asyncio
async def test_remote_grep_rejects_non_positive_max_results(monkeypatch) -> None:
    class FakeSessions:
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
    class FakeSessions:
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
    class FakeSessions:
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
    class FakeSessions:
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
