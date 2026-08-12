"""File read / write / edit on the remote, all routed through the visible tmux pane.

Reads return base64 through pane scrollback. Writes launch a no-echo Python receiver,
stream base64 through the PTY, verify length and SHA-256, then atomically replace the
destination. Edits are read-modify-write with the same exact-string semantics as
Claude's local Edit tool (errors if `old` is non-unique unless `replace_all=True`).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import shlex
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runner import recover_pane, run_in_pane
from .tmux import capture_pane, paste_bytes, paste_text

MAX_READ_BYTES = 1_048_576  # 1 MB
WRITE_RAW_CHUNK_BYTES = 49_152  # Exactly 65,536 base64 bytes per full chunk.
WRITE_READY_TIMEOUT = 15.0
WRITE_FINISH_TIMEOUT = 120.0
WRITE_RECOVERY_TIMEOUT = 8.0

_BIN_BEGIN = "__RSM_BIN_BEGIN__"
_BIN_END = "__RSM_BIN_END__"


class FileOpError(Exception):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = {
            key: value for key, value in details.items() if value is not None
        }


_WRITE_RECEIVER_BYTES = Path(__file__).with_name("_write_receiver.py").read_bytes()

_WRITE_RECEIVER_PACKED = base64.b64encode(
    zlib.compress(_WRITE_RECEIVER_BYTES, level=9)
).decode("ascii")


async def _comment(pane_id: str, text: str) -> None:
    """Emit a no-op annotation line so the watcher sees what the agent is doing."""
    # `:` is a shell builtin no-op that ignores its args. Quoting keeps it on one line.
    await run_in_pane(pane_id, f": {shlex.quote(text)}", timeout=5)


async def read_remote_file(
    pane_id: str, path: str, offset: int = 0, limit: int = MAX_READ_BYTES
) -> tuple[bytes, int]:
    """Read up to `limit` bytes starting at `offset`. Returns (data, total_size)."""
    if offset < 0:
        raise FileOpError("offset must be >= 0.")
    if limit <= 0:
        raise FileOpError("limit must be > 0.")
    if limit > MAX_READ_BYTES:
        raise FileOpError(
            f"limit={limit} exceeds max {MAX_READ_BYTES}; read larger files in "
            f"pieces using offset/limit."
        )

    # Keep this helper compact and single-line so its shell-visible bootstrap
    # remains easy to inspect. Use expression form (open(...).read()) instead
    # of block constructs such as `with`.
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
        raise FileOpError(
            f"remote read failed (rc={result.exit_code}): {result.stdout}"
        )

    out = result.stdout
    bi = out.find(_BIN_BEGIN)
    ei = out.find(_BIN_END)
    if bi == -1 or ei == -1 or ei < bi:
        raise FileOpError(f"remote read: missing sentinels in output:\n{out[:1000]}")
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


async def read_entire_remote_file(pane_id: str, path: str) -> bytes:
    """Read a complete remote file by chunking through read_remote_file."""
    chunks: list[bytes] = []
    offset = 0

    while True:
        data, total_size = await read_remote_file(
            pane_id, path, offset=offset, limit=MAX_READ_BYTES
        )
        chunks.append(data)
        offset += len(data)

        if total_size >= 0:
            if offset >= total_size:
                break
            if not data:
                raise FileOpError(
                    f"remote read made no progress at offset {offset} of {total_size}"
                )
            continue

        if len(data) < MAX_READ_BYTES:
            break
        if not data:
            raise FileOpError(f"remote read made no progress at offset {offset}")

    return b"".join(chunks)


@dataclass
class _ReceiverEvent:
    kind: str
    payload: dict[str, Any]
    screen: str
    exit_code: int | None = None


@dataclass
class _DestinationProbe:
    available: bool
    matches: bool
    actual_bytes: int | None = None
    actual_sha256: str | None = None


def _receiver_command(
    path: str,
    *,
    encoded_bytes: int,
    content_bytes: int,
    sha256: str,
    token: str,
) -> str:
    bootstrap = (
        "import base64,zlib;"
        f"exec(zlib.decompress(base64.b64decode({_WRITE_RECEIVER_PACKED!r})))"
    )
    path_b64 = base64.b64encode(path.encode("utf-8")).decode("ascii")
    args = [path_b64, str(encoded_bytes), str(content_bytes), sha256, token]
    python_command = "python3 -c " + shlex.quote(bootstrap)
    python_command += " " + " ".join(shlex.quote(arg) for arg in args)
    return (
        f"RSM_W={shlex.quote(token)}; {python_command}; "
        f"__rsm_rc=$?; printf '\\n__RSM_WRITE_EXIT_%s_%s__\\n' "
        f'"$RSM_W" "$__rsm_rc"'
    )


def _receiver_event(
    screen: str,
    token: str,
    kinds: tuple[str, ...],
) -> _ReceiverEvent | None:
    for kind in kinds:
        if kind == "EXIT":
            matches = list(
                re.finditer(
                    rf"__RSM_WRITE_EXIT_{re.escape(token)}_(\d+)__",
                    screen,
                )
            )
            if matches:
                return _ReceiverEvent(
                    kind=kind,
                    payload={},
                    screen=screen,
                    exit_code=int(matches[-1].group(1)),
                )
            continue

        matches = list(
            re.finditer(
                rf"__RSM_WRITE_{kind}_{re.escape(token)}__\s+"
                rf"([A-Za-z0-9_-]+={{0,2}})",
                screen,
            )
        )
        if not matches:
            continue
        packed = matches[-1].group(1)
        try:
            raw = base64.urlsafe_b64decode(packed)
            payload = json.loads(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"remote write returned an invalid {kind.lower()} marker: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(
                f"remote write returned a non-object {kind.lower()} marker"
            )
        return _ReceiverEvent(kind=kind, payload=payload, screen=screen)
    return None


async def _wait_for_receiver(
    pane_id: str,
    token: str,
    kinds: tuple[str, ...],
    timeout: float,
) -> _ReceiverEvent:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_screen = ""
    while loop.time() < deadline:
        last_screen = await capture_pane(pane_id, lines=500)
        event = _receiver_event(last_screen, token, kinds)
        if event is not None:
            return event
        await asyncio.sleep(0.05)
    expected = ", ".join(kind.lower() for kind in kinds)
    raise TimeoutError(
        f"remote write timed out waiting for {expected}; "
        f"last pane content:\n{last_screen[-1000:]}"
    )


async def _settle_receiver(pane_id: str, token: str) -> bool:
    try:
        await _wait_for_receiver(
            pane_id,
            token,
            ("EXIT",),
            WRITE_RECOVERY_TIMEOUT,
        )
    except (RuntimeError, TimeoutError, ValueError):
        pass
    return await recover_pane(pane_id, timeout=WRITE_RECOVERY_TIMEOUT)


async def _probe_destination(
    pane_id: str,
    path: str,
    *,
    expected_bytes: int,
    expected_sha256: str,
) -> _DestinationProbe:
    path_b64 = base64.b64encode(path.encode("utf-8")).decode("ascii")
    marker = secrets.token_hex(8)
    py = (
        "import base64,hashlib,os,sys;"
        "p=base64.b64decode(sys.argv[1]).decode('utf-8');"
        "exists=os.path.isfile(p);h=hashlib.sha256();"
        "f=open(p,'rb') if exists else None;"
        "n=sum((h.update(c) or len(c)) for c in "
        "iter(lambda:f.read(1048576),b'')) if exists else -1;"
        "f is None or f.close();"
        "print('__RSM_WRITE_PROBE_'+sys.argv[2]+'__ '+str(n)+' '+h.hexdigest())"
    )
    cmd = f"python3 -c {shlex.quote(py)} {shlex.quote(path_b64)} {shlex.quote(marker)}"
    try:
        result = await run_in_pane(pane_id, cmd, timeout=WRITE_FINISH_TIMEOUT)
    except (RuntimeError, ValueError):
        return _DestinationProbe(available=False, matches=False)
    if result.timed_out or result.exit_code != 0:
        return _DestinationProbe(available=False, matches=False)

    match = re.search(
        rf"__RSM_WRITE_PROBE_{re.escape(marker)}__ (-?\d+) ([0-9a-f]{{64}})",
        result.stdout,
    )
    if match is None:
        return _DestinationProbe(available=False, matches=False)
    actual_bytes = int(match.group(1))
    actual_sha256 = match.group(2) if actual_bytes >= 0 else None
    return _DestinationProbe(
        available=True,
        matches=(actual_bytes == expected_bytes and actual_sha256 == expected_sha256),
        actual_bytes=actual_bytes if actual_bytes >= 0 else None,
        actual_sha256=actual_sha256,
    )


def _write_failure(
    message: str,
    *,
    stage: str,
    pane_recovered: bool,
    destination_state: str,
    expected_bytes: int,
    actual_bytes: int | None = None,
    expected_sha256: str | None = None,
    actual_sha256: str | None = None,
) -> FileOpError:
    return FileOpError(
        message,
        stage=stage,
        verified=False,
        destination_state=destination_state,
        pane_recovered=pane_recovered,
        expected_bytes=expected_bytes,
        actual_bytes=actual_bytes,
        expected_sha256=expected_sha256,
        actual_sha256=actual_sha256,
    )


async def write_remote_file(pane_id: str, path: str, content: bytes) -> int:
    """Stream, verify, and atomically write content through the visible pane."""
    content_bytes = len(content)
    encoded_bytes = 4 * ((content_bytes + 2) // 3)
    expected_sha256 = hashlib.sha256(content).hexdigest()
    token = secrets.token_hex(16)
    delimiter = f".__RSM_WRITE_PAYLOAD_END_{token}__.".encode("ascii")
    command = _receiver_command(
        path,
        encoded_bytes=encoded_bytes,
        content_bytes=content_bytes,
        sha256=expected_sha256,
        token=token,
    )
    stage = "prepare"

    try:
        await _comment(
            pane_id, f"remote-ssh-mcp: writing {content_bytes} bytes to {path}"
        )
        await paste_text(pane_id, command)
        stage = "startup"
        event = await _wait_for_receiver(
            pane_id,
            token,
            ("ERROR", "READY", "EXIT"),
            WRITE_READY_TIMEOUT,
        )
        if event.kind == "ERROR":
            pane_recovered = await _settle_receiver(pane_id, token)
            details = event.payload
            raise _write_failure(
                f"remote write failed during {details.get('stage', 'startup')}: "
                f"{details.get('message', 'unknown receiver error')}",
                stage=str(details.get("stage", "startup")),
                pane_recovered=pane_recovered,
                destination_state=str(details.get("destination_state", "unchanged")),
                expected_bytes=content_bytes,
                actual_bytes=details.get("actual_bytes"),
                expected_sha256=details.get("expected_sha256"),
                actual_sha256=details.get("actual_sha256"),
            )
        if event.kind == "EXIT":
            raise RuntimeError(
                f"write receiver exited with code {event.exit_code} before readiness"
            )
        stage = "transfer"
        for offset in range(0, content_bytes, WRITE_RAW_CHUNK_BYTES):
            encoded = base64.b64encode(content[offset : offset + WRITE_RAW_CHUNK_BYTES])
            await paste_bytes(pane_id, encoded)
        await paste_bytes(pane_id, delimiter)

        stage = "finish"
        event = await _wait_for_receiver(
            pane_id,
            token,
            ("ERROR", "DONE", "EXIT"),
            WRITE_FINISH_TIMEOUT,
        )
        if event.kind == "ERROR":
            pane_recovered = await _settle_receiver(pane_id, token)
            details = event.payload
            raise _write_failure(
                f"remote write failed during {details.get('stage', 'finish')}: "
                f"{details.get('message', 'unknown receiver error')}",
                stage=str(details.get("stage", "finish")),
                pane_recovered=pane_recovered,
                destination_state=str(details.get("destination_state", "unknown")),
                expected_bytes=content_bytes,
                actual_bytes=details.get("actual_bytes"),
                expected_sha256=details.get("expected_sha256"),
                actual_sha256=details.get("actual_sha256"),
            )
        if event.kind == "EXIT":
            raise RuntimeError(
                f"write receiver exited with code {event.exit_code} without a result"
            )

        written = event.payload.get("bytes_written")
        actual_sha256 = event.payload.get("sha256")
        if written != content_bytes or actual_sha256 != expected_sha256:
            raise RuntimeError("write receiver returned inconsistent verification data")

        exit_event = await _wait_for_receiver(
            pane_id,
            token,
            ("EXIT",),
            WRITE_RECOVERY_TIMEOUT,
        )
        if exit_event.exit_code != 0:
            raise RuntimeError(
                f"write receiver exited with code {exit_event.exit_code} after completion"
            )
        return content_bytes
    except asyncio.CancelledError:
        await asyncio.shield(recover_pane(pane_id, timeout=WRITE_RECOVERY_TIMEOUT))
        raise
    except FileOpError:
        raise
    except Exception as exc:
        pane_recovered = await recover_pane(pane_id, timeout=WRITE_RECOVERY_TIMEOUT)
        probe = _DestinationProbe(available=False, matches=False)
        if pane_recovered:
            probe = await _probe_destination(
                pane_id,
                path,
                expected_bytes=content_bytes,
                expected_sha256=expected_sha256,
            )
        if probe.matches:
            return content_bytes
        destination_state = (
            "unchanged" if stage in {"prepare", "startup"} else "unknown"
        )
        raise _write_failure(
            f"remote write failed during {stage}: {exc}",
            stage=stage,
            pane_recovered=pane_recovered,
            destination_state=destination_state,
            expected_bytes=content_bytes,
            actual_bytes=probe.actual_bytes,
            expected_sha256=(expected_sha256 if probe.actual_sha256 else None),
            actual_sha256=probe.actual_sha256,
        ) from exc


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
    if old == "":
        raise FileOpError("old must not be empty.")
    if old == new:
        raise FileOpError("old and new are identical — nothing to do.")

    data = await read_entire_remote_file(pane_id, path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise FileOpError(
            f"remote_edit only handles UTF-8 text files; {path} is not valid UTF-8 ({e}). "
            "Use scp/rsync or a binary-safe remote_run command for binary files."
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
    return EditResult(
        path=path, occurrences_replaced=replaced, bytes_after=len(new_bytes)
    )
