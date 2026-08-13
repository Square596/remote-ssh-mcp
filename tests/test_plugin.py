from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "remote-ssh-mcp"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _launcher() -> tuple[str, list[str]]:
    server = _json(PLUGIN / ".mcp.json")["mcpServers"]["remote-ssh"]
    return server["command"], server["args"]


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _launcher_env(
    tmp_path: Path,
    *,
    installed_spec: str,
    install_spec: str,
    install_fails: bool = False,
) -> tuple[dict[str, str], Path]:
    tool_root = tmp_path / "tools"
    bin_root = tmp_path / "bin"
    command_root = tmp_path / "commands"
    spec_file = tmp_path / "installed-spec"
    log_file = tmp_path / "launcher.log"
    spec_file.write_text(installed_spec + "\n", encoding="utf-8")

    _write_executable(
        tool_root / "remote-ssh-mcp" / "bin" / "python",
        '#!/bin/sh\ncat "$RSM_TEST_SPEC_FILE"\n',
    )
    _write_executable(
        bin_root / "remote-ssh-mcp",
        "#!/bin/sh\nprintf 'exec\\n' >>\"$RSM_TEST_LOG\"\n",
    )
    _write_executable(
        command_root / "uv",
        """#!/bin/sh
if [ "$1" = tool ] && [ "$2" = dir ]; then
    if [ "$3" = --bin ]; then
        printf '%s\n' "$RSM_TEST_BIN_ROOT"
    else
        printf '%s\n' "$RSM_TEST_TOOL_ROOT"
    fi
elif [ "$1" = tool ] && [ "$2" = install ]; then
    printf 'install:%s\n' "$*" >>"$RSM_TEST_LOG"
    printf 'installer stdout\n'
    printf 'installer stderr\n' >&2
    [ "$RSM_TEST_INSTALL_FAILS" != 1 ] || exit 42
    printf '%s\n' "$RSM_TEST_INSTALL_SPEC" >"$RSM_TEST_SPEC_FILE"
else
    exit 99
fi
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{command_root}:{env['PATH']}",
            "RSM_TEST_BIN_ROOT": str(bin_root),
            "RSM_TEST_INSTALL_FAILS": "1" if install_fails else "0",
            "RSM_TEST_INSTALL_SPEC": install_spec,
            "RSM_TEST_LOG": str(log_file),
            "RSM_TEST_SPEC_FILE": str(spec_file),
            "RSM_TEST_TOOL_ROOT": str(tool_root),
        }
    )
    return env, log_file


def _run_launcher(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    shell, args = _launcher()
    assert args[0] == "-lc"
    return subprocess.run(
        [shell, *args],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )


def test_plugin_references_existing_components() -> None:
    manifest = _json(PLUGIN / ".codex-plugin" / "plugin.json")

    assert (PLUGIN / manifest["skills"]).is_dir()
    assert (PLUGIN / manifest["mcpServers"]).is_file()
    assert list((PLUGIN / "skills").glob("*/SKILL.md"))


def test_bundled_server_pins_release_and_sdk() -> None:
    config = _json(PLUGIN / ".mcp.json")
    server = config["mcpServers"]["remote-ssh"]
    command = " ".join(server["args"])

    assert server["command"] == "sh"
    assert "mcp==2.0.0" in command
    assert "remote-ssh-mcp@v0.4.3" in command
    assert "--upgrade" not in command
    assert "SSH_AUTH_SOCK" in server["env_vars"]


def test_bundled_server_reuses_verified_install_without_installing(
    tmp_path: Path,
) -> None:
    expected = "0.4.3|2.0.0|https://github.com/Square596/remote-ssh-mcp|v0.4.3"
    env, log_file = _launcher_env(
        tmp_path,
        installed_spec=expected,
        install_spec=expected,
    )

    result = _run_launcher(env)

    assert result.returncode == 0
    assert result.stdout == ""
    assert log_file.read_text(encoding="utf-8").splitlines() == ["exec"]


def test_bundled_server_repairs_mismatched_install(tmp_path: Path) -> None:
    expected = "0.4.3|2.0.0|https://github.com/Square596/remote-ssh-mcp|v0.4.3"
    env, log_file = _launcher_env(
        tmp_path,
        installed_spec="0.4.3|1.25.0|local|",
        install_spec=expected,
    )

    result = _run_launcher(env)

    assert result.returncode == 0
    assert result.stdout == ""
    log = log_file.read_text(encoding="utf-8").splitlines()
    assert log[-1] == "exec"
    assert "--force --reinstall --with mcp==2.0.0" in log[0]
    assert "remote-ssh-mcp@v0.4.3" in log[0]
    assert "installer stdout" in result.stderr


@pytest.mark.parametrize(
    ("install_fails", "install_spec", "error"),
    [
        (True, "unused", "could not install its pinned release"),
        (False, "0.4.3|2.0.1|local|", "installed an unexpected package"),
    ],
)
def test_bundled_server_never_executes_unverified_install(
    tmp_path: Path,
    install_fails: bool,
    install_spec: str,
    error: str,
) -> None:
    env, log_file = _launcher_env(
        tmp_path,
        installed_spec="wrong",
        install_spec=install_spec,
        install_fails=install_fails,
    )

    result = _run_launcher(env)

    assert result.returncode != 0
    assert result.stdout == ""
    assert error in result.stderr
    assert "exec" not in log_file.read_text(encoding="utf-8").splitlines()
