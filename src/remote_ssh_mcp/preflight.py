"""Local SSH connectivity and forwarded-agent preflight checks."""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from os.path import expanduser
from typing import Optional

LOCAL_SSH_TIMEOUT = 15.0


class PreflightError(Exception):
    pass


@dataclass
class SshAddResult:
    paths: list[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    output: Optional[str] = None
    warning: Optional[str] = None


@dataclass
class PreflightResult:
    agent_warning: Optional[str] = None
    ssh_add_paths: list[str] = field(default_factory=list)
    ssh_add_exit_code: Optional[int] = None
    ssh_add_output: Optional[str] = None
    forwarded_agent_present: Optional[bool] = None


async def communicate_process(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,
    description: str,
) -> tuple[bytes, bytes]:
    """Communicate with a local child process and always reap it on interruption."""
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise PreflightError(f"{description} timed out after {timeout:g}s.") from None
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise


def _process_output(stdout: bytes, stderr: bytes) -> Optional[str]:
    parts = [
        chunk.decode("utf-8", errors="replace").strip()
        for chunk in (stdout, stderr)
        if chunk.strip()
    ]
    return "\n".join(parts) if parts else None


async def _ssh_add(ssh_add_paths: Optional[list[str]]) -> SshAddResult:
    paths = [expanduser(path) for path in ssh_add_paths or []]
    explicit_paths = bool(paths)
    args = ["ssh-add", *paths]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
        await proc.wait()
        if paths:
            warning = (
                "Timed out while running local `ssh-add` for the requested "
                "key path(s). Continuing with the current ssh-agent; some "
                "requested keys may not have been added. Check the paths "
                "and reconnect with corrected paths if those keys are "
                "needed."
            )
        else:
            warning = (
                "Timed out while running local `ssh-add` before connecting. "
                "Continuing, but forwarded-agent operations from the remote "
                "host may fail."
            )
        return SshAddResult(paths=paths, output=warning, warning=warning)
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise

    output = _process_output(out, err)
    if proc.returncode == 0:
        return SshAddResult(
            paths=paths,
            exit_code=proc.returncode,
            output=output,
        )

    if explicit_paths:
        warning = (
            "Local `ssh-add` returned a non-zero exit code while adding "
            "the requested key path(s). Continuing with the current "
            "ssh-agent; some requested keys may not have been added. "
            "Check the paths and reconnect with corrected paths if those "
            "keys are needed."
        )
    else:
        warning = (
            "Local `ssh-add` did not complete successfully before "
            "connecting. Continuing, but forwarded-agent operations from "
            "the remote host may fail."
        )

    return SshAddResult(
        paths=paths,
        exit_code=proc.returncode,
        output=output,
        warning=warning,
    )


async def run_preflight(
    host: str,
    *,
    agent_forwarding: bool = True,
    ssh_add_paths: Optional[list[str]] = None,
) -> PreflightResult:
    """Validate non-interactive SSH access and report forwarded-agent health."""
    warnings: list[str] = []
    ssh_args = ["ssh"]
    result = PreflightResult()

    if agent_forwarding:
        ssh_add = await _ssh_add(ssh_add_paths)
        result.ssh_add_paths = ssh_add.paths
        result.ssh_add_exit_code = ssh_add.exit_code
        result.ssh_add_output = ssh_add.output
        if ssh_add.warning:
            warnings.append(ssh_add.warning)
        ssh_args.append("-A")

    proc = await asyncio.create_subprocess_exec(
        *ssh_args,
        "-o",
        "BatchMode=yes",
        "-o",
        "NumberOfPasswordPrompts=0",
        "-o",
        "ConnectTimeout=10",
        host,
        "true",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _, err = await communicate_process(
        proc, timeout=LOCAL_SSH_TIMEOUT, description=f"SSH preflight for {host!r}"
    )
    if proc.returncode != 0:
        stderr = err.decode("utf-8", errors="replace").strip() or "(no stderr)"
        raise PreflightError(
            f"Couldn't connect to {host!r} non-interactively. Likely causes:\n"
            f"  - Host {host!r} not in ~/.ssh/config — add a `Host {host}` block.\n"
            f"  - SSH key not authorized on the remote.\n"
            f"  - Host requires interactive auth (password / 2FA) — not supported.\n"
            f"  - Network unreachable / wrong port.\n\n"
            f"Try `ssh {host}` in a regular terminal first. ssh said:\n{stderr}"
        )

    if not agent_forwarding:
        result.agent_warning = "\n".join(warnings) if warnings else None
        return result

    proc = await asyncio.create_subprocess_exec(
        "ssh",
        "-A",
        "-o",
        "BatchMode=yes",
        "-o",
        "NumberOfPasswordPrompts=0",
        "-o",
        "ConnectTimeout=10",
        host,
        "ssh-add",
        "-l",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _, err = await communicate_process(
        proc,
        timeout=LOCAL_SSH_TIMEOUT,
        description=f"forwarded-agent check for {host!r}",
    )
    if proc.returncode == 0:
        result.forwarded_agent_present = True
        result.agent_warning = "\n".join(warnings) if warnings else None
        return result

    result.forwarded_agent_present = False
    if proc.returncode == 1:
        warning = (
            "Connected to the host, but ssh-agent has no keys loaded. "
            "Remote commands will work if SSH auth used another method, "
            "but forwarded-agent operations from the remote host, such as "
            "private git fetches through your local agent, may fail. Run "
            "`ssh-add` or call remote_connect with "
            "`agent_forwarding=false` if forwarding is not needed."
        )
    else:
        stderr = err.decode("utf-8", errors="replace").strip()
        detail = f" ssh-add said: {stderr}" if stderr else ""
        warning = (
            "Connected to the host, but ssh-agent is not reachable from "
            "the remote shell. Remote commands will work if SSH auth used "
            "another method, but forwarded-agent operations from the "
            f"remote host may fail.{detail}"
        )

    warnings.append(warning)
    result.agent_warning = "\n".join(warnings)
    return result
