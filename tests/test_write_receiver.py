from __future__ import annotations

import os
import stat

import pytest

from remote_ssh_mcp import _write_receiver
from remote_ssh_mcp._write_receiver import WriteReceiver


def make_receiver(path, decoded_path) -> WriteReceiver:
    receiver = WriteReceiver(
        path=str(path),
        expected_encoded=0,
        expected_bytes=0,
        expected_sha256="",
        token="test",
    )
    receiver.decoded_path = str(decoded_path)
    return receiver


def test_commit_new_file_obeys_process_umask(tmp_path) -> None:
    decoded = tmp_path / "decoded"
    decoded.write_bytes(b"new content")
    target = tmp_path / "nested" / "target.txt"
    receiver = make_receiver(target, decoded)

    previous_umask = os.umask(0o027)
    try:
        receiver.commit_payload()
    finally:
        os.umask(previous_umask)

    assert target.read_bytes() == b"new content"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_commit_preserves_existing_file_mode(tmp_path) -> None:
    decoded = tmp_path / "decoded"
    decoded.write_bytes(b"replacement")
    target = tmp_path / "target.txt"
    target.write_bytes(b"original")
    target.chmod(0o751)

    make_receiver(target, decoded).commit_payload()

    assert target.read_bytes() == b"replacement"
    assert stat.S_IMODE(target.stat().st_mode) == 0o751


def test_commit_follows_symlink_without_replacing_it(tmp_path) -> None:
    decoded = tmp_path / "decoded"
    decoded.write_bytes(b"replacement")
    target = tmp_path / "target.txt"
    target.write_bytes(b"original")
    target.chmod(0o604)
    link = tmp_path / "link.txt"
    link.symlink_to(target.name)

    make_receiver(link, decoded).commit_payload()

    assert link.is_symlink()
    assert os.readlink(link) == target.name
    assert target.read_bytes() == b"replacement"
    assert stat.S_IMODE(target.stat().st_mode) == 0o604


def test_commit_creates_dangling_symlink_target(tmp_path) -> None:
    decoded = tmp_path / "decoded"
    decoded.write_bytes(b"created")
    target = tmp_path / "missing" / "target.txt"
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    make_receiver(link, decoded).commit_payload()

    assert link.is_symlink()
    assert target.read_bytes() == b"created"


def test_commit_rejects_symlink_retargeted_during_copy(tmp_path, monkeypatch) -> None:
    decoded = tmp_path / "decoded"
    decoded.write_bytes(b"replacement")
    original_target = tmp_path / "original.txt"
    original_target.write_bytes(b"original")
    other_target = tmp_path / "other.txt"
    other_target.write_bytes(b"other")
    link = tmp_path / "link.txt"
    link.symlink_to(original_target.name)
    copyfileobj = _write_receiver.shutil.copyfileobj

    def retarget_then_copy(source, target, length):
        link.unlink()
        link.symlink_to(other_target.name)
        copyfileobj(source, target, length)

    monkeypatch.setattr(_write_receiver.shutil, "copyfileobj", retarget_then_copy)

    with pytest.raises(RuntimeError, match="destination path changed"):
        make_receiver(link, decoded).commit_payload()

    assert link.is_symlink()
    assert link.resolve() == other_target
    assert original_target.read_bytes() == b"original"
    assert other_target.read_bytes() == b"other"


def test_commit_does_not_replace_new_symlink_for_missing_path(
    tmp_path, monkeypatch
) -> None:
    decoded = tmp_path / "decoded"
    decoded.write_bytes(b"replacement")
    target = tmp_path / "new.txt"
    other_target = tmp_path / "other.txt"
    other_target.write_bytes(b"other")
    copyfileobj = _write_receiver.shutil.copyfileobj

    def create_link_then_copy(source, destination, length):
        target.symlink_to(other_target.name)
        copyfileobj(source, destination, length)

    monkeypatch.setattr(_write_receiver.shutil, "copyfileobj", create_link_then_copy)

    with pytest.raises(RuntimeError, match="destination path changed"):
        make_receiver(target, decoded).commit_payload()

    assert target.is_symlink()
    assert target.resolve() == other_target
    assert other_target.read_bytes() == b"other"
