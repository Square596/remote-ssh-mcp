"""Bounded asynchronous access to the local tmux server."""

from __future__ import annotations

import asyncio
import re
import secrets
import subprocess

TMUX_IO_TIMEOUT = 15.0

_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)


class TmuxError(RuntimeError):
    """A local tmux operation failed or exceeded its deadline."""


def _strip_terminal_sequences(text: str) -> str:
    return _ANSI_CSI_RE.sub("", _ANSI_OSC_RE.sub("", text))


async def tmux(
    *args: str,
    stdin: bytes | None = None,
    timeout: float = TMUX_IO_TIMEOUT,
) -> tuple[int, bytes, bytes]:
    """Run one tmux subprocess with a mandatory finite timeout."""
    if timeout <= 0:
        raise ValueError("tmux timeout must be > 0")

    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except (TimeoutError, asyncio.TimeoutError):
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        command = " ".join(args[:2])
        raise TmuxError(f"tmux {command} timed out after {timeout:g}s") from None
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out, err


async def capture_pane(target: str, lines: int = 20_000) -> str:
    """Capture recent pane history, join wrapped lines, and remove terminal escapes."""
    if lines <= 0:
        raise ValueError("capture lines must be > 0")
    rc, out, err = await tmux(
        "capture-pane",
        "-t",
        target,
        "-p",
        "-J",
        "-S",
        f"-{lines}",
    )
    if rc != 0:
        raise TmuxError(f"tmux capture-pane failed: {err.decode(errors='replace')}")
    return _strip_terminal_sequences(out.decode("utf-8", errors="replace"))


async def _load_and_paste(
    target: str,
    payload: bytes,
    *,
    bracketed: bool,
    timeout: float = TMUX_IO_TIMEOUT,
) -> None:
    """Load bytes into a private tmux buffer and paste them into one pane."""
    buffer_name = f"rsm-{secrets.token_hex(4)}"

    async def delete_buffer() -> None:
        try:
            await tmux("delete-buffer", "-b", buffer_name, timeout=timeout)
        except (TmuxError, ValueError):
            pass

    rc, _, err = await tmux(
        "load-buffer", "-b", buffer_name, "-", stdin=payload, timeout=timeout
    )
    if rc != 0:
        raise TmuxError(f"tmux load-buffer failed: {err.decode(errors='replace')}")

    args = ["paste-buffer", "-r"]
    if bracketed:
        args.append("-p")
    args.extend(["-b", buffer_name, "-d", "-t", target])
    try:
        rc, _, err = await tmux(*args, timeout=timeout)
    except asyncio.CancelledError:
        await asyncio.shield(delete_buffer())
        raise
    except Exception:
        await delete_buffer()
        raise
    if rc != 0:
        await delete_buffer()
        raise TmuxError(f"tmux paste-buffer failed: {err.decode(errors='replace')}")


async def paste_text(
    target: str,
    text: str,
    *,
    with_enter: bool = True,
    timeout: float = TMUX_IO_TIMEOUT,
) -> None:
    """Paste a shell command using bracketed-paste-aware tmux input."""
    await _load_and_paste(target, text.encode("utf-8"), bracketed=True, timeout=timeout)
    if with_enter:
        await send_keys(target, "Enter", timeout=timeout)


async def paste_bytes(
    target: str,
    payload: bytes,
    *,
    timeout: float = TMUX_IO_TIMEOUT,
) -> None:
    """Paste raw bytes for a program already reading directly from the pane."""
    await _load_and_paste(target, payload, bracketed=False, timeout=timeout)


async def send_keys(
    target: str,
    *keys: str,
    timeout: float = TMUX_IO_TIMEOUT,
) -> None:
    rc, _, err = await tmux("send-keys", "-t", target, *keys, timeout=timeout)
    if rc != 0:
        raise TmuxError(f"tmux send-keys failed: {err.decode(errors='replace')}")


async def kill_window(target: str, timeout: float = TMUX_IO_TIMEOUT) -> bool:
    """Kill a tmux window; return false only when the target was already absent."""
    rc, _, err = await tmux("kill-window", "-t", target, timeout=timeout)
    if rc == 0:
        return True
    message = err.decode(errors="replace").strip()
    if any(
        detail in message
        for detail in ("can't find window", "can't find session", "no server running")
    ):
        return False
    raise TmuxError(f"tmux kill-window failed: {message}")
