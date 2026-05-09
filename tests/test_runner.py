from __future__ import annotations

import re
import subprocess

import pytest

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


def test_wrap_command_rejects_empty_command() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        _wrap_command("abc123", " \t")
