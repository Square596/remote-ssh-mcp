"""Send a command into a tmux pane, wait for it to finish, return clean stdout + exit code.

The mechanism: wrap the user's command with two unique sentinels (BEGIN/END), paste
the wrapped command into the pane via tmux's load-buffer/paste-buffer (which handles
arbitrary length cleanly), then poll capture-pane until the END sentinel — with the
substituted exit code — appears in the scrollback. Output is everything between the
BEGIN and END sentinel lines.

The sentinels embed the marker via a shell variable (e.g. `${RSM_M}`) so the line
where bash *echoes the command at the prompt* contains the literal `${RSM_M}` and
`$?`, while the line where `echo` actually runs contains the substituted values.
That difference is what lets us distinguish the prompt-echo from the real output.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import shlex
import subprocess
import time
from dataclasses import dataclass

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")
TMUX_IO_TIMEOUT = 15.0


def _strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


async def _tmux(
    *args: str,
    stdin: bytes | None = None,
    timeout: float | None = None,
) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdin=subprocess.PIPE if stdin is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        if timeout is None:
            out, err = await proc.communicate(stdin)
        else:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        command = " ".join(args[:2])
        raise RuntimeError(f"tmux {command} timed out after {timeout:g}s") from None
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out, err


async def capture_pane(target: str, lines: int = 20000) -> str:
    """Capture pane scrollback (most recent `lines` lines), joined and ANSI-stripped."""
    rc, out, err = await _tmux(
        "capture-pane", "-t", target, "-p", "-J", "-S", f"-{lines}"
    )
    if rc != 0:
        raise RuntimeError(f"tmux capture-pane failed: {err.decode(errors='replace')}")
    return _strip_ansi(out.decode("utf-8", errors="replace"))


async def _load_and_paste(
    target: str,
    payload: bytes,
    *,
    bracketed: bool,
    timeout: float = TMUX_IO_TIMEOUT,
) -> None:
    """Load bytes into a private tmux buffer and paste them into a pane."""
    buf = f"rsm-{secrets.token_hex(4)}"

    async def delete_buffer() -> None:
        try:
            await _tmux("delete-buffer", "-b", buf, timeout=timeout)
        except RuntimeError:
            pass

    rc, _, err = await _tmux(
        "load-buffer", "-b", buf, "-", stdin=payload, timeout=timeout
    )
    if rc != 0:
        raise RuntimeError(f"tmux load-buffer failed: {err.decode(errors='replace')}")

    args = ["paste-buffer", "-r"]
    if bracketed:
        args.append("-p")
    args.extend(["-b", buf, "-d", "-t", target])
    try:
        rc, _, err = await _tmux(*args, timeout=timeout)
    except asyncio.CancelledError:
        await asyncio.shield(delete_buffer())
        raise
    except Exception:
        await delete_buffer()
        raise
    if rc != 0:
        await delete_buffer()
        raise RuntimeError(f"tmux paste-buffer failed: {err.decode(errors='replace')}")


async def paste_text(
    target: str,
    text: str,
    with_enter: bool = True,
    timeout: float = TMUX_IO_TIMEOUT,
) -> None:
    """Paste a shell command using bracketed-paste-aware tmux input."""
    await _load_and_paste(target, text.encode("utf-8"), bracketed=True, timeout=timeout)
    if with_enter:
        rc, _, err = await _tmux("send-keys", "-t", target, "Enter", timeout=timeout)
        if rc != 0:
            raise RuntimeError(
                f"tmux send-keys Enter failed: {err.decode(errors='replace')}"
            )


async def paste_bytes(
    target: str,
    payload: bytes,
    timeout: float = TMUX_IO_TIMEOUT,
) -> None:
    """Paste raw bytes for a program already reading directly from the pane."""
    await _load_and_paste(target, payload, bracketed=False, timeout=timeout)


@dataclass
class RunResult:
    stdout: str
    exit_code: int
    duration_ms: int
    timed_out: bool = False


async def run_in_pane(
    target: str,
    user_cmd: str,
    timeout: float = 60.0,
    poll_interval: float = 0.1,
) -> RunResult:
    """Run user_cmd in the pane, return clean stdout + exit code.

    Single-line snippets and multi-line shell programs are both supported.
    The complete command is quoted into the single wrapper submission so
    embedded newlines cannot become separate interactive shell submissions.
    """
    marker = secrets.token_hex(8)
    begin_literal = f"__RSM_BEGIN_{marker}__"
    end_re = re.compile(rf"^__RSM_END_{re.escape(marker)}_(\d+)__\s*$", re.MULTILINE)
    # The remote PTY can occasionally render the command echo and the first
    # echo output on the same visual line. The expanded marker is still unique:
    # the echoed command contains `${RSM_M}`, while real output contains the
    # random marker value.
    begin_re = re.compile(re.escape(begin_literal))

    wrapped = _wrap_command(marker, user_cmd)

    start = time.monotonic()
    await paste_text(target, wrapped)

    deadline = start + timeout
    last_screen = ""
    while True:
        if time.monotonic() > deadline:
            return RunResult(
                stdout=last_screen[-2000:],
                exit_code=-1,
                duration_ms=int((time.monotonic() - start) * 1000),
                timed_out=True,
            )

        screen = await capture_pane(target)
        last_screen = screen
        m = end_re.search(screen)
        if m and begin_re.search(screen[: m.start()]):
            # Both sentinels are present — declare done. We *require* BEGIN
            # before bailing, because tmux's grid is sometimes a tick behind
            # the bytes streaming in: a single capture-pane call can show END
            # while BEGIN hasn't yet propagated. Without this guard,
            # _extract_output would fall back to the screen tail (banner +
            # stale prior markers) and the caller would see a bogus stdout.
            exit_code = int(m.group(1))
            stdout = _extract_output(screen, begin_re, m.start())
            return RunResult(
                stdout=stdout,
                exit_code=exit_code,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        await asyncio.sleep(poll_interval)


def _wrap_command(marker: str, user_cmd: str) -> str:
    """Build one wrapper submission while preserving the command's shell syntax."""
    if "\x00" in user_cmd:
        raise ValueError("remote command must not contain NUL characters")

    # MCP command text may arrive with platform-native line endings. Shells use
    # LF as the grammar delimiter, so normalize text line endings before quoting
    # the complete program into one assignment in the wrapper.
    normalized_cmd = user_cmd.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized_cmd.strip():
        raise ValueError("remote command must not be empty")

    return (
        f'RSM_M="{marker}"; '
        f'echo "__RSM_BEGIN_${{RSM_M}}__"; '
        f"__rsm_cmd={shlex.quote(normalized_cmd)}; "
        f'eval "$__rsm_cmd"; '
        f"__rsm_rc=$?; "
        f'RSM_M="{marker}"; '
        f'echo "__RSM_END_${{RSM_M}}_${{__rsm_rc}}__"'
    )


def _extract_output(screen: str, begin_re: re.Pattern[str], end_pos: int) -> str:
    """Return content between the LAST BEGIN sentinel before end_pos and end_pos itself.

    The caller in run_in_pane guarantees BEGIN is present before invoking us,
    so the no-match branch should be unreachable. We assert anyway — a bug in
    the calling order would otherwise corrupt stdout silently, which is what
    the v0.1.2 race-fix was for.
    """
    upto_end = screen[:end_pos]
    matches = list(begin_re.finditer(upto_end))
    assert matches, "_extract_output called without a BEGIN sentinel — caller bug"
    last_begin = matches[-1]
    output_start = last_begin.end()
    if output_start < len(screen) and screen[output_start] == "\n":
        output_start += 1
    return screen[output_start:end_pos].rstrip("\n")
