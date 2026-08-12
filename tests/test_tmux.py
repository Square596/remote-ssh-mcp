from __future__ import annotations

import asyncio
import subprocess

import pytest

from remote_ssh_mcp import tmux as tmux_module
from remote_ssh_mcp.tmux import TmuxError


@pytest.mark.asyncio
async def test_paste_text_uses_private_bracketed_buffer_and_enter(monkeypatch) -> None:
    calls: list[tuple[tuple[str, ...], bytes | None, float | None]] = []

    async def fake_tmux(*args, stdin=None, timeout=None):
        calls.append((args, stdin, timeout))
        return 0, b"", b""

    monkeypatch.setattr(tmux_module, "tmux", fake_tmux)

    await tmux_module.paste_text("%1", "echo hello")

    assert calls[0][0][0:2] == ("load-buffer", "-b")
    assert calls[0][0][-1] == "-"
    assert calls[0][1] == b"echo hello"
    assert calls[1][0][0:2] == ("paste-buffer", "-r")
    assert "-p" in calls[1][0]
    assert calls[1][0][-2:] == ("-t", "%1")
    assert calls[2][0] == ("send-keys", "-t", "%1", "Enter")


@pytest.mark.asyncio
async def test_paste_bytes_is_raw_and_does_not_press_enter(monkeypatch) -> None:
    calls: list[tuple[tuple[str, ...], bytes | None, float | None]] = []

    async def fake_tmux(*args, stdin=None, timeout=None):
        calls.append((args, stdin, timeout))
        return 0, b"", b""

    monkeypatch.setattr(tmux_module, "tmux", fake_tmux)

    await tmux_module.paste_bytes("%2", b"payload")

    assert len(calls) == 2
    assert calls[0][1] == b"payload"
    assert calls[1][0][0:2] == ("paste-buffer", "-r")
    assert "-p" not in calls[1][0]
    assert calls[1][0][-2:] == ("-t", "%2")


class HangingProc:
    returncode = None

    def __init__(self) -> None:
        self.killed = False
        self.waited = False

    async def communicate(self, payload=None):
        await asyncio.Future()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waited = True
        return self.returncode


@pytest.mark.asyncio
async def test_tmux_timeout_kills_and_reaps_subprocess(monkeypatch) -> None:
    proc = HangingProc()

    async def fake_create_subprocess_exec(*args, **kwargs):
        assert kwargs["stdin"] is subprocess.DEVNULL
        return proc

    monkeypatch.setattr(
        tmux_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    with pytest.raises(TmuxError, match="timed out"):
        await tmux_module.tmux("display-message", timeout=0.001)

    assert proc.killed is True
    assert proc.waited is True


@pytest.mark.asyncio
async def test_tmux_cancellation_kills_and_reaps_subprocess(monkeypatch) -> None:
    proc = HangingProc()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(
        tmux_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    task = asyncio.create_task(tmux_module.tmux("display-message"))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert proc.killed is True
    assert proc.waited is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "can't find window: @1",
        "can't find session: remote-ssh-mcp/host",
        "no server running on /tmp/tmux/default",
    ],
)
async def test_kill_window_is_idempotent_when_target_is_absent(
    monkeypatch, message
) -> None:
    async def fake_tmux(*args, **kwargs):
        return 1, b"", message.encode()

    monkeypatch.setattr(tmux_module, "tmux", fake_tmux)

    assert await tmux_module.kill_window("@1") is False
