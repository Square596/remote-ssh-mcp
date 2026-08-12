from __future__ import annotations

import pytest

from remote_ssh_mcp import files
from remote_ssh_mcp.files import (
    MAX_READ_BYTES,
    FileOpError,
    _receiver_command,
    edit_remote_file,
    read_remote_file,
)


@pytest.mark.asyncio
async def test_read_remote_file_rejects_invalid_bounds() -> None:
    with pytest.raises(FileOpError, match="offset must be >= 0"):
        await read_remote_file("pane", "/remote/file", offset=-1)

    with pytest.raises(FileOpError, match="limit must be > 0"):
        await read_remote_file("pane", "/remote/file", limit=0)

    with pytest.raises(FileOpError, match="exceeds max"):
        await read_remote_file("pane", "/remote/file", limit=MAX_READ_BYTES + 1)


def test_write_receiver_bootstrap_is_one_shell_line() -> None:
    command = _receiver_command(
        "/remote/path with spaces/file.txt",
        encoded_bytes=8,
        content_bytes=5,
        sha256="a" * 64,
        token="b" * 32,
    )

    assert "\n" not in command
    assert "/remote/path with spaces/file.txt" not in command


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
async def test_edit_remote_file_replaces_multiline_exact_match(monkeypatch) -> None:
    original = b"before\nalpha\nbeta\nafter\n"
    expected = b"before\none\ntwo\nthree\nafter\n"
    writes: list[bytes] = []

    async def fake_read_entire_remote_file(pane_id, path):
        assert (pane_id, path) == ("pane", "/remote/file.txt")
        return original

    async def fake_write_remote_file(pane_id, path, content):
        assert (pane_id, path) == ("pane", "/remote/file.txt")
        writes.append(content)
        return len(content)

    monkeypatch.setattr(files, "read_entire_remote_file", fake_read_entire_remote_file)
    monkeypatch.setattr(files, "write_remote_file", fake_write_remote_file)

    result = await edit_remote_file(
        "pane",
        "/remote/file.txt",
        old="alpha\nbeta",
        new="one\ntwo\nthree",
    )

    assert writes == [expected]
    assert result.occurrences_replaced == 1
    assert result.bytes_after == len(expected)


@pytest.mark.asyncio
async def test_edit_remote_file_rejects_empty_old() -> None:
    with pytest.raises(FileOpError, match="old must not be empty"):
        await edit_remote_file("pane", "/remote/file.txt", old="", new="x")
