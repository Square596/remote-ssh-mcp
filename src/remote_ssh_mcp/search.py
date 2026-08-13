"""Build bounded remote search commands from structured requests."""

from __future__ import annotations

import base64
import json
import shlex
import zlib
from pathlib import Path
from typing import Any


_SEARCH_RUNNER_BYTES = Path(__file__).with_name("_search_runner.py").read_bytes()
_SEARCH_RUNNER_PACKED = base64.b64encode(
    zlib.compress(_SEARCH_RUNNER_BYTES, level=9)
).decode("ascii")


def _command(request: dict[str, Any]) -> str:
    bootstrap = (
        "import base64,zlib;"
        f"exec(zlib.decompress(base64.b64decode({_SEARCH_RUNNER_PACKED!r})))"
    )
    payload = base64.urlsafe_b64encode(
        json.dumps(request, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return f"python3 -c {shlex.quote(bootstrap)} {shlex.quote(payload)}"


def grep_command(
    pattern: str,
    path: str,
    *,
    glob: str | None,
    case_insensitive: bool,
    limit: int,
) -> str:
    return _command(
        {
            "operation": "grep",
            "pattern": pattern,
            "path": path,
            "glob": glob,
            "case_insensitive": case_insensitive,
            "limit": limit,
        }
    )


def glob_command(pattern: str, path: str, *, limit: int) -> str:
    return _command(
        {
            "operation": "glob",
            "pattern": pattern,
            "path": path,
            "limit": limit,
        }
    )
