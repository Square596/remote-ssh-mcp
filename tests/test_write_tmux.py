from __future__ import annotations

import base64
import hashlib
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import uuid

import pytest

from remote_ssh_mcp import files
from remote_ssh_mcp.files import FileOpError, write_remote_file
from remote_ssh_mcp.runner import PANE_HISTORY_LIMIT, capture_pane, run_in_pane
from remote_ssh_mcp.search import glob_command, grep_command
from remote_ssh_mcp.session import Connection, SessionManager

pytestmark = pytest.mark.skipif(
    os.environ.get("RSM_RUN_TMUX_TESTS") != "1",
    reason="set RSM_RUN_TMUX_TESTS=1 to run real tmux transfer tests",
)


@pytest.fixture(params=("bash", "zsh"))
def tmux_pane(request, monkeypatch):
    tmux = shutil.which("tmux")
    shell = shutil.which(request.param)
    if tmux is None or shell is None:
        pytest.skip(f"tmux and {request.param} are required")

    socket_root = tempfile.TemporaryDirectory(prefix="rsm-tmux-", dir="/tmp")
    monkeypatch.setenv("TMUX_TMPDIR", socket_root.name)
    session = f"rsm-write-test-{uuid.uuid4().hex[:10]}"
    shell_args = ["-f"] if request.param == "zsh" else ["--noprofile", "--norc"]
    subprocess.run(
        [
            tmux,
            "new-session",
            "-d",
            "-s",
            session,
            "-x",
            "220",
            "-y",
            "50",
            "-P",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            tmux,
            "set-option",
            "-t",
            session,
            "history-limit",
            str(PANE_HISTORY_LIMIT),
        ],
        check=True,
        capture_output=True,
    )
    proc = subprocess.run(
        [
            tmux,
            "new-window",
            "-d",
            "-t",
            f"{session}:",
            "-P",
            "-F",
            "#{pane_id}",
            shell,
            *shell_args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    pane_id = proc.stdout.strip()
    subprocess.run(
        [tmux, "kill-window", "-t", f"{session}:0"],
        check=True,
        capture_output=True,
    )
    try:
        yield request.param, pane_id
    finally:
        subprocess.run(
            [tmux, "kill-session", "-t", session],
            check=False,
            capture_output=True,
        )
        socket_root.cleanup()


def _payload(size: int) -> bytes:
    seed = b"RSM_PAYLOAD_MUST_NOT_APPEAR_IN_THE_PANE_0123456789\n"
    return (seed * ((size // len(seed)) + 1))[:size]


@pytest.mark.asyncio
async def test_session_lookup_is_exact_for_prefix_names(monkeypatch) -> None:
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux is required")

    socket_root = tempfile.TemporaryDirectory(prefix="rsm-tmux-", dir="/tmp")
    monkeypatch.setenv("TMUX_TMPDIR", socket_root.name)
    stem = f"rsm-prefix-{uuid.uuid4().hex[:10]}"
    names = (stem, f"{stem}-extended")
    try:
        for name in reversed(names):
            subprocess.run(
                [tmux, "new-session", "-d", "-s", name],
                check=True,
                capture_output=True,
            )

        target = await SessionManager()._session_target(stem)
        assert target is not None
        resolved = subprocess.run(
            [tmux, "display-message", "-p", "-t", target, "#{session_name}"],
            check=True,
            capture_output=True,
            text=True,
        )

        assert resolved.stdout.strip() == stem
    finally:
        for name in names:
            subprocess.run(
                [tmux, "kill-session", "-t", name],
                check=False,
                capture_output=True,
            )
        socket_root.cleanup()


@pytest.mark.asyncio
async def test_multiline_run_supports_heredocs_and_preserves_state(tmux_pane, tmp_path):
    shell, pane_id = tmux_pane
    command = (
        "export RSM_MULTILINE_STATE=kept\n"
        "for item in alpha beta; do\n"
        "  printf 'item:%s\\n' \"$item\"\n"
        "done\n"
        "cat <<'RSM_HEREDOC'\n"
        "literal:$RSM_MULTILINE_STATE\n"
        "RSM_HEREDOC\n"
        f"cd {shlex.quote(str(tmp_path))}\n"
    )

    result = await run_in_pane(pane_id, command, timeout=10)
    state = await run_in_pane(
        pane_id,
        'printf "STATE:%s:%s\\n" "$PWD" "$RSM_MULTILINE_STATE"',
        timeout=10,
    )
    failure = await run_in_pane(
        pane_id, "printf 'before-failure\\n'\nfalse\n", timeout=10
    )
    no_newline = await run_in_pane(pane_id, "printf no-trailing-newline", timeout=10)

    assert result.exit_code == 0, (shell, result.stdout)
    assert result.stdout.splitlines() == [
        "item:alpha",
        "item:beta",
        "literal:$RSM_MULTILINE_STATE",
    ]
    assert state.exit_code == 0, (shell, state.stdout)
    assert state.stdout == f"STATE:{tmp_path}:kept"
    assert failure.exit_code == 1, (shell, failure.stdout)
    assert failure.stdout == "before-failure"
    assert no_newline.exit_code == 0, (shell, no_newline.stdout)
    assert no_newline.stdout == "no-trailing-newline"


@pytest.mark.asyncio
async def test_timed_out_run_recovers_shell(tmux_pane):
    shell, pane_id = tmux_pane
    conn = Connection(
        connection_id="test",
        host="local-test",
        session_name="test",
        window_id="@test",
        pane_id=pane_id,
        project_path=None,
        label="test",
    )

    trace_enabled = await run_in_pane(pane_id, "set -x", timeout=5)
    result = await SessionManager().run_command(
        conn, "printf 'timeout-marker\\n'; sleep 30", timeout=0.2
    )
    probe = await run_in_pane(pane_id, "printf recovered", timeout=5)

    assert trace_enabled.stdout == "", (shell, trace_enabled.stdout)
    assert result.timed_out is True, (shell, result.stdout)
    assert result.pane_recovered is True, (shell, result.stdout)
    assert result.stdout == "timeout-marker", (shell, result.stdout)
    assert "__RSM_" not in result.stdout, (shell, result.stdout)
    assert conn.state == "ready"
    assert probe.stdout == "recovered"


@pytest.mark.asyncio
async def test_run_preserves_explicit_xtrace_state_without_leaking(tmux_pane):
    shell, pane_id = tmux_pane

    enabled = await run_in_pane(pane_id, "set -x", timeout=5)
    traced = await run_in_pane(
        pane_id, "case $- in *x*) printf on ;; *) printf off ;; esac", timeout=5
    )
    disabled = await run_in_pane(pane_id, "set +x", timeout=5)
    untraced = await run_in_pane(
        pane_id, "case $- in *x*) printf on ;; *) printf off ;; esac", timeout=5
    )

    assert enabled.stdout == "", (shell, enabled.stdout)
    assert traced.stdout == "on", (shell, traced.stdout)
    assert disabled.stdout == "", (shell, disabled.stdout)
    assert untraced.stdout == "off", (shell, untraced.stdout)
    assert all(
        "__RSM_" not in result.stdout
        for result in (enabled, traced, disabled, untraced)
    )


@pytest.mark.asyncio
async def test_run_preserves_framing_beyond_previous_history_limit(tmux_pane):
    shell, pane_id = tmux_pane
    command = "python3 -c 'import sys; sys.stdout.write(\"x\\n\" * 100001)'"

    result = await run_in_pane(pane_id, command, timeout=15)

    assert result.exit_code == 0, (shell, result.stdout[-1000:])
    assert len(result.stdout.splitlines()) == 100001


@pytest.mark.asyncio
async def test_bounded_search_commands_report_results_and_failures(tmux_pane, tmp_path):
    shell, pane_id = tmux_pane
    sample = tmp_path / "sample.txt"
    sample.write_text("needle\nneedle\nneedle\nneedle\n", encoding="utf-8")
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text(name, encoding="utf-8")

    grep = await run_in_pane(
        pane_id,
        grep_command(
            "needle",
            str(sample),
            glob=None,
            case_insensitive=False,
            limit=3,
        ),
        timeout=10,
    )
    no_matches = await run_in_pane(
        pane_id,
        grep_command(
            "absent",
            str(sample),
            glob=None,
            case_insensitive=False,
            limit=3,
        ),
        timeout=10,
    )
    glob = await run_in_pane(
        pane_id, glob_command("*.py", str(tmp_path), limit=3), timeout=10
    )
    missing = await run_in_pane(
        pane_id,
        glob_command("*.py", str(tmp_path / "missing"), limit=3),
        timeout=10,
    )

    assert grep.exit_code == 0, (shell, grep.stdout)
    assert len(grep.stdout.splitlines()) == 3
    assert no_matches.exit_code == 0, (shell, no_matches.stdout)
    assert no_matches.stdout == ""
    assert glob.exit_code == 0, (shell, glob.stdout)
    assert len(glob.stdout.splitlines()) == 3
    assert missing.exit_code == 2, (shell, missing.stdout)
    assert "missing" in missing.stdout


@pytest.mark.asyncio
async def test_timed_out_file_read_recovers_shell(tmux_pane, tmp_path, monkeypatch):
    shell, pane_id = tmux_pane
    fifo = tmp_path / "blocked-read"
    os.mkfifo(fifo)
    real_run = files.run_in_pane
    conn = Connection(
        connection_id="test",
        host="local-test",
        session_name="test",
        window_id="@test",
        pane_id=pane_id,
        project_path=None,
        label="test",
    )

    async def capped_run(pane, command, timeout=60):
        return await real_run(pane, command, timeout=min(timeout, 0.2))

    monkeypatch.setattr(files, "run_in_pane", capped_run)
    manager = SessionManager()

    with pytest.raises(FileOpError) as exc_info:
        async with manager.operation(conn, "read", recover_on_failure=True):
            await files.read_remote_file(pane_id, str(fifo))

    probe = await real_run(pane_id, "printf recovered", timeout=5)
    assert exc_info.value.details["pane_recovered"] is True, shell
    assert conn.state == "ready"
    assert probe.stdout == "recovered"


@pytest.mark.asyncio
async def test_large_writes_roundtrip_without_echoing_payload(tmux_pane, tmp_path):
    _, pane_id = tmux_pane
    target = tmp_path / "nested dir" / "tool '✨.sh"
    target.parent.mkdir()
    target.write_bytes(b"old")
    target.chmod(0o755)

    for size in (0, 20_000, 200_000, 1_000_000, 5_000_000):
        payload = _payload(size)
        written = await write_remote_file(pane_id, str(target), payload)

        assert written == size
        assert target.read_bytes() == payload
        assert stat.S_IMODE(target.stat().st_mode) == 0o755

    screen = await capture_pane(pane_id)
    encoded_prefix = base64.b64encode(_payload(192)).decode("ascii")
    assert encoded_prefix not in screen


@pytest.mark.asyncio
async def test_write_obeys_umask_and_preserves_symlink(tmux_pane, tmp_path):
    _, pane_id = tmux_pane
    new_file = tmp_path / "nested" / "created.txt"
    target = tmp_path / "target.txt"
    target.write_bytes(b"old")
    target.chmod(0o604)
    link = tmp_path / "link.txt"
    link.symlink_to(target.name)

    umask_result = await run_in_pane(pane_id, "umask 027", timeout=5)
    assert umask_result.exit_code == 0
    await write_remote_file(pane_id, str(new_file), b"created")
    await write_remote_file(pane_id, str(link), b"replacement")

    assert stat.S_IMODE(new_file.stat().st_mode) == 0o640
    assert link.is_symlink()
    assert target.read_bytes() == b"replacement"
    assert stat.S_IMODE(target.stat().st_mode) == 0o604


@pytest.mark.asyncio
async def test_corrupt_payload_is_rejected_before_replace(
    tmux_pane, tmp_path, monkeypatch
):
    _, pane_id = tmux_pane
    target = tmp_path / "target.txt"
    target.write_bytes(b"old content")
    payload = _payload(100_000)
    real_paste_bytes = files.paste_bytes
    corrupted = False

    async def corrupt_first_chunk(pane, chunk, timeout=files.WRITE_READY_TIMEOUT):
        nonlocal corrupted
        if not corrupted and not chunk.startswith(b".__RSM_WRITE_PAYLOAD_END_"):
            chunk = bytearray(chunk)
            chunk[10] = ord("A") if chunk[10] != ord("A") else ord("B")
            chunk = bytes(chunk)
            corrupted = True
        await real_paste_bytes(pane, chunk, timeout=timeout)

    monkeypatch.setattr(files, "paste_bytes", corrupt_first_chunk)

    with pytest.raises(FileOpError) as exc_info:
        await write_remote_file(pane_id, str(target), payload)

    assert target.read_bytes() == b"old content"
    assert exc_info.value.details == {
        "stage": "verify",
        "verified": False,
        "destination_state": "unchanged",
        "pane_recovered": True,
        "expected_bytes": len(payload),
        "actual_bytes": len(payload),
        "expected_sha256": hashlib.sha256(payload).hexdigest(),
        "actual_sha256": exc_info.value.details["actual_sha256"],
    }
    assert (
        exc_info.value.details["actual_sha256"]
        != exc_info.value.details["expected_sha256"]
    )
    probe = await run_in_pane(pane_id, "echo pane-recovered", timeout=5)
    assert probe.stdout == "pane-recovered"


@pytest.mark.asyncio
async def test_transfer_failure_interrupts_receiver_and_recovers_pane(
    tmux_pane, tmp_path, monkeypatch
):
    _, pane_id = tmux_pane
    target = tmp_path / "target.txt"
    target.write_bytes(b"old content")
    payload = _payload(150_000)
    real_paste_bytes = files.paste_bytes
    payload_chunks = 0

    async def fail_second_chunk(pane, chunk, timeout=files.WRITE_READY_TIMEOUT):
        nonlocal payload_chunks
        if not chunk.startswith(b".__RSM_WRITE_PAYLOAD_END_"):
            payload_chunks += 1
            if payload_chunks == 2:
                raise RuntimeError("injected tmux transfer failure")
        await real_paste_bytes(pane, chunk, timeout=timeout)

    monkeypatch.setattr(files, "paste_bytes", fail_second_chunk)

    with pytest.raises(FileOpError) as exc_info:
        await write_remote_file(pane_id, str(target), payload)

    assert target.read_bytes() == b"old content"
    assert exc_info.value.details["stage"] == "transfer"
    assert exc_info.value.details["verified"] is False
    assert exc_info.value.details["destination_state"] == "unknown"
    assert exc_info.value.details["pane_recovered"] is True
    probe = await run_in_pane(pane_id, "echo pane-recovered", timeout=5)
    assert probe.stdout == "pane-recovered"


@pytest.mark.asyncio
async def test_lost_acknowledgement_returns_success_after_destination_probe(
    tmux_pane, tmp_path, monkeypatch
):
    _, pane_id = tmux_pane
    target = tmp_path / "target.txt"
    payload = _payload(80_000)
    real_wait = files._wait_for_receiver
    lost_exit = False

    async def lose_first_exit(pane, token, kinds, timeout):
        nonlocal lost_exit
        event = await real_wait(pane, token, kinds, timeout)
        if kinds == ("EXIT",) and not lost_exit:
            lost_exit = True
            raise TimeoutError("injected lost acknowledgement")
        return event

    monkeypatch.setattr(files, "_wait_for_receiver", lose_first_exit)

    written = await write_remote_file(pane_id, str(target), payload)

    assert written == len(payload)
    assert target.read_bytes() == payload


@pytest.mark.asyncio
async def test_payload_delimiter_may_split_across_pty_reads(
    tmux_pane, tmp_path, monkeypatch
):
    _, pane_id = tmux_pane
    target = tmp_path / "target.txt"
    payload = _payload(30_000)
    real_paste_bytes = files.paste_bytes

    async def split_delimiter(pane, chunk, timeout=files.WRITE_READY_TIMEOUT):
        if chunk.startswith(b".__RSM_WRITE_PAYLOAD_END_"):
            middle = len(chunk) // 2
            await real_paste_bytes(pane, chunk[:middle], timeout=timeout)
            await real_paste_bytes(pane, chunk[middle:], timeout=timeout)
            return
        await real_paste_bytes(pane, chunk, timeout=timeout)

    monkeypatch.setattr(files, "paste_bytes", split_delimiter)

    written = await write_remote_file(pane_id, str(target), payload)

    assert written == len(payload)
    assert target.read_bytes() == payload


@pytest.mark.asyncio
async def test_commit_failure_reports_unchanged_destination(tmux_pane, tmp_path):
    _, pane_id = tmux_pane
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocker", encoding="utf-8")
    target = blocker / "target.txt"

    with pytest.raises(FileOpError) as exc_info:
        await write_remote_file(pane_id, str(target), b"new content")

    assert blocker.read_text(encoding="utf-8") == "blocker"
    assert exc_info.value.details["stage"] == "commit"
    assert exc_info.value.details["verified"] is False
    assert exc_info.value.details["destination_state"] == "unchanged"
    assert exc_info.value.details["pane_recovered"] is True
