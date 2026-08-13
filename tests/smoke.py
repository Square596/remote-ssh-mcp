"""End-to-end smoke test for the public MCP tools on a real SSH host.

Run with:
    uv run --frozen python tests/smoke.py <host> \
        --work-dir <writable-existing-remote-dir> \
        --second-dir <existing-remote-dir> \
        --missing-dir <nonexistent-remote-dir>

The host must be reachable non-interactively through its SSH config alias.
Every run uses unique subdirectories and verifies their removal before exit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import posixpath
import secrets
import shlex
import sys
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from remote_ssh_mcp import server
from remote_ssh_mcp.session import SessionManager

ROOT = Path(__file__).parents[1]
LARGE_WRITE_BYTES = 5_000_000
READ_CHUNK_BYTES = 500_000


class SmokeFailure(RuntimeError):
    pass


def step(label: str) -> None:
    print(f"\n=== {label} ===")


def remote_path(root: str, name: str) -> str:
    return posixpath.join(root.rstrip("/") or "/", name)


def project_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return project["project"]["version"]


class SmokeHarness:
    def __init__(
        self,
        host: str,
        work_dir: str,
        second_dir: str,
        missing_dir: str,
    ) -> None:
        self.host = host
        self.work_dir = work_dir.rstrip("/") or "/"
        self.second_dir = second_dir.rstrip("/") or "/"
        self.run_id = f"rsm-smoke-{secrets.token_hex(6)}"
        self.primary_dir = remote_path(self.work_dir, f"{self.run_id}-main")
        self.secondary_dir = remote_path(self.second_dir, f"{self.run_id}-second")
        self.missing_dir = remote_path(missing_dir, self.run_id)
        self.connection_ids: list[str] = []
        self.cleanup_failures: list[str] = []
        self._validate_cleanup_paths()

    def _validate_cleanup_paths(self) -> None:
        for parent, path in (
            (self.work_dir, self.primary_dir),
            (self.second_dir, self.secondary_dir),
        ):
            if posixpath.dirname(path) != parent or not posixpath.basename(
                path
            ).startswith("rsm-smoke-"):
                raise ValueError(f"unsafe smoke cleanup path: {path!r}")

    @staticmethod
    def _tool(name: str):
        registered = getattr(server, name)
        return getattr(registered, "fn", registered)

    async def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        result = await self._tool(name)(**arguments)
        if not isinstance(result, dict):
            raise SmokeFailure(f"{name} returned a non-object result: {result!r}")
        return result

    async def require_ok(self, name: str, **arguments: Any) -> dict[str, Any]:
        result = await self.call(name, **arguments)
        if result.get("ok") is not True:
            raise SmokeFailure(f"{name} failed: {result!r}")
        return result

    @staticmethod
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise SmokeFailure(message)

    async def connect(self, project_path: str, label: str) -> dict[str, Any]:
        result = await self.require_ok(
            "remote_connect",
            host=self.host,
            project_path=project_path,
            label=label,
        )
        connection_id = result.get("connection_id")
        if not isinstance(connection_id, str):
            raise SmokeFailure(f"remote_connect omitted connection_id: {result!r}")
        self.connection_ids.append(connection_id)
        return result

    async def run(self) -> None:
        installed_version = distribution_version("remote-ssh-mcp")
        expected_version = project_version()
        self.require(
            installed_version == expected_version,
            f"checkout version {expected_version} loaded package {installed_version}",
        )
        print(f"testing remote-ssh-mcp {installed_version}")

        step("connect and create isolated work directories")
        main = await self.connect(self.work_dir, f"{self.run_id}-main")
        main_id = main["connection_id"]
        self.require(main.get("cwd_warning") is None, repr(main.get("cwd_warning")))
        created = await self.require_ok(
            "remote_run",
            connection_id=main_id,
            cmd=(
                "mkdir -p -- "
                f"{shlex.quote(self.primary_dir)} "
                f"{shlex.quote(self.secondary_dir)}"
            ),
        )
        self.require(created["exit_code"] == 0, f"mkdir failed: {created!r}")

        step("remote_run command, failure, and persistent state")
        result = await self.require_ok(
            "remote_run", connection_id=main_id, cmd="hostname && pwd"
        )
        self.require(
            result["exit_code"] == 0 and bool(result["stdout"].strip()),
            f"hostname/pwd failed: {result!r}",
        )
        result = await self.require_ok("remote_run", connection_id=main_id, cmd="false")
        self.require(result["exit_code"] == 1, f"false returned {result!r}")
        await self.require_ok(
            "remote_run",
            connection_id=main_id,
            cmd=(f"cd {shlex.quote(self.primary_dir)} && export RSM_SMOKE_STATE=kept"),
        )
        result = await self.require_ok(
            "remote_run",
            connection_id=main_id,
            cmd='pwd; printf "state=%s\\n" "$RSM_SMOKE_STATE"',
        )
        self.require(
            self.primary_dir in result["stdout"] and "state=kept" in result["stdout"],
            f"shell state did not persist: {result!r}",
        )

        step("remote_run multiline program and heredoc")
        result = await self.require_ok(
            "remote_run",
            connection_id=main_id,
            cmd=(
                "for i in 1 2 3 4 5; do\n"
                "  echo line$i\n"
                "done\n"
                "cat <<'RSM_HEREDOC'\n"
                "literal:$HOME\n"
                "RSM_HEREDOC\n"
            ),
        )
        expected_lines = [f"line{i}" for i in range(1, 6)] + ["literal:$HOME"]
        self.require(
            result["stdout"].splitlines() == expected_lines,
            f"multiline output mismatch: {result!r}",
        )

        step("remote_run clean timeout output and pane recovery")
        result = await self.require_ok(
            "remote_run",
            connection_id=main_id,
            cmd="printf 'timeout-marker\\n'; sleep 30",
            timeout=1,
        )
        self.require(result.get("timed_out") is True, repr(result))
        self.require(result.get("pane_recovered") is True, repr(result))
        self.require(result.get("stdout") == "timeout-marker", repr(result))
        recovered = await self.require_ok(
            "remote_run", connection_id=main_id, cmd="printf recovered"
        )
        self.require(recovered.get("stdout") == "recovered", repr(recovered))

        step("remote_write and remote_read small UTF-8 text")
        text_path = remote_path(self.primary_dir, "text.txt")
        text = "Hello, remote!\nLine 2 with ✨ unicode\nLine 3\n"
        written = await self.require_ok(
            "remote_write", connection_id=main_id, path=text_path, content=text
        )
        self.require(
            written["bytes_written"] == len(text.encode()),
            f"small write length mismatch: {written!r}",
        )
        read = await self.require_ok(
            "remote_read", connection_id=main_id, path=text_path
        )
        self.require(read["content"] == text, f"small read mismatch: {read!r}")

        step("remote_write 5 MB UTF-8 text and remote_read it in chunks")
        prefix = "✨ remote write start\n"
        suffix = "\nremote write end ✨\n"
        middle_bytes = LARGE_WRITE_BYTES - len(prefix.encode()) - len(suffix.encode())
        large_text = prefix + ("x" * middle_bytes) + suffix
        self.require(
            len(large_text.encode()) == LARGE_WRITE_BYTES,
            "large payload construction produced the wrong byte length",
        )
        large_path = remote_path(self.primary_dir, "large.txt")
        written = await self.require_ok(
            "remote_write", connection_id=main_id, path=large_path, content=large_text
        )
        self.require(
            written["bytes_written"] == LARGE_WRITE_BYTES,
            f"large write length mismatch: {written!r}",
        )
        chunks: list[str] = []
        offset = 0
        while offset < LARGE_WRITE_BYTES:
            chunk = await self.require_ok(
                "remote_read",
                connection_id=main_id,
                path=large_path,
                offset=offset,
                limit=READ_CHUNK_BYTES,
            )
            self.require(
                chunk.get("encoding_warning") is None,
                f"large UTF-8 read split an encoded character: {chunk!r}",
            )
            self.require(
                chunk["total_size"] == LARGE_WRITE_BYTES,
                f"large read reported the wrong size: {chunk!r}",
            )
            byte_size = chunk["byte_size"]
            self.require(byte_size > 0, f"large read made no progress at {offset}")
            chunks.append(chunk["content"])
            offset += byte_size
        self.require("".join(chunks) == large_text, "large chunked read mismatch")

        step("remote_edit exact and replace-all behavior")
        await self.require_ok(
            "remote_write",
            connection_id=main_id,
            path=text_path,
            content="alpha\nbravo\ncharlie\n",
        )
        edited = await self.require_ok(
            "remote_edit",
            connection_id=main_id,
            path=text_path,
            old="bravo",
            new="DELTA",
        )
        self.require(edited["occurrences_replaced"] == 1, repr(edited))
        await self.require_ok(
            "remote_write", connection_id=main_id, path=text_path, content="x x x\n"
        )
        rejected = await self.call(
            "remote_edit",
            connection_id=main_id,
            path=text_path,
            old="x",
            new="Y",
        )
        self.require(
            rejected.get("ok") is False and "not unique" in rejected.get("error", ""),
            f"non-unique edit was not rejected: {rejected!r}",
        )
        edited = await self.require_ok(
            "remote_edit",
            connection_id=main_id,
            path=text_path,
            old="x",
            new="Y",
            replace_all=True,
        )
        self.require(edited["occurrences_replaced"] == 3, repr(edited))

        step("remote_run bounded long output")
        result = await self.require_ok(
            "remote_run",
            connection_id=main_id,
            cmd="for i in $(seq 1 1000); do echo line$i; done",
        )
        lines = result["stdout"].splitlines()
        self.require(
            len(lines) == 1000 and lines[0] == "line1" and lines[-1] == "line1000",
            f"long output mismatch: {len(lines)} lines",
        )

        step("remote_grep and remote_glob exact truncation")
        for name in ("grep-a.txt", "grep-b.txt"):
            await self.require_ok(
                "remote_write",
                connection_id=main_id,
                path=remote_path(self.primary_dir, name),
                content=f"{name}\nNEEDLE_42\n",
            )
        grep = await self.require_ok(
            "remote_grep",
            connection_id=main_id,
            pattern="NEEDLE_42",
            path=self.primary_dir,
            max_results=1,
        )
        self.require(
            grep["count"] == 1 and grep["truncated"] is True,
            f"grep truncation mismatch: {grep!r}",
        )
        glob = await self.require_ok(
            "remote_glob",
            connection_id=main_id,
            pattern="grep-*.txt",
            path=self.primary_dir,
            max_results=1,
        )
        self.require(
            glob["count"] == 1 and glob["truncated"] is True,
            f"glob truncation mismatch: {glob!r}",
        )

        step("connection isolation and status")
        secondary = await self.connect(self.second_dir, f"{self.run_id}-second")
        secondary_id = secondary["connection_id"]
        self.require(
            secondary.get("cwd_warning") is None,
            repr(secondary.get("cwd_warning")),
        )
        await self.require_ok(
            "remote_run",
            connection_id=secondary_id,
            cmd=(
                f"cd {shlex.quote(self.secondary_dir)} && "
                "export RSM_SECONDARY_STATE=isolated"
            ),
        )
        parent = await self.require_ok(
            "remote_run",
            connection_id=main_id,
            cmd='printf "secondary=%s cwd=%s\\n" "$RSM_SECONDARY_STATE" "$PWD"',
        )
        child = await self.require_ok(
            "remote_run",
            connection_id=secondary_id,
            cmd='printf "secondary=%s cwd=%s\\n" "$RSM_SECONDARY_STATE" "$PWD"',
        )
        self.require("isolated" not in parent["stdout"], repr(parent))
        self.require(
            "isolated" in child["stdout"] and self.secondary_dir in child["stdout"],
            repr(child),
        )
        status = await self.require_ok("remote_status")
        active_ids = {item["connection_id"] for item in status["connections"]}
        self.require(
            {main_id, secondary_id}.issubset(active_ids),
            f"status omitted active connections: {status!r}",
        )

        step("bad project path warning")
        bad = await self.connect(self.missing_dir, f"{self.run_id}-missing")
        warning = bad.get("cwd_warning")
        self.require(
            isinstance(warning, str)
            and "rc=" in warning
            and "doesn't exist" in warning
            and "timed out" not in warning,
            f"bad project path warning mismatch: {bad!r}",
        )

        step("20 rapid public write/read round-trips")
        for index in range(20):
            path = remote_path(self.primary_dir, f"stress-{index}.txt")
            content = f"payload-{index}\n"
            await self.require_ok(
                "remote_write",
                connection_id=main_id,
                path=path,
                content=content,
            )
            read = await self.require_ok(
                "remote_read", connection_id=main_id, path=path
            )
            self.require(read["content"] == content, f"stress mismatch at {index}")

    async def cleanup(self) -> None:
        step("verified cleanup")
        cleanup_id: str | None = None
        try:
            cleanup = await self.connect(self.work_dir, f"{self.run_id}-cleanup")
            cleanup_id = cleanup["connection_id"]
        except Exception as exc:
            self.cleanup_failures.append(f"could not open cleanup connection: {exc}")
            if self.connection_ids:
                cleanup_id = self.connection_ids[0]

        if cleanup_id is not None:
            cleanup_command = (
                f"cd {shlex.quote(self.work_dir)} && rm -rf -- "
                f"{shlex.quote(self.primary_dir)} {shlex.quote(self.secondary_dir)}"
            )
            try:
                result = await self.require_ok(
                    "remote_run",
                    connection_id=cleanup_id,
                    cmd=cleanup_command,
                    timeout=120,
                )
                if result.get("exit_code") != 0:
                    self.cleanup_failures.append(f"cleanup command failed: {result!r}")

                paths_json = json.dumps(
                    [self.primary_dir, self.secondary_dir], ensure_ascii=False
                )
                verify_code = (
                    "import json,os,sys;"
                    f"paths=json.loads({paths_json!r});"
                    "remaining=[p for p in paths if os.path.lexists(p)];"
                    "print('cleanup-verified' if not remaining else repr(remaining));"
                    "sys.exit(bool(remaining))"
                )
                verified = await self.require_ok(
                    "remote_run",
                    connection_id=cleanup_id,
                    cmd=f"python3 -c {shlex.quote(verify_code)}",
                )
                if (
                    verified.get("exit_code") != 0
                    or verified.get("stdout", "").strip() != "cleanup-verified"
                ):
                    self.cleanup_failures.append(
                        f"remote paths remain after cleanup: {verified!r}"
                    )
            except Exception as exc:
                self.cleanup_failures.append(f"remote cleanup failed: {exc}")

        for connection_id in reversed(self.connection_ids.copy()):
            try:
                result = await self.call(
                    "remote_disconnect", connection_id=connection_id
                )
                if result.get("ok") is not True:
                    self.cleanup_failures.append(
                        f"disconnect {connection_id} failed: {result!r}"
                    )
            except Exception as exc:
                self.cleanup_failures.append(
                    f"disconnect {connection_id} raised: {exc}"
                )

        try:
            status = await self.require_ok("remote_status")
            if status["connections"]:
                self.cleanup_failures.append(
                    f"connections remain after cleanup: {status['connections']!r}"
                )
        except Exception as exc:
            self.cleanup_failures.append(f"could not verify connection cleanup: {exc}")


async def main(host: str, work_dir: str, second_dir: str, missing_dir: str) -> int:
    server.sessions = SessionManager()
    harness = SmokeHarness(host, work_dir, second_dir, missing_dir)
    failure: str | None = None
    try:
        await harness.run()
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        await harness.cleanup()

    if failure is not None:
        print(f"\nSMOKE FAILURE: {failure}")
    if harness.cleanup_failures:
        print("\nCLEANUP FAILURES:")
        for cleanup_failure in harness.cleanup_failures:
            print(f"  - {cleanup_failure}")
    if failure is not None or harness.cleanup_failures:
        return 1

    print("\nALL CHECKS PASSED; CLEANUP VERIFIED")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("host")
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--second-dir", required=True)
    parser.add_argument("--missing-dir", required=True)
    args = parser.parse_args()
    sys.exit(
        asyncio.run(main(args.host, args.work_dir, args.second_dir, args.missing_dir))
    )
