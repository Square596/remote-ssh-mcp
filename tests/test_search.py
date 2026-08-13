from __future__ import annotations

import subprocess

from remote_ssh_mcp import _search_runner
from remote_ssh_mcp.search import glob_command, grep_command


def test_grep_fallback_treats_no_matches_as_success(
    monkeypatch, tmp_path, capfd
) -> None:
    (tmp_path / "file.txt").write_text("haystack\n", encoding="utf-8")
    monkeypatch.setattr(_search_runner.shutil, "which", lambda command: None)

    result = _search_runner._grep(
        {
            "pattern": "needle",
            "path": str(tmp_path),
            "glob": None,
            "case_insensitive": False,
            "limit": 2,
        }
    )

    assert result == 0
    assert capfd.readouterr().out == ""


def test_grep_fallback_limits_output(monkeypatch, tmp_path, capfd) -> None:
    (tmp_path / "file.txt").write_text("needle\nneedle\nneedle\n", encoding="utf-8")
    monkeypatch.setattr(_search_runner.shutil, "which", lambda command: None)

    result = _search_runner._grep(
        {
            "pattern": "needle",
            "path": str(tmp_path),
            "glob": "*.txt",
            "case_insensitive": False,
            "limit": 2,
        }
    )

    assert result == 0
    assert len(capfd.readouterr().out.splitlines()) == 2


def test_limited_process_preserves_completed_failure(monkeypatch, capfd) -> None:
    class FailedProcess:
        def __init__(self, *, stdout, errors) -> None:
            self.stdout = stdout
            errors.write(b"permission denied\n")

        def poll(self) -> int:
            return 2

        def wait(self, timeout=None) -> int:
            return 2

        def terminate(self) -> None:
            raise AssertionError("completed process must not be terminated")

        def kill(self) -> None:
            raise AssertionError("completed process must not be killed")

    def fake_popen(command, *, stdin, stdout, stderr):
        del command, stdin, stdout
        import io

        return FailedProcess(stdout=io.BytesIO(b"first\nsecond\n"), errors=stderr)

    monkeypatch.setattr(_search_runner.subprocess, "Popen", fake_popen)

    result = _search_runner._emit_limited_process(["search"], 2, {0, 1})

    assert result == 2
    assert capfd.readouterr().out == "first\nsecond\npermission denied\n"


def test_glob_is_sorted_and_bounded(tmp_path, capfd) -> None:
    for name in ("c.py", "a.py", "b.py", "ignored.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")

    result = _search_runner._glob(
        {"path": str(tmp_path), "pattern": "*.py", "limit": 2}
    )

    assert result == 0
    assert capfd.readouterr().out.splitlines() == [
        str(tmp_path / "a.py"),
        str(tmp_path / "b.py"),
    ]


def test_search_command_keeps_inputs_out_of_shell_syntax(tmp_path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("needle; touch marker\n", encoding="utf-8")
    marker = tmp_path / "marker"

    result = subprocess.run(
        grep_command(
            "needle; touch marker",
            str(source),
            glob=None,
            case_insensitive=False,
            limit=2,
        ),
        shell=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )

    assert result.returncode == 0
    assert "needle; touch marker" in result.stdout
    assert not marker.exists()


def test_glob_command_preserves_missing_path_failure(tmp_path) -> None:
    result = subprocess.run(
        glob_command("*.py", str(tmp_path / "missing"), limit=2),
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "No such file or directory" in result.stderr
