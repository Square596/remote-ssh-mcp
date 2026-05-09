from __future__ import annotations

import subprocess
import uuid
from types import SimpleNamespace

import pytest

from remote_ssh_mcp.session import PreflightResult, SessionError, SessionManager


class FakeProc:
    def __init__(
        self, returncode: int, stderr: bytes = b"", stdout: bytes = b""
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def install_process_mock(
    monkeypatch: pytest.MonkeyPatch, results: list[FakeProc]
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE
        calls.append(tuple(args))
        return results.pop(0)

    monkeypatch.setattr(
        "remote_ssh_mcp.session.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    return calls


@pytest.fixture
def ssh_host() -> str:
    return f"host-{uuid.uuid4().hex}"


@pytest.mark.asyncio
async def test_preflight_allows_identityfile_auth_without_agent(monkeypatch, ssh_host):
    calls = install_process_mock(
        monkeypatch,
        [
            FakeProc(0),
            FakeProc(0),
            FakeProc(2, b"Error connecting to agent: Operation not permitted"),
        ],
    )

    result = await SessionManager()._preflight(ssh_host)

    assert result.agent_warning is not None
    assert "ssh-agent is not reachable from the remote shell" in result.agent_warning
    assert result.forwarded_agent_present is False
    assert result.ssh_add_exit_code == 0
    assert result.ssh_add_paths == []
    assert calls[0] == ("ssh-add",)
    assert calls[2] == (
        "ssh",
        "-A",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        ssh_host,
        "ssh-add",
        "-l",
    )


@pytest.mark.asyncio
async def test_preflight_allows_empty_agent_by_default(monkeypatch, ssh_host):
    install_process_mock(monkeypatch, [FakeProc(0), FakeProc(0), FakeProc(1)])

    result = await SessionManager()._preflight(ssh_host)

    assert result.agent_warning is not None
    assert "no keys loaded" in result.agent_warning
    assert result.forwarded_agent_present is False


@pytest.mark.asyncio
async def test_preflight_local_bare_ssh_add_failure_warns(monkeypatch, ssh_host):
    install_process_mock(
        monkeypatch,
        [
            FakeProc(2, b"Error connecting to agent"),
            FakeProc(0),
            FakeProc(0),
        ],
    )

    result = await SessionManager()._preflight(ssh_host)

    assert result.agent_warning is not None
    assert "Local `ssh-add` did not complete successfully" in result.agent_warning
    assert result.ssh_add_exit_code == 2
    assert result.ssh_add_output == "Error connecting to agent"
    assert result.forwarded_agent_present is True


@pytest.mark.asyncio
async def test_preflight_explicit_ssh_add_paths_bulk_failure_warns(
    monkeypatch, ssh_host, tmp_path
):
    key_paths = [str(tmp_path / f"requested-{i}") for i in range(1, 4)]
    ssh_add_output = (
        f"Identity added: {key_paths[0]} ({key_paths[0]})\n"
        f"{key_paths[1]}: No such file or directory\n"
        f"Identity added: {key_paths[2]} ({key_paths[2]})"
    ).encode()
    expected_ssh_add_call = (
        "ssh-add",
        *key_paths,
    )
    calls = install_process_mock(
        monkeypatch,
        [
            FakeProc(1, stderr=ssh_add_output),
            FakeProc(0),
            FakeProc(0),
        ],
    )

    result = await SessionManager()._preflight(
        ssh_host,
        ssh_add_paths=key_paths,
    )

    assert calls[0] == expected_ssh_add_call
    assert result.agent_warning is not None
    assert "some requested keys may not have been added" in result.agent_warning
    assert "reconnect with corrected paths" in result.agent_warning
    assert result.ssh_add_paths == key_paths
    assert result.ssh_add_exit_code == 1
    assert result.ssh_add_output == ssh_add_output.decode()
    assert result.forwarded_agent_present is True


@pytest.mark.asyncio
async def test_preflight_ssh_failure_is_fatal(monkeypatch, ssh_host):
    install_process_mock(monkeypatch, [FakeProc(0), FakeProc(255, b"no route to host")])

    with pytest.raises(SessionError, match="Couldn't connect"):
        await SessionManager()._preflight(ssh_host)


@pytest.mark.asyncio
async def test_preflight_loaded_agent_has_no_warning(monkeypatch, ssh_host):
    install_process_mock(monkeypatch, [FakeProc(0), FakeProc(0), FakeProc(0)])

    result = await SessionManager()._preflight(ssh_host)

    assert result.agent_warning is None
    assert result.forwarded_agent_present is True
    assert result.ssh_add_exit_code == 0


@pytest.mark.asyncio
async def test_preflight_can_disable_agent_forwarding(monkeypatch, ssh_host):
    calls = install_process_mock(monkeypatch, [FakeProc(0)])

    result = await SessionManager()._preflight(ssh_host, agent_forwarding=False)

    assert result.agent_warning is None
    assert result.forwarded_agent_present is None
    assert result.ssh_add_exit_code is None
    assert result.ssh_add_output is None
    assert calls == [
        (
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            ssh_host,
            "true",
        )
    ]


@pytest.mark.asyncio
async def test_connect_omits_agent_forwarding_from_session_command(
    monkeypatch, ssh_host, tmp_path
):
    sm = SessionManager()
    session_cmds: list[str] = []
    key_path = str(tmp_path / "requested")

    async def fake_preflight(*args, **kwargs):
        return PreflightResult(
            agent_warning="warn",
            ssh_add_paths=[key_path],
            ssh_add_exit_code=1,
            ssh_add_output="ssh-add output",
            forwarded_agent_present=False,
        )

    async def fake_session_exists(*args, **kwargs):
        return False

    async def fake_new_session(session, label, cmd):
        session_cmds.append(cmd)
        return "%1", "%1.0"

    async def fake_configure_history(*args, **kwargs):
        return None

    async def fake_wait_for_shell(*args, **kwargs):
        return None

    async def fake_run_in_pane(*args, **kwargs):
        return SimpleNamespace(stdout=f"{tmp_path / 'cwd'}\n", exit_code=0)

    monkeypatch.setattr(sm, "_preflight", fake_preflight)
    monkeypatch.setattr(sm, "_session_exists", fake_session_exists)
    monkeypatch.setattr(sm, "_new_session", fake_new_session)
    monkeypatch.setattr(sm, "_configure_history", fake_configure_history)
    monkeypatch.setattr(sm, "_wait_for_shell", fake_wait_for_shell)
    monkeypatch.setattr("remote_ssh_mcp.session.run_in_pane", fake_run_in_pane)

    conn = await sm.connect(ssh_host, agent_forwarding=False)

    assert session_cmds == [
        f"ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=3 {ssh_host}"
    ]
    assert conn.agent_warning == "warn"
    assert conn.agent_forwarding is False
    assert conn.ssh_add_paths == [key_path]
    assert conn.ssh_add_exit_code == 1
    assert conn.ssh_add_output == "ssh-add output"
    assert conn.forwarded_agent_present is False


@pytest.mark.asyncio
async def test_remote_connect_response_includes_agent_status(
    monkeypatch, ssh_host, tmp_path
):
    from remote_ssh_mcp import server

    key_path = str(tmp_path / "requested")
    project_path = str(tmp_path / "project")
    conn = SimpleNamespace(
        connection_id="abc123",
        host=ssh_host,
        project_path=project_path,
        cwd=project_path,
        cwd_warning=None,
        agent_warning="warn",
        agent_forwarding=True,
        ssh_add_paths=[key_path],
        ssh_add_exit_code=1,
        ssh_add_output="ssh-add output",
        forwarded_agent_present=False,
        session_name=f"remote-ssh-mcp/{ssh_host}",
        label="test",
    )

    class FakeSessions:
        async def connect(self, **kwargs):
            return conn

    monkeypatch.setattr(server, "sessions", FakeSessions())

    remote_connect = getattr(server.remote_connect, "fn", server.remote_connect)
    result = await remote_connect(host=ssh_host, project_path=project_path)

    assert result["ok"] is True
    assert result["agent_forwarding"] is True
    assert result["ssh_add_paths"] == [key_path]
    assert result["ssh_add_exit_code"] == 1
    assert result["ssh_add_output"] == "ssh-add output"
    assert result["forwarded_agent_present"] is False
