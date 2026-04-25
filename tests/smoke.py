"""End-to-end smoke test that exercises every module against a real SSH host.

Run with:
    uv run python tests/smoke.py <host>

`<host>` must be reachable via `ssh -A <host>` non-interactively
(BatchMode=yes) — i.e. configured in your ~/.ssh/config with key-based
auth. Nothing is hardcoded; substitute whatever alias works for you.
"""

from __future__ import annotations

import asyncio
import sys

from remote_ssh_mcp.files import edit_remote_file, read_remote_file, write_remote_file
from remote_ssh_mcp.runner import run_in_pane
from remote_ssh_mcp.session import SessionManager


def step(label: str) -> None:
    print(f"\n=== {label} ===")


async def main(host: str) -> int:
    sm = SessionManager()
    failures: list[str] = []

    step(f"connect to {host}")
    conn = await sm.connect(host=host, project_path="/tmp", label="smoke-test")
    print(f"connection_id={conn.connection_id} pane={conn.pane_id} attach={conn.session_name}")

    step("remote_run: hostname && pwd")
    r = await run_in_pane(conn.pane_id, "hostname && pwd")
    print(f"rc={r.exit_code} duration={r.duration_ms}ms")
    print(r.stdout)
    if r.exit_code != 0 or not r.stdout.strip():
        failures.append(f"hostname/pwd failed: rc={r.exit_code}")

    step("remote_run: failing command")
    r = await run_in_pane(conn.pane_id, "false")
    print(f"rc={r.exit_code} (expect 1)")
    if r.exit_code != 1:
        failures.append(f"`false` should yield rc=1, got {r.exit_code}")

    step("remote_run: shell state persists (cd + export)")
    await run_in_pane(conn.pane_id, "cd /tmp && export RSM_TEST=42")
    r = await run_in_pane(conn.pane_id, 'pwd; echo "RSM_TEST=$RSM_TEST"')
    print(r.stdout)
    if "RSM_TEST=42" not in r.stdout or "/tmp" not in r.stdout:
        failures.append("shell state did not persist across calls")

    step("remote_run: multi-line stdout")
    r = await run_in_pane(conn.pane_id, "for i in 1 2 3 4 5; do echo line$i; done")
    print(r.stdout)
    if r.stdout.strip().splitlines() != [f"line{i}" for i in range(1, 6)]:
        failures.append(f"multi-line stdout mismatch: {r.stdout!r}")

    step("remote_run: command with embedded special chars")
    r = await run_in_pane(conn.pane_id, """echo "hello 'world' \\$HOME=$HOME" """)
    print(r.stdout)
    if "hello" not in r.stdout or "$HOME=" not in r.stdout:
        failures.append("special-char echo failed")

    step("remote_write + remote_read roundtrip (small text)")
    test_path = "/tmp/rsm_smoke_test.txt"
    payload = b"Hello, remote!\nLine 2 with \xe2\x9c\xa8 unicode\nLine 3\n"
    await write_remote_file(conn.pane_id, test_path, payload)
    data, total = await read_remote_file(conn.pane_id, test_path)
    print(f"wrote {len(payload)} bytes, read {len(data)} bytes, total_size reported={total}")
    if data != payload:
        failures.append(f"roundtrip mismatch:\n  wrote: {payload!r}\n  read:  {data!r}")

    step("remote_write: medium binary (random 200 KB)")
    import os as _os
    blob = _os.urandom(200_000)
    medium_path = "/tmp/rsm_smoke_blob.bin"
    await write_remote_file(conn.pane_id, medium_path, blob)
    data, total = await read_remote_file(conn.pane_id, medium_path)
    if data != blob:
        failures.append(f"binary blob roundtrip mismatch (got {len(data)}/{len(blob)} bytes)")
    else:
        print(f"binary blob roundtrip OK ({len(blob)} bytes)")

    step("remote_edit: unique replacement")
    await write_remote_file(conn.pane_id, test_path, b"alpha\nbravo\ncharlie\n")
    res = await edit_remote_file(conn.pane_id, test_path, old="bravo", new="DELTA")
    print(f"replaced={res.occurrences_replaced} bytes_after={res.bytes_after}")
    data, _ = await read_remote_file(conn.pane_id, test_path)
    if data != b"alpha\nDELTA\ncharlie\n":
        failures.append(f"edit produced wrong content: {data!r}")

    step("remote_edit: non-unique without replace_all should error")
    await write_remote_file(conn.pane_id, test_path, b"x x x\n")
    try:
        await edit_remote_file(conn.pane_id, test_path, old="x", new="Y")
    except Exception as e:
        print(f"got expected error: {e!r}")
    else:
        failures.append("non-unique edit should have raised but didn't")

    step("remote_edit: replace_all")
    res = await edit_remote_file(conn.pane_id, test_path, old="x", new="Y", replace_all=True)
    data, _ = await read_remote_file(conn.pane_id, test_path)
    if data != b"Y Y Y\n" or res.occurrences_replaced != 3:
        failures.append(f"replace_all produced wrong content: {data!r} replaced={res.occurrences_replaced}")
    else:
        print(f"replace_all OK ({res.occurrences_replaced} occurrences)")

    step("remote_run: long output (1000 lines)")
    r = await run_in_pane(conn.pane_id, "for i in $(seq 1 1000); do echo line$i; done")
    lines = r.stdout.strip().splitlines()
    if len(lines) != 1000 or lines[0] != "line1" or lines[-1] != "line1000":
        failures.append(f"long output: got {len(lines)} lines (expected 1000)")
    else:
        print(f"long output OK ({len(lines)} lines, duration={r.duration_ms}ms)")

    step("remote_grep: search for unique pattern in /tmp")
    await write_remote_file(conn.pane_id, "/tmp/rsm_grep_a.txt", b"foo\nNEEDLE_42\nbar\n")
    await write_remote_file(conn.pane_id, "/tmp/rsm_grep_b.txt", b"baz\nqux\n")
    r = await run_in_pane(
        conn.pane_id,
        "if command -v rg >/dev/null 2>&1; then "
        "rg -n NEEDLE_42 /tmp 2>/dev/null || true; "
        "else grep -rn NEEDLE_42 /tmp 2>/dev/null || true; fi",
    )
    if "NEEDLE_42" not in r.stdout or "rsm_grep_a.txt" not in r.stdout:
        failures.append(f"grep didn't find NEEDLE_42 in expected file: {r.stdout!r}")
    else:
        print("grep OK")

    step("remote_glob: find *.txt in /tmp")
    r = await run_in_pane(
        conn.pane_id, "find /tmp -type f -name 'rsm_grep_*.txt' 2>/dev/null"
    )
    files_found = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if not any("rsm_grep_a.txt" in f for f in files_found) or not any(
        "rsm_grep_b.txt" in f for f in files_found
    ):
        failures.append(f"glob missing expected files: {files_found}")
    else:
        print(f"glob OK ({len(files_found)} files)")

    step("multi-connection isolation (parent + simulated subagent)")
    sub = await sm.connect(host=host, project_path="/var", label="smoke-sub")
    print(f"sub connection_id={sub.connection_id} pane={sub.pane_id}")
    await run_in_pane(sub.pane_id, "export RSM_SUB_VAR=subvalue")
    parent_check = await run_in_pane(conn.pane_id, "echo P=$RSM_SUB_VAR/cwd=$(pwd)")
    sub_check = await run_in_pane(sub.pane_id, "echo S=$RSM_SUB_VAR/cwd=$(pwd)")
    if "RSM_SUB_VAR=subvalue" not in (await run_in_pane(sub.pane_id, "echo RSM_SUB_VAR=$RSM_SUB_VAR")).stdout:
        failures.append("subagent connection didn't keep its own env")
    if "subvalue" in parent_check.stdout:
        failures.append("parent connection saw subagent's env (no isolation!)")
    if "/var" not in sub_check.stdout or "/tmp" not in parent_check.stdout:
        failures.append(f"cwd isolation broken: parent={parent_check.stdout!r} sub={sub_check.stdout!r}")
    else:
        print("isolation OK: parent in /tmp, sub in /var")

    step("disconnect (sub then main)")
    await sm.disconnect(sub.connection_id)
    info = await sm.disconnect(conn.connection_id)
    print(info)

    print("\n" + "=" * 50)
    if failures:
        print(f"FAILURES: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <host>", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
