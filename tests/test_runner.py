from __future__ import annotations

import re

from remote_ssh_mcp.runner import _extract_output


def test_extract_output_accepts_begin_marker_glued_to_echoed_command() -> None:
    marker = "abc123"
    begin_literal = f"__RSM_BEGIN_{marker}__"
    begin_re = re.compile(re.escape(begin_literal))
    screen = (
        '(base) host:/tmp$ RSM_M="abc123"; echo "__RSM_BEGIN_${RSM_M}__"'
        "__RSM_BEGIN_abc123__\n"
        "payload\n"
        "__RSM_END_abc123_0__\n"
    )
    end_pos = screen.index("__RSM_END_abc123_0__")

    assert _extract_output(screen, begin_re, end_pos) == "payload"
