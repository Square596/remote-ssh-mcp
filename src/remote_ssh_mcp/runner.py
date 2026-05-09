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


def _strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


async def _tmux(*args: str, stdin: bytes | None = None) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdin=subprocess.PIPE if stdin is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin)
    return proc.returncode or 0, out, err


async def capture_pane(target: str, lines: int = 20000) -> str:
    """Capture pane scrollback (most recent `lines` lines), joined and ANSI-stripped."""
    rc, out, err = await _tmux(
        "capture-pane", "-t", target, "-p", "-J", "-S", f"-{lines}"
    )
    if rc != 0:
        raise RuntimeError(f"tmux capture-pane failed: {err.decode(errors='replace')}")
    return _strip_ansi(out.decode("utf-8", errors="replace"))


async def paste_text(target: str, text: str, with_enter: bool = True) -> None:
    """Paste arbitrary text into a pane. Safer than send-keys for long content."""
    buf = f"rsm-{secrets.token_hex(4)}"
    rc, _, err = await _tmux("load-buffer", "-b", buf, "-", stdin=text.encode("utf-8"))
    if rc != 0:
        raise RuntimeError(f"tmux load-buffer failed: {err.decode(errors='replace')}")
    rc, _, err = await _tmux("paste-buffer", "-b", buf, "-d", "-t", target)
    if rc != 0:
        raise RuntimeError(f"tmux paste-buffer failed: {err.decode(errors='replace')}")
    if with_enter:
        rc, _, err = await _tmux("send-keys", "-t", target, "Enter")
        if rc != 0:
            raise RuntimeError(
                f"tmux send-keys Enter failed: {err.decode(errors='replace')}"
            )


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

    user_cmd should be a single-line shell snippet (compound commands with
    `;`, `&&`, `||`, pipes are fine). For multi-line scripts, write a file
    via remote_write and execute it. Embedded literal newlines are stripped
    here because tmux paste-buffer converts them to CR, which interacts
    badly with pre-login PTY buffering.
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
    """Build the single shell line pasted into the pane for a user command."""
    # Strict single-line wrapping. Newlines in user_cmd would cause the paste
    # to be split into multiple shell lines, breaking sentinel ordering.
    safe_user_cmd = user_cmd.replace("\n", "; ").replace("\r", "")
    if not safe_user_cmd.strip():
        raise ValueError("remote command must not be empty")

    return (
        f'RSM_M="{marker}"; '
        f'echo "__RSM_BEGIN_${{RSM_M}}__"; '
        f"__rsm_cmd={shlex.quote(safe_user_cmd)}; "
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
