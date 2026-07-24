from __future__ import annotations

import pytest

from remote_ssh_mcp.session import SessionError, SessionManager


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
