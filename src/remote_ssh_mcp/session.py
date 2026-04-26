"""Per-host tmux session, per-connection tmux window, lifecycle and pre-flight checks."""

from __future__ import annotations

import asyncio
import re
import secrets
import shlex
import subprocess
from dataclasses import dataclass, field
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
    ) -> Connection:
        async with self._connect_lock:
            session = _session_name(host)
            label = label or f"agent-{secrets.token_hex(3)}"

            await self._preflight(host)

            # ServerAliveInterval keeps idle TCP connections from being torn
            # down by middleboxes or aggressive remote configs — relevant when
            # multiple subagents connect in parallel and one sits briefly idle
            # between connect-completion and its first command.
            ssh_cmd = (
                f"ssh -A "
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
            )
            self._connections[conn_id] = conn
            return conn

    async def _preflight(self, host: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "ssh-add", "-l", stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        _, _ = await proc.communicate()
        if proc.returncode not in (0, 1):
            # rc 0 = keys present, 1 = no keys, 2 = agent not running.
            raise SessionError(
                "ssh-agent not reachable. Start it (`eval $(ssh-agent)`) and "
                "load a key with `ssh-add` before connecting."
            )
        if proc.returncode == 1:
            raise SessionError(
                "ssh-agent has no keys loaded. Run `ssh-add` (or "
                "`ssh-add ~/.ssh/your_key`) and try again — agent forwarding "
                "(`ssh -A`) requires loaded keys."
            )

        proc = await asyncio.create_subprocess_exec(
            "ssh",
            "-A",
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

    async def _session_exists(self, session: str) -> bool:
        rc, _, _ = await _tmux("has-session", "-t", f"={session}")
        return rc == 0

    async def _new_session(
        self, session: str, label: str, cmd: str
    ) -> tuple[str, str]:
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

    async def _new_window(
        self, session: str, label: str, cmd: str
    ) -> tuple[str, str]:
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
            }
            for c in self._connections.values()
        ]
