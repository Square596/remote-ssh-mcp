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
async def test_paste_text_uses_bracketed_paste_and_enter(monkeypatch) -> None:
    calls: list[tuple[tuple[str, ...], bytes | None, float | None]] = []

    async def fake_tmux(*args, stdin=None, timeout=None):
        calls.append((args, stdin, timeout))
        return 0, b"", b""

    monkeypatch.setattr(runner, "_tmux", fake_tmux)

    await runner.paste_text("%1", "echo hello")

    assert calls[0][0][0:2] == ("load-buffer", "-b")
    assert calls[0][0][3:] == ("-",)
    assert calls[0][1] == b"echo hello"
    assert calls[1][0][0:3] == ("paste-buffer", "-r", "-p")
    assert calls[1][0][-2:] == ("-t", "%1")
    assert calls[2][0] == ("send-keys", "-t", "%1", "Enter")


@pytest.mark.asyncio
async def test_paste_bytes_is_raw_and_does_not_press_enter(monkeypatch) -> None:
    calls: list[tuple[tuple[str, ...], bytes | None, float | None]] = []

    async def fake_tmux(*args, stdin=None, timeout=None):
        calls.append((args, stdin, timeout))
        return 0, b"", b""

    monkeypatch.setattr(runner, "_tmux", fake_tmux)

    await runner.paste_bytes("%2", b"payload")

    assert len(calls) == 2
    assert calls[0][1] == b"payload"
    assert calls[1][0][0:2] == ("paste-buffer", "-r")
    assert "-p" not in calls[1][0]
    assert calls[1][0][-2:] == ("-t", "%2")
