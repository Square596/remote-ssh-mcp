from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from remote_ssh_mcp import runner
from remote_ssh_mcp.runner import (
    _extract_output,
    _extract_partial_output,
    _wrap_command,
)


def test_extract_output_accepts_begin_marker_glued_to_echoed_command() -> None:
    marker = "abc123"
    begin_literal = f"__RSM_BEGIN_{marker}__"
    begin_re = re.compile(re.escape(begin_literal))
    screen = (
        '(base) prompt$ RSM_M="abc123"; echo "__RSM_BEGIN_${RSM_M}__"'
        "__RSM_BEGIN_abc123__\n"
        "payload\n"
        "__RSM_END_abc123_0__\n"
    )
    end_pos = screen.index("__RSM_END_abc123_0__")

    assert _extract_output(screen, begin_re, end_pos) == "payload"


def test_extract_partial_output_ignores_history_and_protocol_framing() -> None:
    marker = "leftright"
    begin_re = re.compile(rf"__RSM_BEGIN_{marker}__")
    end_re = re.compile(rf"^__RSM_END_{marker}_(\d+)__$", re.MULTILINE)
    screen = (
        "old prompt and output\n"
        "printf '__RSM_BEGIN_' left right '__'\n"
        "__RSM_BEGIN_leftright__\n"
        "timeout-marker\n"
        "\n\n"
    )

    assert _extract_partial_output(screen, begin_re, end_re) == "timeout-marker"


def test_extract_partial_output_stops_at_late_completion_marker() -> None:
    marker = "leftright"
    begin_re = re.compile(rf"__RSM_BEGIN_{marker}__")
    end_re = re.compile(rf"^__RSM_END_{marker}_(\d+)__$", re.MULTILINE)
    screen = (
        "__RSM_BEGIN_leftright__\n"
        "completed-near-deadline\n"
        "__RSM_END_leftright_0__\n"
        "__RSM_CWD_leftright__ /tmp\n"
    )

    assert (
        _extract_partial_output(screen, begin_re, end_re) == "completed-near-deadline"
    )


def test_extract_partial_output_requires_an_attributable_begin_marker() -> None:
    begin_re = re.compile(r"__RSM_BEGIN_leftright__")
    end_re = re.compile(r"^__RSM_END_leftright_(\d+)__$", re.MULTILINE)

    assert _extract_partial_output("old prompt and output\n", begin_re, end_re) is None


