from __future__ import annotations

import asyncio

import pytest

from remote_ssh_mcp.runner import RunResult
from remote_ssh_mcp.session import (
    Connection,
    PreflightResult,
    SessionError,
    SessionManager,
)


def make_connection(**overrides) -> Connection:
    fields = {
        "connection_id": "conn",
        "host": "example-host",
        "session_name": "remote-ssh-mcp/example-host",
        "window_id": "@1",
        "pane_id": "%1",
        "project_path": None,
        "label": "test",
    }
    fields.update(overrides)
    return Connection(**fields)


@pytest.mark.asyncio
async def test_wait_for_shell_uses_executed_marker_not_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[str] = []
    captures = [
        "dynamic prompt with no conventional terminator\n"
        "printf '%s%s\\n' '__RSM_READY_' 'abc123__'\n",
        "arbitrary prompt text λ\n__RSM_READY_abc123__\n",
    ]

    async def fake_paste_text(pane_id: str, text: str) -> None:
        assert pane_id == "%1.0"
        probes.append(text)

    async def fake_capture_pane(pane_id: str, lines: int) -> str:
        assert pane_id == "%1.0"
        assert lines == 100
        return captures.pop(0)

    async def fake_sleep(delay: float) -> None:
        assert 0 < delay <= 0.5

    monkeypatch.setattr("remote_ssh_mcp.session.secrets.token_hex", lambda _: "abc123")
    monkeypatch.setattr("remote_ssh_mcp.session.paste_text", fake_paste_text)
    monkeypatch.setattr("remote_ssh_mcp.session.capture_pane", fake_capture_pane)
    monkeypatch.setattr("remote_ssh_mcp.session.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("remote_ssh_mcp.session.SHELL_READY_RETRY_INTERVAL", 0)

    await SessionManager()._wait_for_shell("%1.0", "example-host", timeout=1)

    assert probes == [
        "printf '%s%s\\n' '__RSM_READY_' 'abc123__'",
        "printf '%s%s\\n' '__RSM_READY_' 'abc123__'",
    ]


@pytest.mark.asyncio
async def test_wait_for_shell_timeout_reports_recent_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_capture_pane(pane_id: str, lines: int) -> str:
        assert pane_id == "%1.0"
        assert lines == 100
        return "login initialization is still running"

    monkeypatch.setattr("remote_ssh_mcp.session.capture_pane", fake_capture_pane)

    with pytest.raises(SessionError) as exc_info:
        await SessionManager()._wait_for_shell("%1.0", "example-host", timeout=0)

    message = str(exc_info.value)
    assert "could execute the readiness probe" in message
    assert "login initialization is still running" in message
    assert "expected to end" not in message


@pytest.mark.asyncio
async def test_wait_for_shell_wraps_paste_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_paste_text(pane_id: str, text: str) -> None:
        raise RuntimeError("tmux paste failed")

    monkeypatch.setattr("remote_ssh_mcp.session.paste_text", fake_paste_text)

    with pytest.raises(SessionError, match="could not send.*tmux paste failed"):
        await SessionManager()._wait_for_shell("%1.0", "example-host", timeout=1)


@pytest.mark.asyncio
async def test_wait_for_shell_wraps_capture_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_paste_text(pane_id: str, text: str) -> None:
        return None

    async def fake_capture_pane(pane_id: str, lines: int) -> str:
        raise RuntimeError("pane disappeared")

    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr("remote_ssh_mcp.session.paste_text", fake_paste_text)
    monkeypatch.setattr("remote_ssh_mcp.session.capture_pane", fake_capture_pane)
    monkeypatch.setattr("remote_ssh_mcp.session.asyncio.sleep", fake_sleep)

    with pytest.raises(SessionError, match="could not inspect.*pane disappeared"):
        await SessionManager()._wait_for_shell("%1.0", "example-host", timeout=1)


@pytest.mark.asyncio
async def test_operation_exposes_busy_state_and_records_failure() -> None:
    sm = SessionManager()
    conn = make_connection()

    with pytest.raises(RuntimeError, match="operation failed"):
        async with sm.operation(conn, "write"):
            assert conn.state == "busy"
            assert conn.current_operation == "write"
            raise RuntimeError("operation failed")

    assert conn.state == "ready"
    assert conn.current_operation is None
    assert conn.last_error == "operation failed"


@pytest.mark.asyncio
async def test_run_timeout_recovers_pane_and_updates_status(monkeypatch) -> None:
    sm = SessionManager()
    conn = make_connection(cwd="/before")

    async def fake_run(*args, **kwargs):
        return RunResult("partial", -1, 10, timed_out=True)

    async def fake_recover(pane_id):
        assert pane_id == "%1"
        return True

    monkeypatch.setattr("remote_ssh_mcp.session.run_in_pane", fake_run)
    monkeypatch.setattr("remote_ssh_mcp.session.recover_pane", fake_recover)

    result = await sm.run_command(conn, "sleep 10", timeout=0.1)

    assert result.pane_recovered is True
    assert conn.state == "ready"
    assert conn.cwd == "/before"
    assert conn.last_error == "run timed out after 0.1s"


@pytest.mark.asyncio
async def test_successful_run_updates_connection_cwd(monkeypatch) -> None:
    sm = SessionManager()
    conn = make_connection(cwd="/before")

    async def fake_run(*args, **kwargs):
        return RunResult("", 0, 10, cwd="/after")

    monkeypatch.setattr("remote_ssh_mcp.session.run_in_pane", fake_run)

    result = await sm.run_command(conn, "cd /after", timeout=1)

    assert result.exit_code == 0
    assert conn.cwd == "/after"
    assert conn.last_error is None


@pytest.mark.asyncio
async def test_failed_recovery_marks_connection_unresponsive(monkeypatch) -> None:
    sm = SessionManager()
    conn = make_connection()
    sm._connections[conn.connection_id] = conn

    async def fake_run(*args, **kwargs):
        raise RuntimeError("pane vanished")

    async def fake_recover(*args, **kwargs):
        return False

    monkeypatch.setattr("remote_ssh_mcp.session.run_in_pane", fake_run)
    monkeypatch.setattr("remote_ssh_mcp.session.recover_pane", fake_recover)

    with pytest.raises(SessionError, match="connection is unresponsive"):
        await sm.run_command(conn, "true", timeout=1)

    assert conn.state == "unresponsive"
    with pytest.raises(SessionError, match="remote_disconnect"):
        sm.get(conn.connection_id)


@pytest.mark.asyncio
async def test_cancelled_run_recovers_before_releasing_pane(monkeypatch) -> None:
    sm = SessionManager()
    conn = make_connection()
    started = asyncio.Event()

    async def fake_run(*args, **kwargs):
        started.set()
        await asyncio.Future()

    async def fake_recover(*args, **kwargs):
        return True

    monkeypatch.setattr("remote_ssh_mcp.session.run_in_pane", fake_run)
    monkeypatch.setattr("remote_ssh_mcp.session.recover_pane", fake_recover)
    task = asyncio.create_task(sm.run_command(conn, "sleep 10", timeout=30))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert conn.state == "ready"
    assert conn.current_operation is None
    assert conn.last_error == "run was cancelled"


@pytest.mark.asyncio
async def test_connect_failure_cleans_up_created_window(monkeypatch) -> None:
    sm = SessionManager()
    cleaned: list[str] = []

    async def fake_preflight(*args, **kwargs):
        return PreflightResult()

    async def fake_session_exists(*args, **kwargs):
        return False

    async def fake_new_session(*args, **kwargs):
        return "@1", "%1"

    async def fake_configure_history(*args, **kwargs):
        return None

    async def fake_wait(*args, **kwargs):
        raise SessionError("login failed")

    async def fake_cleanup(window_id):
        cleaned.append(window_id)

    monkeypatch.setattr(sm, "_preflight", fake_preflight)
    monkeypatch.setattr(sm, "_session_exists", fake_session_exists)
    monkeypatch.setattr(sm, "_new_session", fake_new_session)
    monkeypatch.setattr(sm, "_configure_history", fake_configure_history)
    monkeypatch.setattr(sm, "_wait_for_shell", fake_wait)
    monkeypatch.setattr(sm, "_cleanup_window", fake_cleanup)

    with pytest.raises(SessionError, match="login failed"):
        await sm.connect("example-host")

    assert cleaned == ["@1"]
    assert sm.list_connections() == []


@pytest.mark.asyncio
async def test_disconnect_waits_for_active_operation(monkeypatch) -> None:
    sm = SessionManager()
    conn = make_connection()
    sm._connections[conn.connection_id] = conn
    killed: list[str] = []

    async def fake_kill(window_id):
        killed.append(window_id)
        return True

    monkeypatch.setattr("remote_ssh_mcp.session.kill_window", fake_kill)
    await conn.lock.acquire()
    task = asyncio.create_task(sm.disconnect(conn.connection_id))
    await asyncio.sleep(0)
    assert task.done() is False
    assert conn.state == "ready"
    conn.lock.release()

    assert await task == {"closed": True}
    assert killed == ["@1"]
    assert sm.list_connections() == []


@pytest.mark.asyncio
async def test_cancelled_disconnect_finishes_window_cleanup(monkeypatch) -> None:
    sm = SessionManager()
    conn = make_connection()
    sm._connections[conn.connection_id] = conn
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_kill(window_id):
        started.set()
        await release.wait()
        return True

    monkeypatch.setattr("remote_ssh_mcp.session.kill_window", fake_kill)
    task = asyncio.create_task(sm.disconnect(conn.connection_id))
    await started.wait()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert sm.list_connections() == []


@pytest.mark.parametrize(
    ("host", "message"),
    [
        ("-oProxyCommand=bad", "Invalid host name"),
        ("host\nother", "Invalid host name"),
    ],
)
@pytest.mark.asyncio
async def test_connect_rejects_unsafe_host_before_preflight(host, message) -> None:
    with pytest.raises(SessionError, match=message):
        await SessionManager().connect(host)


@pytest.mark.asyncio
async def test_connect_rejects_option_shaped_ssh_add_path_before_preflight() -> None:
    with pytest.raises(SessionError, match="must not start"):
        await SessionManager().connect("example-host", ssh_add_paths=["-D"])
