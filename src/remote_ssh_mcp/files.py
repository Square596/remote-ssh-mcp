"""File read / write / edit on the remote, all routed through the visible tmux pane.

We round-trip binary content as base64. Reads go in one shot (`base64 < file`) and
the result is parsed out of the pane scrollback. Writes are chunked: the base64
payload is appended to a temp file in 60 KB chunks (well under any ARG_MAX), then a
small Python script on the remote decodes the temp file and atomically renames it
into place. Edits are read-modify-write with the same exact-string semantics as
Claude's local Edit tool (errors if `old` is non-unique unless `replace_all=True`).
"""

from __future__ import annotations

import base64
import secrets
import shlex
from dataclasses import dataclass

from .runner import run_in_pane

MAX_READ_BYTES = 1_048_576  # 1 MB
WRITE_CHUNK_BYTES = 60_000  # base64 chars per chunk

_BIN_BEGIN = "__RSM_BIN_BEGIN__"
_BIN_END = "__RSM_BIN_END__"


class FileOpError(Exception):
    pass


async def _comment(pane_id: str, text: str) -> None:
    """Emit a no-op annotation line so the watcher sees what the agent is doing."""
    # `:` is a shell builtin no-op that ignores its args. Quoting keeps it on one line.
    await run_in_pane(pane_id, f": {shlex.quote(text)}", timeout=5)


async def read_remote_file(
    pane_id: str, path: str, offset: int = 0, limit: int = MAX_READ_BYTES
) -> tuple[bytes, int]:
    """Read up to `limit` bytes starting at `offset`. Returns (data, total_size)."""
    if limit > MAX_READ_BYTES:
        raise FileOpError(
            f"limit={limit} exceeds max {MAX_READ_BYTES}; chunked reads are not "
            f"yet supported. Read the file in pieces using offset/limit."
        )

    # Single-line python — must avoid block constructs (with/try/def/for) since
    # the runner collapses any newlines to `; ` to keep the wrapped command on
    # one line. We use expression form (open(...).read()) instead of `with`.
    py_one_line = (
        "import base64,os,sys; "
        f"p={path!r}; o={offset}; n={limit}; "
        "s=os.path.getsize(p); "
        "f=open(p,'rb'); f.seek(o); d=f.read(n); f.close(); "
        f"sys.stdout.write({_BIN_BEGIN!r}+chr(10)); "
        "sys.stdout.write(base64.b64encode(d).decode()+chr(10)); "
        f"sys.stdout.write({_BIN_END!r}+chr(10)); "
        "sys.stdout.write('__RSM_SIZE__'+str(s)+chr(10))"
    )
    cmd = f"python3 -c {shlex.quote(py_one_line)}"

    await _comment(pane_id, f"remote-ssh-mcp: reading {path}")
    result = await run_in_pane(pane_id, cmd, timeout=120)
    if result.exit_code != 0:
        raise FileOpError(f"remote read failed (rc={result.exit_code}): {result.stdout}")

    out = result.stdout
    bi = out.find(_BIN_BEGIN)
    ei = out.find(_BIN_END)
    if bi == -1 or ei == -1 or ei < bi:
        raise FileOpError(
            f"remote read: missing sentinels in output:\n{out[:1000]}"
        )
    b64 = out[bi + len(_BIN_BEGIN) : ei].strip()
    # The b64 payload may contain extra whitespace from terminal wrapping; normalize.
    b64 = "".join(b64.split())
    try:
        data = base64.b64decode(b64, validate=True)
    except Exception as e:
        raise FileOpError(f"remote read: base64 decode failed: {e}")

    size = -1
    size_marker = "__RSM_SIZE__"
    si = out.find(size_marker, ei)
    if si != -1:
        try:
            tail = out[si + len(size_marker) :].splitlines()[0]
            size = int(tail.strip())
        except Exception:
            pass

    return data, size


async def write_remote_file(pane_id: str, path: str, content: bytes) -> int:
    """Write `content` to `path` atomically. Returns bytes written."""
    b64 = base64.b64encode(content).decode("ascii")
    tmp_id = secrets.token_hex(6)
    tmp_b64 = f"/tmp/.rsm-{tmp_id}.b64"

    await _comment(pane_id, f"remote-ssh-mcp: writing {len(content)} bytes to {path}")

    init = await run_in_pane(pane_id, f": > {shlex.quote(tmp_b64)}", timeout=10)
    if init.exit_code != 0:
        raise FileOpError(f"write init failed: {init.stdout}")

    for i in range(0, len(b64), WRITE_CHUNK_BYTES):
        chunk = b64[i : i + WRITE_CHUNK_BYTES]
        cmd = f"printf %s {shlex.quote(chunk)} >> {shlex.quote(tmp_b64)}"
        r = await run_in_pane(pane_id, cmd, timeout=30)
        if r.exit_code != 0:
            raise FileOpError(
                f"write chunk failed at offset {i} (rc={r.exit_code}): {r.stdout}"
            )

    # Single-line python (no block constructs — see read_remote_file for why).
    # Atomic via tempfile + os.replace; cleanup on error skipped because the
    # tmpfiles live under `/tmp/.rsm-*` and a stale one is harmless.
    py_one_line = (
        "import base64,os,tempfile; "
        f"src={tmp_b64!r}; dst={path!r}; "
        "f=open(src,'rb'); data=base64.b64decode(f.read()); f.close(); "
        "d=os.path.dirname(os.path.abspath(dst)) or '.'; "
        "os.makedirs(d,exist_ok=True); "
        "fd,tmp=tempfile.mkstemp(dir=d,prefix='.rsm-tmp-'); "
        "os.write(fd,data); os.close(fd); os.replace(tmp,dst); "
        "os.unlink(src); "
        "print('wrote',len(data),'bytes to',dst)"
    )
    r = await run_in_pane(pane_id, f"python3 -c {shlex.quote(py_one_line)}", timeout=60)
    if r.exit_code != 0:
        raise FileOpError(f"write decode failed (rc={r.exit_code}): {r.stdout}")

    return len(content)


@dataclass
class EditResult:
    path: str
    occurrences_replaced: int
    bytes_after: int


async def edit_remote_file(
    pane_id: str, path: str, old: str, new: str, replace_all: bool = False
) -> EditResult:
    """Exact-string replacement, mirroring Claude Code's local Edit semantics.

    - If `old` does not appear, raise.
    - If `old` appears more than once and `replace_all=False`, raise.
    - Otherwise replace and write back atomically.
    """
    if old == new:
        raise FileOpError("old and new are identical — nothing to do.")

    data, _ = await read_remote_file(pane_id, path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise FileOpError(
            f"remote_edit only handles UTF-8 text files; {path} is not valid UTF-8 ({e}). "
            f"Use remote_write with the full new content for binary files."
        )

    count = text.count(old)
    if count == 0:
        raise FileOpError(
            f"`old` string not found in {path}. The file may have changed; re-read it first."
        )
    if count > 1 and not replace_all:
        raise FileOpError(
            f"`old` string is not unique in {path} ({count} occurrences). "
            f"Provide more surrounding context, or pass replace_all=True."
        )

    if replace_all:
        new_text = text.replace(old, new)
        replaced = count
    else:
        new_text = text.replace(old, new, 1)
        replaced = 1

    new_bytes = new_text.encode("utf-8")
    await write_remote_file(pane_id, path, new_bytes)
    return EditResult(path=path, occurrences_replaced=replaced, bytes_after=len(new_bytes))
