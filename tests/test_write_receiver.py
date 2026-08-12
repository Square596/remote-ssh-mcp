from __future__ import annotations

import os
import stat

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
