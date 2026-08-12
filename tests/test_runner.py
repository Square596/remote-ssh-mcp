from __future__ import annotations

import re
import shlex
import shutil
import subprocess

import pytest

from remote_ssh_mcp import runner
from remote_ssh_mcp.runner import _extract_output, _wrap_command


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
