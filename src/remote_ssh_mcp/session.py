"""Per-host tmux session, per-connection tmux window, lifecycle and pre-flight checks."""

from __future__ import annotations

import asyncio
import re
import secrets
import shlex
import subprocess
from dataclasses import dataclass, field
from os.path import expanduser
from typing import Optional

from .runner import _tmux, capture_pane, run_in_pane

SESSION_PREFIX = "remote-ssh-mcp"
HOST_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def _session_name(host: str) -> str:
    if not HOST_RE.match(host):
        raise SessionError(
            f"Invalid host name {host!r}. Use the alias from your ~/.ssh/config "
            f"(letters, digits, dot, underscore, hyphen only)."
        )
    return f"{SESSION_PREFIX}/{host}"


class SessionError(Exception):
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


@dataclass
class Connection:
    connection_id: str
    host: str
    session_name: str
    window_id: str
    pane_id: str
    project_path: Optional[str]
    label: str
    cwd: str = "?"
    cwd_warning: Optional[str] = None
    agent_warning: Optional[str] = None
    agent_forwarding: bool = True
    ssh_add_paths: list[str] = field(default_factory=list)
    ssh_add_exit_code: Optional[int] = None
    ssh_add_output: Optional[str] = None
    forwarded_agent_present: Optional[bool] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionManager:
    def __init__(self) -> None:
        self._connections: dict[str, Connection] = {}
        self._connect_lock = asyncio.Lock()

    async def connect(
        self,
        host: str,
        project_path: Optional[str] = None,
        label: Optional[str] = None,
        agent_forwarding: bool = True,
        ssh_add_paths: Optional[list[str]] = None,
    ) -> Connection:
        async with self._connect_lock:
            session = _session_name(host)
            label = label or f"agent-{secrets.token_hex(3)}"

            preflight = await self._preflight(
                host,
                agent_forwarding=agent_forwarding,
                ssh_add_paths=ssh_add_paths,
            )

            # ServerAliveInterval keeps idle TCP connections from being torn
            # down by middleboxes or aggressive remote configs — relevant when
            # multiple subagents connect in parallel and one sits briefly idle
            # between connect-completion and its first command.
            agent_flag = "-A " if agent_forwarding else ""
            ssh_cmd = (
                f"ssh {agent_flag}"
                f"-o ServerAliveInterval=30 "
                f"-o ServerAliveCountMax=3 "
                f"{shlex.quote(host)}"
            )
            if await self._session_exists(session):
                window_id, pane_id = await self._new_window(session, label, ssh_cmd)
            else:
                window_id, pane_id = await self._new_session(session, label, ssh_cmd)

            await self._configure_history(session)
            await self._wait_for_shell(pane_id, host)

            cwd_warning: Optional[str] = None
            if project_path:
                cd_cmd = f"cd {shlex.quote(project_path)}"
                cd_result = await run_in_pane(pane_id, cd_cmd, timeout=15)
                # Retry once on timeout — the first paste after a fresh ssh
                # is sometimes lost (agent-forwarding race, slow login script,
                # or a transient network blip). A second attempt usually goes
                # through. If it still times out, surface that cleanly rather
                # than masquerading as a path-not-found.
                if cd_result.timed_out:
                    cd_result = await run_in_pane(pane_id, cd_cmd, timeout=15)
                if cd_result.timed_out:
                    cwd_warning = (
                        f"Couldn't cd into project_path={project_path!r}: the "
                        f"remote shell stopped responding (likely an SSH drop "
                        f"or hung login script). Two attempts both timed out. "
                        f"Try remote_disconnect then remote_connect to start a "
                        f"fresh window. Last pane content:\n"
                        f"{cd_result.stdout.strip()[:400]}"
                    )
                elif cd_result.exit_code != 0:
                    cwd_warning = (
                        f"cd into project_path={project_path!r} returned "
                        f"rc={cd_result.exit_code} — the path likely doesn't "
                        f"exist or you don't have access. Shell output:\n"
                        f"{cd_result.stdout.strip()[:400]}\n"
                        f"The shell is now in its login default directory "
                        f"(usually $HOME). Tools that depend on cwd (uv run, "
                        f"relative paths, project-scoped configs) WILL behave "
                        f"wrong until this is fixed."
                    )

            # Always capture the actual cwd so the agent knows where it is.
            pwd_result = await run_in_pane(pane_id, "pwd", timeout=10)
            cwd = pwd_result.stdout.strip() if pwd_result.exit_code == 0 else "?"

            conn_id = secrets.token_hex(6)
            conn = Connection(
                connection_id=conn_id,
                host=host,
                session_name=session,
                window_id=window_id,
                pane_id=pane_id,
                project_path=project_path,
                label=label,
                cwd=cwd,
                cwd_warning=cwd_warning,
                agent_warning=preflight.agent_warning,
                agent_forwarding=agent_forwarding,
                ssh_add_paths=preflight.ssh_add_paths,
                ssh_add_exit_code=preflight.ssh_add_exit_code,
                ssh_add_output=preflight.ssh_add_output,
                forwarded_agent_present=preflight.forwarded_agent_present,
            )
            self._connections[conn_id] = conn
            return conn

    async def _preflight(
        self,
        host: str,
        agent_forwarding: bool = True,
        ssh_add_paths: Optional[list[str]] = None,
    ) -> PreflightResult:
        warnings: list[str] = []
        ssh_args = ["ssh"]
        result = PreflightResult()

        if agent_forwarding:
            ssh_add = await self._ssh_add(ssh_add_paths)
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
            "ConnectTimeout=10",
            host,
            "true",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            stderr = err.decode("utf-8", errors="replace").strip() or "(no stderr)"
            raise SessionError(
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
            "ConnectTimeout=10",
            host,
            "ssh-add",
            "-l",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _, err = await proc.communicate()
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

    async def _ssh_add(self, ssh_add_paths: Optional[list[str]]) -> SshAddResult:
        paths = [expanduser(path) for path in ssh_add_paths or []]
        explicit_paths = bool(paths)
        args = ["ssh-add"]
        args.extend(paths)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
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

        output = self._process_output(out, err)

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

    @staticmethod
    def _process_output(stdout: bytes, stderr: bytes) -> Optional[str]:
        parts = [
            chunk.decode("utf-8", errors="replace").strip()
            for chunk in (stdout, stderr)
            if chunk.strip()
        ]
        return "\n".join(parts) if parts else None

    async def _session_exists(self, session: str) -> bool:
        rc, _, _ = await _tmux("has-session", "-t", f"={session}")
        return rc == 0

    async def _new_session(self, session: str, label: str, cmd: str) -> tuple[str, str]:
        rc, out, err = await _tmux(
            "new-session",
            "-d",
            "-s",
            session,
            "-n",
            label,
            "-x",
            "220",
            "-y",
            "50",
            "-P",
            "-F",
            "#{window_id} #{pane_id}",
            cmd,
        )
        if rc != 0:
            raise SessionError(
                f"tmux new-session failed: {err.decode(errors='replace')}"
            )
        window_id, pane_id = out.decode().strip().split()
        return window_id, pane_id

    async def _new_window(self, session: str, label: str, cmd: str) -> tuple[str, str]:
        rc, out, err = await _tmux(
            "new-window",
            "-t",
            f"={session}:",
            "-n",
            label,
            "-P",
            "-F",
            "#{window_id} #{pane_id}",
            cmd,
        )
        if rc != 0:
            raise SessionError(
                f"tmux new-window failed: {err.decode(errors='replace')}"
            )
        window_id, pane_id = out.decode().strip().split()
        return window_id, pane_id

    async def _wait_for_shell(
        self, pane_id: str, host: str, timeout: float = 45.0
    ) -> None:
        """Wait until the remote shell is actually ready to accept commands.

        Naive `sleep N` is fragile (slow networks, slow MOTD). Polling
        capture-pane for a prompt doesn't work because tmux's `-p` doesn't
        include the cursor line.

        Trick: send a harmless Enter every 0.5s. While bash is starting,
        these are absorbed by login scripts or just redraw the not-yet-
        rendered prompt — no harm. Once bash is the active reader, an
        empty Enter submits an empty command, redrawing the prompt
        *below* the previous one. The previous prompt is now in
        scrollback and visible to capture-pane. We detect a prompt-like
        line and stop.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout

        # Wait briefly for SSH banner to start streaming.
        await asyncio.sleep(0.5)

        prompt_re = re.compile(r"[\$#>❯»]\s*$", re.MULTILINE)

        while loop.time() < deadline:
            await _tmux("send-keys", "-t", pane_id, "Enter")
            await asyncio.sleep(0.5)
            screen = await capture_pane(pane_id)
            non_empty = [ln for ln in screen.splitlines() if ln.strip()]
            if non_empty:
                last = non_empty[-1].rstrip()
                if prompt_re.search(last):
                    return

        screen = await capture_pane(pane_id)
        raise SessionError(
            f"SSH login to {host!r} did not yield a usable shell within "
            f"{timeout:.0f}s. The prompt is expected to end in `$`, `#`, "
            f"`>`, `❯`, or `»`; if your shell uses something else, set a "
            f"more conventional PS1 in your remote shell rc. Last pane:\n"
            f"{screen[-1500:]}"
        )

    async def _configure_history(self, session: str) -> None:
        # Bigger history makes large `remote_read` outputs survivable.
        await _tmux("set-option", "-t", f"={session}", "history-limit", "100000")

    async def disconnect(self, connection_id: str) -> dict:
        conn = self._connections.pop(connection_id, None)
        if conn is None:
            return {"closed": False, "reason": "no such connection"}

        # Try a graceful exit first so the user sees the SSH session close.
        try:
            await run_in_pane(conn.pane_id, "exit", timeout=3)
        except Exception:
            pass

        await _tmux("kill-window", "-t", conn.window_id)
        # If session has zero windows, tmux auto-closes it.
        return {"closed": True}

    def get(self, connection_id: str) -> Connection:
        conn = self._connections.get(connection_id)
        if conn is None:
            raise SessionError(
                f"No active connection {connection_id!r}. "
                f"Call remote_connect first, or check remote_status()."
            )
        return conn

    def list_connections(self) -> list[dict]:
        return [
            {
                "connection_id": c.connection_id,
                "host": c.host,
                "label": c.label,
                "project_path": c.project_path,
                "cwd": c.cwd,
                "session_name": c.session_name,
                "window_id": c.window_id,
                "agent_warning": c.agent_warning,
                "agent_forwarding": c.agent_forwarding,
                "ssh_add_paths": c.ssh_add_paths,
                "ssh_add_exit_code": c.ssh_add_exit_code,
                "ssh_add_output": c.ssh_add_output,
                "forwarded_agent_present": c.forwarded_agent_present,
            }
            for c in self._connections.values()
        ]
