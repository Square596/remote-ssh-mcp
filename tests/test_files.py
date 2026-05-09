from __future__ import annotations

import stat
import subprocess
from types import SimpleNamespace

import pytest

from remote_ssh_mcp import files
from remote_ssh_mcp.files import (
    MAX_READ_BYTES,
    FileOpError,
    edit_remote_file,
    read_remote_file,
    write_remote_file,
)


@pytest.mark.asyncio
async def test_read_remote_file_rejects_invalid_bounds() -> None:
    with pytest.raises(FileOpError, match="offset must be >= 0"):
        await read_remote_file("pane", "/remote/file", offset=-1)

    with pytest.raises(FileOpError, match="limit must be > 0"):
        await read_remote_file("pane", "/remote/file", limit=0)

    with pytest.raises(FileOpError, match="exceeds max"):
        await read_remote_file("pane", "/remote/file", limit=MAX_READ_BYTES + 1)


@pytest.mark.asyncio
async def test_edit_remote_file_reads_all_chunks_before_writing(monkeypatch) -> None:
    original = b"prefix old\n" + (b"a" * MAX_READ_BYTES) + b"\ntail"
    expected = original.replace(b"old", b"new", 1)
    writes: list[bytes] = []

    async def fake_read_remote_file(pane_id, path, offset=0, limit=MAX_READ_BYTES):
        assert pane_id == "pane"
        assert path == "/remote/file.txt"
        assert limit == MAX_READ_BYTES
        return original[offset : offset + limit], len(original)

    async def fake_write_remote_file(pane_id, path, content):
        assert pane_id == "pane"
        assert path == "/remote/file.txt"
        writes.append(content)
        return len(content)

    monkeypatch.setattr(files, "read_remote_file", fake_read_remote_file)
    monkeypatch.setattr(files, "write_remote_file", fake_write_remote_file)

    result = await edit_remote_file("pane", "/remote/file.txt", old="old", new="new")

    assert writes == [expected]
    assert result.occurrences_replaced == 1
    assert result.bytes_after == len(expected)


@pytest.mark.asyncio
async def test_edit_remote_file_rejects_empty_old() -> None:
    with pytest.raises(FileOpError, match="old must not be empty"):
        await edit_remote_file("pane", "/remote/file.txt", old="", new="x")


@pytest.mark.asyncio
async def test_write_remote_file_preserves_existing_mode(monkeypatch, tmp_path) -> None:
    target = tmp_path / "tool.sh"
    target.write_bytes(b"old")
    target.chmod(0o755)

    async def fake_run_in_pane(pane_id, cmd, timeout=60):
        assert pane_id == "pane"
        proc = subprocess.run(
            cmd,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
        )
        return SimpleNamespace(
            stdout=proc.stdout + proc.stderr,
            exit_code=proc.returncode,
            timed_out=False,
        )

    monkeypatch.setattr(files, "run_in_pane", fake_run_in_pane)

    written = await write_remote_file("pane", str(target), b"new")

    assert written == 3
    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
