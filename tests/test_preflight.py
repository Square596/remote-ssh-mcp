from __future__ import annotations

import subprocess

import pytest

from remote_ssh_mcp.session import SessionError, SessionManager


class FakeProc:
    def __init__(self, returncode: int, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", self._stderr


def install_process_mock(monkeypatch: pytest.MonkeyPatch, results: list[FakeProc]) -> None:
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
    monkeypatch.setattr(fake_create_subprocess_exec, "calls", calls, raising=False)


@pytest.mark.asyncio
async def test_preflight_allows_identityfile_auth_without_agent(monkeypatch):
    install_process_mock(
        monkeypatch,
        [
            FakeProc(0),
            FakeProc(2, b"Error connecting to agent: Operation not permitted"),
        ],
    )

    warning = await SessionManager()._preflight("mlspace")

    assert warning is not None
    assert "ssh-agent is not reachable" in warning


@pytest.mark.asyncio
async def test_preflight_allows_empty_agent_by_default(monkeypatch):
    install_process_mock(monkeypatch, [FakeProc(0), FakeProc(1)])

    warning = await SessionManager()._preflight("mlspace")

    assert warning is not None
    assert "no keys loaded" in warning


@pytest.mark.asyncio
async def test_preflight_strict_mode_requires_loaded_agent(monkeypatch):
    install_process_mock(monkeypatch, [FakeProc(0), FakeProc(1)])

    with pytest.raises(SessionError, match="agent forwarding was required"):
        await SessionManager()._preflight("mlspace", require_agent_forwarding=True)


@pytest.mark.asyncio
async def test_preflight_env_strict_mode_requires_loaded_agent(monkeypatch):
    monkeypatch.setenv("REMOTE_SSH_MCP_REQUIRE_AGENT", "1")
    install_process_mock(monkeypatch, [FakeProc(0), FakeProc(2)])

    with pytest.raises(SessionError, match="agent forwarding was required"):
        await SessionManager()._preflight("mlspace")


@pytest.mark.asyncio
async def test_preflight_ssh_failure_is_fatal(monkeypatch):
    install_process_mock(monkeypatch, [FakeProc(255, b"no route to host")])

    with pytest.raises(SessionError, match="Couldn't connect"):
        await SessionManager()._preflight("mlspace")


@pytest.mark.asyncio
async def test_preflight_loaded_agent_has_no_warning(monkeypatch):
    install_process_mock(monkeypatch, [FakeProc(0), FakeProc(0)])

    warning = await SessionManager()._preflight("mlspace")

    assert warning is None