def test_wrap_command_accepts_trailing_semicolon_and_preserves_state(tmp_path) -> None:
    marker = "abc123"
    wrapped = _wrap_command(marker, f"cd {tmp_path};")

    proc = subprocess.run(
        ["bash", "-lc", f'{wrapped}; printf "AFTER:%s\\n" "$PWD"'],
        check=False,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert f"__RSM_END_{marker}_0__" in proc.stdout
    assert f"AFTER:{tmp_path}" in proc.stdout


def test_wrap_command_accepts_background_command() -> None:
    marker = "abc123"
    wrapped = _wrap_command(marker, "true &")

    proc = subprocess.run(
        ["bash", "-lc", wrapped],
        check=False,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert f"__RSM_END_{marker}_0__" in proc.stdout


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_wrap_command_preserves_multiline_shell_syntax_and_state(
    shell: str, tmp_path
) -> None:
    shell_path = shutil.which(shell)
    if shell_path is None:
        pytest.skip(f"{shell} is required")

    marker = "abc123"
    command = (
        "\n"
        "export RSM_MULTILINE='kept'\n"
        "for item in alpha beta; do\n"
        "  printf 'item:%s\\n' \"$item\"\n"
        "done\n"
        "\n"
        "cat <<'RSM_HEREDOC'\n"
        "literal:$RSM_MULTILINE\n"
        "single-quote:' backslash:\\\n"
        "RSM_HEREDOC\n"
        f"cd {shlex.quote(str(tmp_path))}\n"
    )
    wrapped = _wrap_command(marker, command)

    proc = subprocess.run(
        [
            shell_path,
            "-fc",
            f'{wrapped}; printf "STATE:%s:%s\\n" "$PWD" "$RSM_MULTILINE"',
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert "item:alpha\nitem:beta" in proc.stdout
    assert "literal:$RSM_MULTILINE" in proc.stdout
    assert "single-quote:' backslash:\\" in proc.stdout
    assert f"__RSM_END_{marker}_0__" in proc.stdout
    assert f"STATE:{tmp_path}:kept" in proc.stdout


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_wrap_command_reports_multiline_final_exit_status(shell: str) -> None:
    shell_path = shutil.which(shell)
    if shell_path is None:
        pytest.skip(f"{shell} is required")

    marker = "abc123"
    wrapped = _wrap_command(marker, "printf 'before-failure\\n'\n\nfalse\n")
    proc = subprocess.run(
        [shell_path, "-fc", wrapped],
        check=False,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert "before-failure" in proc.stdout
    assert f"__RSM_END_{marker}_1__" in proc.stdout


def test_wrap_command_normalizes_text_line_endings() -> None:
    wrapped = _wrap_command("abc123", "printf first\r\nprintf second\r")

    assert "\r" not in wrapped
    assert "printf first\nprintf second\n" in wrapped


def test_wrap_command_rejects_empty_command() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        _wrap_command("abc123", " \t")


def test_wrap_command_rejects_nul_character() -> None:
    with pytest.raises(ValueError, match="must not contain NUL"):
        _wrap_command("abc123", "printf 'before'\x00printf 'after'")


def test_wrap_command_framing_cannot_be_clobbered_by_user_variables() -> None:
    wrapped = _wrap_command(
        "left",
        "RSM_A=clobber; RSM_B=clobber; __rsm_rc=clobber; printf done",
        marker_right="right",
    )

    proc = subprocess.run(
        ["bash", "-fc", wrapped],
        check=False,
        capture_output=True,
        text=True,
    )

    assert "__RSM_BEGIN_leftright__" in proc.stdout
    assert "__RSM_END_leftright_0__" in proc.stdout
    assert "__RSM_CWD_leftright__" in proc.stdout


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_wrap_command_suspends_and_restores_inherited_xtrace(shell: str) -> None:
    shell_path = shutil.which(shell)
    if shell_path is None:
        pytest.skip(f"{shell} is required")

    marker = "abc123"
    wrapped = _wrap_command(marker, "printf clean")
    proc = subprocess.run(
        [
            shell_path,
            "-fc",
            (
                f"set -x; {wrapped}; "
                "case $- in *x*) set +x; printf TRACE_RESTORED ;; esac"
            ),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    begin_re = re.compile(rf"__RSM_BEGIN_{marker}__")
    end_pos = proc.stdout.index(f"__RSM_END_{marker}_0__")

    assert _extract_output(proc.stdout, begin_re, end_pos) == "clean"
    assert "TRACE_RESTORED" in proc.stdout


@pytest.mark.asyncio
async def test_run_rejects_non_positive_deadlines_before_paste(monkeypatch) -> None:
    async def unexpected_paste(*args, **kwargs):
        raise AssertionError("paste should not run")

    monkeypatch.setattr(runner, "paste_text", unexpected_paste)

    with pytest.raises(ValueError, match="timeout must be > 0"):
        await runner.run_in_pane("%1", "true", timeout=0)
    with pytest.raises(ValueError, match="poll interval must be > 0"):
        await runner.run_in_pane("%1", "true", poll_interval=0)


@pytest.mark.asyncio
async def test_run_polls_recent_lines_then_captures_full_output(monkeypatch) -> None:
    markers = iter(("left", "right"))
    captures: list[int] = []
    poll = "__RSM_END_leftright_0__\n__RSM_CWD_leftright__ /tmp\n"
    final = (
        "__RSM_BEGIN_leftright__\n"
        "output without a user newline\n"
        "__RSM_END_leftright_0__\n"
        "__RSM_CWD_leftright__ /tmp\n"
    )

    async def fake_paste_text(target, text):
        assert target == "%1"

    async def fake_capture_pane(target, lines):
        assert target == "%1"
        captures.append(lines)
        return poll if len(captures) == 1 else final

    monkeypatch.setattr(runner.secrets, "token_hex", lambda _: next(markers))
    monkeypatch.setattr(runner, "paste_text", fake_paste_text)
    monkeypatch.setattr(runner, "capture_pane", fake_capture_pane)

    result = await runner.run_in_pane("%1", "printf output", timeout=1)

    assert captures == [runner.RUN_POLL_CAPTURE_LINES, runner.RUN_FINAL_CAPTURE_LINES]
    assert result.stdout == "output without a user newline"
    assert result.exit_code == 0
    assert result.cwd == "/tmp"


@pytest.mark.asyncio
async def test_timeout_retries_history_without_leaking_unattributed_output(
    monkeypatch,
) -> None:
    markers = iter(("left", "right"))
    ticks = iter((0.0, 0.0, 0.02, 0.02))
    captures: list[int] = []
    recent = "old prompt and echoed wrapper\n"
    full = (
        "older pane history\n"
        "printf '__RSM_BEGIN_' left right '__'\n"
        "__RSM_BEGIN_leftright__\n"
        "timeout-marker\n"
        "\n\n"
    )

    async def fake_paste_text(target, text):
        assert target == "%1"

    async def fake_capture_pane(target, lines):
        assert target == "%1"
        captures.append(lines)
        return recent if len(captures) == 1 else full

    async def fake_sleep(delay):
        assert delay == 0.02

    monkeypatch.setattr(runner.secrets, "token_hex", lambda _: next(markers))
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(runner, "asyncio", SimpleNamespace(sleep=fake_sleep))
    monkeypatch.setattr(runner, "paste_text", fake_paste_text)
    monkeypatch.setattr(runner, "capture_pane", fake_capture_pane)

    result = await runner.run_in_pane(
        "%1", "printf timeout-marker; sleep 30", timeout=0.01, poll_interval=0.02
    )

    assert captures == [runner.RUN_POLL_CAPTURE_LINES, runner.RUN_TIMEOUT_CAPTURE_LINES]
    assert result.stdout == "timeout-marker"
    assert result.timed_out is True


@pytest.mark.asyncio
async def test_timeout_refreshes_output_after_the_last_poll(monkeypatch) -> None:
    markers = iter(("left", "right"))
    ticks = iter((0.0, 0.0, 0.02, 0.02))
    captures: list[int] = []
    recent = "__RSM_BEGIN_leftright__\nearly\n"
    at_deadline = "__RSM_BEGIN_leftright__\nearly\nlate\n"

    async def fake_paste_text(target, text):
        assert target == "%1"

    async def fake_capture_pane(target, lines):
        assert target == "%1"
        captures.append(lines)
        return recent if len(captures) == 1 else at_deadline

    async def fake_sleep(delay):
        assert delay == 0.02

    monkeypatch.setattr(runner.secrets, "token_hex", lambda _: next(markers))
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(runner, "asyncio", SimpleNamespace(sleep=fake_sleep))
    monkeypatch.setattr(runner, "paste_text", fake_paste_text)
    monkeypatch.setattr(runner, "capture_pane", fake_capture_pane)

    result = await runner.run_in_pane(
        "%1",
        "printf early; sleep 1; printf late; sleep 30",
        timeout=0.01,
        poll_interval=0.02,
    )

    assert captures == [runner.RUN_POLL_CAPTURE_LINES, runner.RUN_TIMEOUT_CAPTURE_LINES]
    assert result.stdout == "early\nlate"
    assert result.timed_out is True


@pytest.mark.parametrize("deadline_capture", [None, "marker evicted by new output"])
@pytest.mark.asyncio
async def test_timeout_falls_back_to_last_attributable_poll(
    monkeypatch, deadline_capture: str | None
) -> None:
    markers = iter(("left", "right"))
    ticks = iter((0.0, 0.0, 0.02, 0.02))
    captures = 0
    recent = "__RSM_BEGIN_leftright__\nattributable\n"

    async def fake_paste_text(target, text):
        assert target == "%1"

    async def fake_capture_pane(target, lines):
        nonlocal captures
        assert target == "%1"
        captures += 1
        if captures == 1:
            return recent
        if deadline_capture is None:
            raise RuntimeError("tmux capture failed")
        return deadline_capture

    async def fake_sleep(delay):
        assert delay == 0.02

    monkeypatch.setattr(runner.secrets, "token_hex", lambda _: next(markers))
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(runner, "asyncio", SimpleNamespace(sleep=fake_sleep))
    monkeypatch.setattr(runner, "paste_text", fake_paste_text)
    monkeypatch.setattr(runner, "capture_pane", fake_capture_pane)

    result = await runner.run_in_pane(
        "%1", "printf attributable; sleep 30", timeout=0.01, poll_interval=0.02
    )

    assert captures == 2
    assert result.stdout == "attributable"
    assert result.timed_out is True


@pytest.mark.asyncio
async def test_timeout_returns_empty_output_when_command_never_started(
    monkeypatch,
) -> None:
    markers = iter(("left", "right"))
    ticks = iter((0.0, 0.0, 0.02, 0.02))

    async def fake_paste_text(target, text):
        assert target == "%1"

    async def fake_capture_pane(target, lines):
        assert target == "%1"
        return "old prompt and echoed wrapper\n"

    async def fake_sleep(delay):
        assert delay == 0.02

    monkeypatch.setattr(runner.secrets, "token_hex", lambda _: next(markers))
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(runner, "asyncio", SimpleNamespace(sleep=fake_sleep))
    monkeypatch.setattr(runner, "paste_text", fake_paste_text)
    monkeypatch.setattr(runner, "capture_pane", fake_capture_pane)

    result = await runner.run_in_pane(
        "%1", "sleep 30", timeout=0.01, poll_interval=0.02
    )

    assert result.stdout == ""
    assert result.timed_out is True
