"""Self-contained receiver executed by ``python3 -c`` on the remote host.

Keep this module limited to the Python standard library: :mod:`files` reads,
compresses, and sends its source to hosts where remote-ssh-mcp is not installed.
"""

import base64
import copy
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import termios


def _new_file_mode():
    current_umask = os.umask(0)
    os.umask(current_umask)
    return 0o666 & ~current_umask


class WriteReceiver:
    def __init__(
        self,
        path,
        expected_encoded,
        expected_bytes,
        expected_sha256,
        token,
    ):
        self.path = path
        self.expected_encoded = expected_encoded
        self.expected_bytes = expected_bytes
        self.expected_sha256 = expected_sha256
        self.token = token
        self.delimiter = (".__RSM_WRITE_PAYLOAD_END_" + token + "__.").encode()
        self.encoded_path = None
        self.decoded_path = None
        self.destination_tmp = None
        self.actual_bytes = None
        self.actual_sha256 = None
        self.committed = False
        self.replace_started = False

    def emit(self, kind, payload):
        raw = json.dumps(payload, separators=(",", ":")).encode()
        packed = base64.urlsafe_b64encode(raw).decode()
        print(
            "\n__RSM_WRITE_" + kind + "_" + self.token + "__ " + packed,
            flush=True,
        )

    @staticmethod
    def remove(pathname):
        if pathname is not None:
            try:
                os.unlink(pathname)
            except OSError:
                pass

    def fail(self, stage, exc, destination_state="unchanged"):
        payload = {
            "message": str(exc),
            "stage": stage,
            "destination_state": destination_state,
            "expected_bytes": self.expected_bytes,
        }
        if self.actual_bytes is not None:
            payload["actual_bytes"] = self.actual_bytes
        if stage == "verify":
            payload["expected_sha256"] = self.expected_sha256
        if self.actual_sha256 is not None:
            payload["actual_sha256"] = self.actual_sha256
        self.emit("ERROR", payload)
        return 1

    def receive_payload(self):
        fd = sys.stdin.fileno()
        old_tty = termios.tcgetattr(fd)
        new_tty = copy.deepcopy(old_tty)
        new_tty[3] &= ~(termios.ECHO | termios.ICANON)
        new_tty[6][termios.VMIN] = 1
        new_tty[6][termios.VTIME] = 0
        encoded_fd, self.encoded_path = tempfile.mkstemp(
            prefix=".rsm-write-", suffix=".b64"
        )
        encoded_file = os.fdopen(encoded_fd, "wb")
        pending = bytearray()
        received = 0
        failure = None
        try:
            termios.tcsetattr(fd, termios.TCSANOW, new_tty)
            self.emit("READY", {})
            while True:
                part = os.read(fd, 65_536)
                if not part:
                    raise EOFError("terminal input ended during transfer")
                pending.extend(part)
                delimiter_at = pending.find(self.delimiter)
                if delimiter_at >= 0:
                    encoded_file.write(pending[:delimiter_at])
                    received += delimiter_at
                    trailing = pending[delimiter_at + len(self.delimiter) :]
                    if trailing:
                        raise ValueError(
                            "unexpected bytes followed the payload delimiter"
                        )
                    break

                flush_bytes = len(pending) - len(self.delimiter) + 1
                if flush_bytes > 0:
                    encoded_file.write(pending[:flush_bytes])
                    received += flush_bytes
                    del pending[:flush_bytes]
                if received + len(pending) > self.expected_encoded + 65_536:
                    raise ValueError("payload exceeded its expected encoded length")
        except BaseException as exc:
            failure = exc
        finally:
            encoded_file.close()
            try:
                termios.tcflush(fd, termios.TCIFLUSH)
            except termios.error:
                pass
            termios.tcsetattr(fd, termios.TCSANOW, old_tty)

        if failure is not None:
            raise failure
        if received != self.expected_encoded:
            raise ValueError(
                "encoded length mismatch: expected "
                + str(self.expected_encoded)
                + ", received "
                + str(received)
            )

    def verify_payload(self):
        decoded_fd, self.decoded_path = tempfile.mkstemp(
            prefix=".rsm-write-", suffix=".data"
        )
        digest = hashlib.sha256()
        self.actual_bytes = 0
        carry = b""
        source = open(self.encoded_path, "rb")
        target = os.fdopen(decoded_fd, "wb")
        with source, target:
            while True:
                part = source.read(65_536)
                if not part:
                    break
                block = carry + part
                usable = len(block) - (len(block) % 4)
                if usable:
                    decoded = base64.b64decode(block[:usable], validate=True)
                    target.write(decoded)
                    digest.update(decoded)
                    self.actual_bytes += len(decoded)
                carry = block[usable:]
            if carry:
                raise ValueError("encoded payload length is not divisible by four")
            target.flush()
            os.fsync(target.fileno())

        self.actual_sha256 = digest.hexdigest()
        if self.actual_bytes != self.expected_bytes:
            raise ValueError(
                "decoded length mismatch: expected "
                + str(self.expected_bytes)
                + ", received "
                + str(self.actual_bytes)
            )
        if self.actual_sha256 != self.expected_sha256:
            raise ValueError("sha256 mismatch")

    def destination_snapshot(self):
        try:
            entry = os.lstat(self.path)
        except FileNotFoundError:
            return {
                "kind": "missing",
                "destination": self.path,
            }

        identity = (entry.st_dev, entry.st_ino)
        if stat.S_ISLNK(entry.st_mode):
            return {
                "kind": "symlink",
                "identity": identity,
                "link": os.readlink(self.path),
                "destination": os.path.realpath(self.path),
            }
        return {
            "kind": "entry",
            "identity": identity,
            "destination": self.path,
        }

    def destination_matches(self, snapshot):
        try:
            entry = os.lstat(self.path)
        except FileNotFoundError:
            return snapshot["kind"] == "missing"

        if snapshot["kind"] == "missing":
            return False
        if (entry.st_dev, entry.st_ino) != snapshot["identity"]:
            return False
        if snapshot["kind"] == "symlink":
            return (
                stat.S_ISLNK(entry.st_mode)
                and os.readlink(self.path) == snapshot["link"]
                and os.path.realpath(self.path) == snapshot["destination"]
            )
        return not stat.S_ISLNK(entry.st_mode)

    def commit_payload(self):
        snapshot = self.destination_snapshot()
        destination = snapshot["destination"]
        directory = os.path.dirname(os.path.abspath(destination)) or "."
        os.makedirs(directory, exist_ok=True)
        try:
            mode = os.stat(destination).st_mode & 0o7777
        except FileNotFoundError:
            mode = _new_file_mode()

        destination_fd, self.destination_tmp = tempfile.mkstemp(
            dir=directory, prefix=".rsm-tmp-"
        )
        try:
            source = open(self.decoded_path, "rb")
            target = os.fdopen(destination_fd, "wb")
            with source, target:
                shutil.copyfileobj(source, target, length=1_048_576)
                target.flush()
                os.fchmod(target.fileno(), mode)
                os.fsync(target.fileno())

            if not self.destination_matches(snapshot):
                raise RuntimeError("destination path changed during write")

            if snapshot["kind"] == "missing":
                os.link(self.destination_tmp, destination, follow_symlinks=False)
                self.replace_started = True
                os.unlink(self.destination_tmp)
            else:
                self.replace_started = True
                os.replace(self.destination_tmp, destination)
            self.destination_tmp = None
            self.committed = True

            if snapshot["kind"] == "symlink" and not self.destination_matches(snapshot):
                raise RuntimeError("destination symlink changed during write")
        finally:
            self.remove(self.destination_tmp)

    def run(self):
        try:
            self.receive_payload()
        except BaseException as exc:
            self.remove(self.encoded_path)
            return self.fail("transfer", exc)

        try:
            self.verify_payload()
        except BaseException as exc:
            self.remove(self.encoded_path)
            self.remove(self.decoded_path)
            return self.fail("verify", exc)

        try:
            self.commit_payload()
        except BaseException as exc:
            self.remove(self.encoded_path)
            self.remove(self.decoded_path)
            state = "unknown" if self.replace_started or self.committed else "unchanged"
            return self.fail("commit", exc, state)

        self.remove(self.encoded_path)
        self.remove(self.decoded_path)
        self.emit(
            "DONE",
            {"bytes_written": self.actual_bytes, "sha256": self.actual_sha256},
        )
        return 0


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    path = base64.b64decode(args[0]).decode("utf-8")
    receiver = WriteReceiver(
        path=path,
        expected_encoded=int(args[1]),
        expected_bytes=int(args[2]),
        expected_sha256=args[3],
        token=args[4],
    )
    return receiver.run()


if __name__ == "__main__":
    raise SystemExit(main())
