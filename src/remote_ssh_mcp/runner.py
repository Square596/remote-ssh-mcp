"""Run commands through persistent tmux panes with bounded marker polling."""

from __future__ import annotations

import asyncio
import re
import secrets
import shlex
import time
from dataclasses import dataclass

from .tmux import capture_pane, paste_text, send_keys

RUN_POLL_CAPTURE_LINES = 200
RUN_TIMEOUT_CAPTURE_LINES = 20_000
PANE_HISTORY_LIMIT = 1_100_000
RUN_FINAL_CAPTURE_LINES = PANE_HISTORY_LIMIT
PANE_RECOVERY_TIMEOUT = 8.0


@dataclass
class RunResult:
    stdout: str
    exit_code: int
    duration_ms: int
    timed_out: bool = False
    cwd: str | None = None
    pane_recovered: bool | None = None


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
    if timeout <= 0:
        raise ValueError("remote command timeout must be > 0")
    if poll_interval <= 0:
        raise ValueError("remote command poll interval must be > 0")

    marker_left = secrets.token_hex(8)
    marker_right = secrets.token_hex(8)
    marker = marker_left + marker_right
    begin_literal = f"__RSM_BEGIN_{marker}__"
    end_re = re.compile(rf"^__RSM_END_{re.escape(marker)}_(\d+)__$", re.MULTILINE)
    cwd_re = re.compile(rf"^__RSM_CWD_{re.escape(marker)}__ (.*)$", re.MULTILINE)
    # The remote PTY can render the command echo and first output on the same
    # visual line. The echoed wrapper keeps the random halves separated while
    # executed output joins them, so only the latter contains the full marker.
    begin_re = re.compile(re.escape(begin_literal))

    wrapped = _wrap_command(marker_left, user_cmd, marker_right=marker_right)

    start = time.monotonic()
    await paste_text(target, wrapped)

    deadline = start + timeout
    last_screen = ""
    while True:
        if time.monotonic() > deadline:
            # Refresh once at the deadline so output produced since the last
            # poll is not lost. If tmux itself fails, the last successful poll
            # remains the safest attributable fallback.
            try:
                timeout_screen = await capture_pane(
                    target, lines=RUN_TIMEOUT_CAPTURE_LINES
                )
            except RuntimeError:
                timeout_screen = last_screen
            partial_stdout = _extract_partial_output(timeout_screen, begin_re, end_re)
            if partial_stdout is None:
                partial_stdout = _extract_partial_output(last_screen, begin_re, end_re)
            if partial_stdout is None:
                partial_stdout = ""
            return RunResult(
                stdout=partial_stdout,
                exit_code=-1,
                duration_ms=int((time.monotonic() - start) * 1000),
                timed_out=True,
            )

        screen = await capture_pane(target, lines=RUN_POLL_CAPTURE_LINES)
        last_screen = screen
        m = end_re.search(screen)
        cwd_match = cwd_re.search(screen, m.end() if m else 0)
        if m and cwd_match:
            screen = await capture_pane(target, lines=RUN_FINAL_CAPTURE_LINES)
            m = end_re.search(screen)
            cwd_match = cwd_re.search(screen, m.end() if m else 0)
            if m is None or cwd_match is None:
                raise RuntimeError("tmux pane lost command completion markers")
            begin_match = begin_re.search(screen[: m.start()])
            if begin_match is None:
                raise RuntimeError(
                    "command output exceeded available tmux history before completion"
                )
            exit_code = int(m.group(1))
            stdout = _extract_output(screen, begin_re, m.start())
            return RunResult(
                stdout=stdout,
                exit_code=exit_code,
                duration_ms=int((time.monotonic() - start) * 1000),
                cwd=cwd_match.group(1),
            )

        await asyncio.sleep(poll_interval)


def _wrap_command(
    marker: str,
    user_cmd: str,
    *,
    marker_right: str = "",
) -> str:
    """Build one wrapper submission while preserving the command's shell syntax."""
    if "\x00" in user_cmd:
        raise ValueError("remote command must not contain NUL characters")

    # MCP command text may arrive with platform-native line endings. Shells use
    # LF as the grammar delimiter, so normalize text line endings before quoting
    # the complete program into one assignment in the wrapper.
    normalized_cmd = user_cmd.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized_cmd.strip():
        raise ValueError("remote command must not be empty")

    # Shell execution tracing is pane-wide state. Suspend inherited tracing
    # around the framed command so PS4/xtrace diagnostics cannot become tool
    # output, then restore the setting after all protocol markers are emitted.
    # The randomized variable names avoid collisions with user shell state.
    xtrace_var = f"__rsm_xtrace_{marker}"
    exit_var = f"__rsm_exit_{marker}"

    return (
        f"case $- in *x*) {xtrace_var}=1 ;; *) {xtrace_var}=0 ;; esac; "
        "set +x; "
        f"printf '%s%s%s%s\\n' '__RSM_BEGIN_' {shlex.quote(marker)} "
        f"{shlex.quote(marker_right)} '__'; "
        f"eval {shlex.quote(normalized_cmd)}; "
        f"{exit_var}=$?; "
        f"printf '\\n%s%s%s_%s%s\\n' '__RSM_END_' {shlex.quote(marker)} "
        f'{shlex.quote(marker_right)} "${{{exit_var}}}" "__"; '
        f"printf '%s%s%s%s %s\\n' '__RSM_CWD_' {shlex.quote(marker)} "
        f"{shlex.quote(marker_right)} '__' \"$PWD\"; "
        f'if [ "${{{xtrace_var}}}" = 1 ]; then '
        f"unset {xtrace_var} {exit_var}; set -x; "
        f"else unset {xtrace_var} {exit_var}; fi"
    )


async def recover_pane(target: str, timeout: float = PANE_RECOVERY_TIMEOUT) -> bool:
    """Interrupt a foreground command, restore tty settings, and probe the shell."""
    deadline = time.monotonic() + timeout
    try:
        for _ in range(2):
            await send_keys(
                target, "C-c", timeout=max(0.1, deadline - time.monotonic())
            )
            await asyncio.sleep(0.15)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        probe = await run_in_pane(
            target,
            "stty sane >/dev/null 2>&1 || true",
            timeout=remaining,
        )
    except (RuntimeError, ValueError):
        return False
    return not probe.timed_out and probe.exit_code == 0


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


def _extract_partial_output(
    screen: str,
    begin_re: re.Pattern[str],
    end_re: re.Pattern[str],
) -> str | None:
    """Return attributable output from an incomplete command capture."""
    begin_matches = list(begin_re.finditer(screen))
    if not begin_matches:
        return None

    last_begin = begin_matches[-1]
    output_start = last_begin.end()
    if output_start < len(screen) and screen[output_start] == "\n":
        output_start += 1

    end_match = end_re.search(screen, output_start)
    output_end = end_match.start() if end_match is not None else len(screen)
    return screen[output_start:output_end].rstrip("\n")
