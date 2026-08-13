from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from importlib.metadata import version as distribution_version
from types import SimpleNamespace

import pytest

from remote_ssh_mcp import server
from remote_ssh_mcp.files import EditResult, FileOpError


def tool_fn(tool):
    return getattr(tool, "fn", tool)


class OperationSessions:
    @asynccontextmanager
    async def operation(self, conn, name, **kwargs):
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
async def test_tool_descriptions_are_general_and_semantic() -> None:
    tools = {tool.name: tool.description for tool in await server.mcp.list_tools()}

    assert tools["remote_connect"] == (
        "Open a persistent SSH connection for a host and optional project path."
    )
    assert tools["remote_disconnect"] == "Close an active remote connection."
    assert tools["remote_grep"] == (
        "Search remote files for a text pattern and return matching lines."
    )


def test_cli_version_exits_without_starting_server(capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        server.mcp,
        "run",
        lambda: pytest.fail("the MCP server must not start for --version"),
    )

    with pytest.raises(SystemExit) as exc_info:
        server.main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == (
        f"remote-ssh-mcp {distribution_version('remote-ssh-mcp')}"
    )


@pytest.mark.asyncio
async def test_modern_sdks_receive_output_schemas_and_tool_annotations() -> None:
    tools = {
        tool.name: tool.model_dump(by_alias=True, exclude_none=True)
        for tool in await server.mcp.list_tools()
    }
    run_tool = tools["remote_run"]

    # MCP SDK 1.2 predates both decorator options. The compatibility matrix
    # exercises that fallback; newer 1.x and 2.x SDKs exercise these assertions.
    if "outputSchema" not in run_tool:
        assert "annotations" not in run_tool
        return

    assert run_tool["outputSchema"]["type"] == "object"
    assert run_tool["outputSchema"]["required"] == ["ok"]
    assert "stdout" in run_tool["outputSchema"]["properties"]
    assert run_tool["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    assert tools["remote_read"]["annotations"]["readOnlyHint"] is True
    assert tools["remote_disconnect"]["annotations"]["destructiveHint"] is True


@pytest.mark.asyncio
async def test_modern_structured_output_keeps_success_diagnostics_sparse(
    monkeypatch,
) -> None:
    tool_metadata = {
        tool.name: tool.model_dump(by_alias=True, exclude_none=True)
        for tool in await server.mcp.list_tools()
    }
    if "outputSchema" not in tool_metadata["remote_write"]:
        return

    class FakeSessions(OperationSessions):
        def get(self, connection_id):
            return SimpleNamespace(pane_id="%1", lock=asyncio.Lock())

    async def fake_write(pane_id, path, content):
        return len(content)

    monkeypatch.setattr(server, "sessions", FakeSessions())
    monkeypatch.setattr(server, "write_remote_file", fake_write)

    result = await server.mcp.call_tool(
        "remote_write",
        {"connection_id": "conn", "path": "file.txt", "content": "hello"},
    )

    structured_content = getattr(result, "structured_content", None)
    if structured_content is None:
        structured_content = result.structuredContent
    assert structured_content == {
        "ok": True,
        "path": "file.txt",
        "bytes_written": 5,
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
async def test_remote_run_preserves_clean_partial_output_on_timeout(
    monkeypatch,
) -> None:
    class FakeSessions:
        def get(self, connection_id):
            assert connection_id == "conn"
            return SimpleNamespace(pane_id="%1", lock=asyncio.Lock())

        async def run_command(self, conn, cmd, timeout):
            assert conn.pane_id == "%1"
            assert cmd == "printf timeout-marker; sleep 30"
            assert timeout == 1.0
            return SimpleNamespace(
                stdout="timeout-marker",
                exit_code=-1,
                duration_ms=1000,
                timed_out=True,
                pane_recovered=True,
            )

    monkeypatch.setattr(server, "sessions", FakeSessions())

    result = await tool_fn(server.remote_run)(
        connection_id="conn", cmd="printf timeout-marker; sleep 30", timeout=1
    )

    assert result == {
        "ok": True,
        "stdout": "timeout-marker",
        "exit_code": -1,
        "duration_ms": 1000,
        "timed_out": True,
        "truncated": False,
        "pane_recovered": True,
    }


@pytest.mark.asyncio
async def test_remote_grep_reports_exact_truncation(monkeypatch) -> None:
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
                stdout="a\nb\nc\nd\n",
                timed_out=False,
                exit_code=0,
                pane_recovered=None,
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
    assert result["matches"] == ["a", "b", "c"]
    assert result["truncated"] is True
    assert "python3 -c" in captured["cmd"]
    assert "head -n" not in captured["cmd"]


@pytest.mark.asyncio
async def test_remote_grep_exact_limit_is_not_truncated(monkeypatch) -> None:
    class FakeSessions:
        def get(self, connection_id):
            return SimpleNamespace(pane_id="%1", lock=asyncio.Lock())

        async def run_command(self, conn, cmd, timeout, operation_name):
            return SimpleNamespace(
                stdout="a\nb\nc\n",
                timed_out=False,
                exit_code=0,
                pane_recovered=None,
            )

    monkeypatch.setattr(server, "sessions", FakeSessions())

    result = await tool_fn(server.remote_grep)(
        connection_id="conn", pattern="needle", max_results=3
    )

    assert result == {
        "ok": True,
        "matches": ["a", "b", "c"],
        "count": 3,
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_remote_glob_reports_exact_truncation(monkeypatch) -> None:
    class FakeSessions:
        def get(self, connection_id):
            return SimpleNamespace(pane_id="%1", lock=asyncio.Lock())

        async def run_command(self, conn, cmd, timeout, operation_name):
            return SimpleNamespace(
                stdout="a.py\nb.py\nc.py\n",
                timed_out=False,
                exit_code=0,
                pane_recovered=None,
            )

    monkeypatch.setattr(server, "sessions", FakeSessions())

    result = await tool_fn(server.remote_glob)(
        connection_id="conn", pattern="*.py", max_results=2
    )

    assert result == {
        "ok": True,
        "files": ["a.py", "b.py"],
        "count": 2,
        "truncated": True,
    }


@pytest.mark.asyncio
async def test_remote_search_preserves_command_failures(monkeypatch) -> None:
    class FakeSessions:
        def get(self, connection_id):
            return SimpleNamespace(pane_id="%1", lock=asyncio.Lock())

        async def run_command(self, conn, cmd, timeout, operation_name):
            return SimpleNamespace(
                stdout="permission denied\n",
                timed_out=False,
                exit_code=2,
                pane_recovered=True,
            )

    monkeypatch.setattr(server, "sessions", FakeSessions())

    grep_result = await tool_fn(server.remote_grep)(
        connection_id="conn", pattern="needle"
    )
    glob_result = await tool_fn(server.remote_glob)(
        connection_id="conn", pattern="*.py"
    )

    for operation, result in (("grep", grep_result), ("glob", glob_result)):
        assert result == {
            "ok": False,
            "error": f"{operation} failed",
            "exit_code": 2,
            "partial_stdout": "permission denied\n",
            "pane_recovered": True,
        }


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
@pytest.mark.parametrize("tool_name", ["remote_grep", "remote_glob"])
async def test_search_validates_max_before_connection_lookup(
    monkeypatch, tool_name
) -> None:
    class FakeSessions:
        def get(self, connection_id):
            raise AssertionError("connection lookup must not run")

    monkeypatch.setattr(server, "sessions", FakeSessions())

    result = await tool_fn(getattr(server, tool_name))(
        connection_id="conn", pattern="needle", max_results=10_001
    )

    assert result == {
        "ok": False,
        "error": "max_results=10001 exceeds max 10000.",
    }


@pytest.mark.asyncio
async def test_invalid_read_does_not_start_recovering_operation(monkeypatch) -> None:
    class FakeSessions:
        def get(self, connection_id):
            raise AssertionError("connection lookup must not run")

        def operation(self, *args, **kwargs):
            raise AssertionError("operation must not start")

    monkeypatch.setattr(server, "sessions", FakeSessions())

    result = await tool_fn(server.remote_read)(
        connection_id="conn", path="file.txt", offset=-1
    )

    assert result == {"ok": False, "error": "offset must be >= 0."}


@pytest.mark.asyncio
async def test_invalid_edit_does_not_start_recovering_operation(monkeypatch) -> None:
    class FakeSessions:
        def get(self, connection_id):
            raise AssertionError("connection lookup must not run")

        def operation(self, *args, **kwargs):
            raise AssertionError("operation must not start")

    monkeypatch.setattr(server, "sessions", FakeSessions())

    result = await tool_fn(server.remote_edit)(
        connection_id="conn", path="file.txt", old="", new="replacement"
    )

    assert result == {"ok": False, "error": "old must not be empty."}


@pytest.mark.asyncio
async def test_remote_read_only_warns_for_invalid_utf8(monkeypatch) -> None:
    class FakeSessions(OperationSessions):
        def get(self, connection_id):
            return SimpleNamespace(pane_id="%1", lock=asyncio.Lock())

    reads = [b"plain text", b"invalid:\xff"]

    async def fake_read(*args, **kwargs):
        data = reads.pop(0)
        return data, len(data)

    monkeypatch.setattr(server, "sessions", FakeSessions())
    monkeypatch.setattr(server, "read_remote_file", fake_read)

    valid = await tool_fn(server.remote_read)(connection_id="conn", path="file.txt")
    invalid = await tool_fn(server.remote_read)(connection_id="conn", path="file.txt")

    assert valid == {
        "ok": True,
        "content": "plain text",
        "byte_size": 10,
        "total_size": 10,
        "offset": 0,
    }
    assert invalid["content"] == "invalid:\ufffd"
    assert "encoding_warning" in invalid


@pytest.mark.asyncio
async def test_failed_file_recovery_marks_connection_unresponsive(monkeypatch) -> None:
    from remote_ssh_mcp.session import Connection, SessionManager

    manager = SessionManager()
    connection = Connection(
        connection_id="conn",
        host="host",
        session_name="session",
        window_id="@1",
        pane_id="%1",
        project_path=None,
        label="test",
    )
    manager._connections[connection.connection_id] = connection

    async def fake_write(*args, **kwargs):
        raise FileOpError(
            "pane could not recover",
            pane_recovered=False,
            stage="transfer",
        )

    monkeypatch.setattr(server, "sessions", manager)
    monkeypatch.setattr(server, "write_remote_file", fake_write)

    result = await tool_fn(server.remote_write)(
        connection_id="conn", path="file.txt", content="content"
    )

    assert result["ok"] is False
    assert result["pane_recovered"] is False
    assert connection.state == "unresponsive"


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
