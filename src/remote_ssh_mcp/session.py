"""Per-host tmux session, per-connection tmux window, lifecycle and pre-flight checks."""

from __future__ import annotations

import asyncio
import re
import secrets
import shlex
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from os.path import expanduser
from typing import AsyncIterator, Literal, Optional

from .runner import PANE_HISTORY_LIMIT, RunResult, recover_pane, run_in_pane
from .tmux import TmuxError, capture_pane, kill_window, paste_text, tmux

SESSION_PREFIX = "remote-ssh-mcp"
HOST_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
SHELL_READY_CAPTURE_LINES = 100
SHELL_READY_POLL_INTERVAL = 0.5
SHELL_READY_RETRY_INTERVAL = 2.0
LOCAL_SSH_TIMEOUT = 15.0
LAST_ERROR_LIMIT = 1_000

ConnectionState = Literal["ready", "busy", "unresponsive", "closing"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _session_name(host: str) -> str:
    if host.startswith("-") or not HOST_RE.fullmatch(host):
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
    state: ConnectionState = "ready"
    current_operation: Optional[str] = None
    last_activity_at: str = field(default_factory=_now)
    last_error: Optional[str] = None
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
        session = _session_name(host)
        label = label or f"agent-{secrets.token_hex(3)}"
        self._validate_text("label", label, allow_newlines=False)
        if project_path is not None:
            self._validate_text("project_path", project_path)
        self._validate_ssh_add_paths(ssh_add_paths)

        preflight = await self._preflight(
            host,
            agent_forwarding=agent_forwarding,
            ssh_add_paths=ssh_add_paths,
        )

        ssh_args = ["ssh"]
        if agent_forwarding:
            ssh_args.append("-A")
        ssh_args.extend(
            [
                "-o",
                "BatchMode=yes",
                "-o",
                "NumberOfPasswordPrompts=0",
                "-o",
                "ServerAliveInterval=30",
                "-o",
                "ServerAliveCountMax=3",
                "-o",
                "EscapeChar=none",
                host,
            ]
        )
        ssh_cmd = " ".join(shlex.quote(arg) for arg in ssh_args)
        window_id: str | None = None
        pane_id: str | None = None
        try:
            # Only tmux session/window creation needs cross-connection
            # serialization. Login readiness can proceed independently per pane.
            async with self._connect_lock:
                if await self._session_exists(session):
                    await self._configure_history(session)
                    window_id, pane_id = await self._new_window(session, label, ssh_cmd)
                else:
                    window_id, pane_id = await self._new_session(
                        session, label, ssh_cmd
                    )

            await self._wait_for_shell(pane_id, host)

            cwd_warning: Optional[str] = None
            if project_path:
                cd_cmd = f"cd {shlex.quote(project_path)}"
                cd_result = await run_in_pane(pane_id, cd_cmd, timeout=15)
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

            pwd_result = await run_in_pane(pane_id, "pwd", timeout=10)
            cwd = getattr(pwd_result, "cwd", None) or (
                pwd_result.stdout.strip() if pwd_result.exit_code == 0 else "?"
            )

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
        except BaseException:
            if window_id is not None:
                await asyncio.shield(self._cleanup_window(window_id))
            raise

    @staticmethod
    def _validate_text(name: str, value: str, *, allow_newlines: bool = True) -> None:
        if "\x00" in value:
            raise SessionError(f"{name} must not contain NUL characters.")
        if not allow_newlines and any(char in value for char in "\r\n"):
            raise SessionError(f"{name} must not contain newlines.")

    @classmethod
    def _validate_ssh_add_paths(cls, paths: Optional[list[str]]) -> None:
        for path in paths or []:
            cls._validate_text("ssh_add_paths entry", path, allow_newlines=False)
            if not path or path.startswith("-"):
                raise SessionError(
                    "ssh_add_paths entries must be non-empty file paths and must "
                    "not start with '-'."
                )

    @staticmethod
    async def _cleanup_window(window_id: str) -> None:
        try:
            await kill_window(window_id)
        except (TmuxError, ValueError):
            pass

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
            "NumberOfPasswordPrompts=0",
            "-o",
            "ConnectTimeout=10",
            host,
            "true",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _, err = await self._communicate_process(
            proc, timeout=LOCAL_SSH_TIMEOUT, description=f"SSH preflight for {host!r}"
        )
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
        _, err = await self._communicate_process(
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

    @staticmethod
    async def _communicate_process(
        proc: asyncio.subprocess.Process,
        *,
        timeout: float,
        description: str,
    ) -> tuple[bytes, bytes]:
        try:
            return await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise SessionError(f"{description} timed out after {timeout:g}s.") from None
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise

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
        except TimeoutError:
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
        rc, _, _ = await tmux("has-session", "-t", session)
        return rc == 0

    async def _new_session(self, session: str, label: str, cmd: str) -> tuple[str, str]:
        rc, out, err = await tmux(
            "new-session",
            "-d",
            "-s",
            session,
            "-n",
            "__bootstrap__",
            "-x",
            "220",
            "-y",
            "50",
            "-P",
            "-F",
            "#{window_id} #{pane_id}",
        )
        if rc != 0:
            raise SessionError(
                f"tmux new-session failed: {err.decode(errors='replace')}"
            )
        bootstrap_window, _ = out.decode().strip().split()
        window_id: str | None = None
        try:
            # tmux snapshots history-limit when a pane is created. Establish
            # the session option before creating the real SSH pane.
            await self._configure_history(session)
            window_id, pane_id = await self._new_window(session, label, cmd)
            await self._cleanup_window(bootstrap_window)
            return window_id, pane_id
        except BaseException:
            if window_id is not None:
                await asyncio.shield(self._cleanup_window(window_id))
            await asyncio.shield(self._cleanup_window(bootstrap_window))
            raise

    async def _new_window(self, session: str, label: str, cmd: str) -> tuple[str, str]:
        rc, out, err = await tmux(
            "new-window",
            "-t",
            f"{session}:",
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

        Prompt text is not a reliable readiness signal: it may contain any
        characters, span multiple lines, or change dynamically. Instead, send
        a harmless command whose output contains a unique marker. The marker
        is split across arguments in the typed command, so terminal echo alone
        cannot be mistaken for successful execution.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        marker = secrets.token_hex(8)
        expected = f"__RSM_READY_{marker}__"
        probe = f"printf '%s%s\\n' '__RSM_READY_' '{marker}__'"
        next_probe_at = loop.time()
        last_screen = ""

        while loop.time() < deadline:
            if loop.time() >= next_probe_at:
                try:
                    await paste_text(pane_id, probe)
                except RuntimeError as exc:
                    raise SessionError(
                        f"SSH login to {host!r} could not send the shell "
                        f"readiness probe: {exc}"
                    ) from exc
                next_probe_at = loop.time() + SHELL_READY_RETRY_INTERVAL

            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(SHELL_READY_POLL_INTERVAL, remaining))

            try:
                last_screen = await capture_pane(
                    pane_id, lines=SHELL_READY_CAPTURE_LINES
                )
            except RuntimeError as exc:
                raise SessionError(
                    f"SSH login to {host!r} could not inspect its tmux pane "
                    f"while waiting for shell readiness: {exc}"
                ) from exc
            if expected in last_screen:
                return

        if not last_screen:
            try:
                last_screen = await capture_pane(
                    pane_id, lines=SHELL_READY_CAPTURE_LINES
                )
            except RuntimeError as exc:
                raise SessionError(
                    f"SSH login to {host!r} could not inspect its tmux pane "
                    f"while waiting for shell readiness: {exc}"
                ) from exc

        raise SessionError(
            f"SSH login to {host!r} did not yield a shell that could execute "
            f"the readiness probe within {timeout:.0f}s. The remote login may "
            f"be stuck in startup scripts or an interactive program. "
            f"Last pane:\n{last_screen[-1500:]}"
        )

    async def _configure_history(self, session: str) -> None:
        # Bigger history makes large `remote_read` outputs survivable.
        rc, _, err = await tmux(
            "set-option",
            "-t",
            session,
            "history-limit",
            str(PANE_HISTORY_LIMIT),
        )
        if rc != 0:
            raise SessionError(
                f"tmux history configuration failed: {err.decode(errors='replace')}"
            )

    @asynccontextmanager
    async def operation(
        self,
        conn: Connection,
        name: str,
        *,
        recover_on_failure: bool = False,
    ) -> AsyncIterator[Connection]:
        """Serialize one pane operation and publish its lifecycle in status."""
        async with conn.lock:
            self._ensure_usable(conn)
            conn.state = "busy"
            conn.current_operation = name
            conn.last_activity_at = _now()
            conn.last_error = None
            try:
                yield conn
            except asyncio.CancelledError:
                message = f"{name} was cancelled"
                if recover_on_failure:
                    recovered = await asyncio.shield(recover_pane(conn.pane_id))
                    self._record_recovery(conn, recovered, message)
                elif conn.last_error is None:
                    self._record_error(conn, message)
                raise
            except Exception as exc:
                if recover_on_failure:
                    details = getattr(exc, "details", {})
                    recovered = details.get("pane_recovered")
                    if not isinstance(recovered, bool):
                        recovered = await recover_pane(conn.pane_id)
                        if isinstance(details, dict):
                            details["pane_recovered"] = recovered
                    self._record_recovery(conn, recovered, f"{name} failed: {exc}")
                else:
                    self._record_error(conn, str(exc))
                raise
            finally:
                conn.current_operation = None
                conn.last_activity_at = _now()
                if conn.state == "busy":
                    conn.state = "ready"

    async def run_command(
        self,
        conn: Connection,
        command: str,
        *,
        timeout: float,
        operation_name: str = "run",
    ) -> RunResult:
        """Run a public command and recover the pane on timeout or cancellation."""
        async with self.operation(conn, operation_name):
            try:
                result = await run_in_pane(conn.pane_id, command, timeout=timeout)
            except asyncio.CancelledError:
                recovered = await asyncio.shield(recover_pane(conn.pane_id))
                self._record_recovery(
                    conn, recovered, f"{operation_name} was cancelled"
                )
                raise
            except (RuntimeError, ValueError) as exc:
                recovered = await recover_pane(conn.pane_id)
                self._record_recovery(
                    conn, recovered, f"{operation_name} failed: {exc}"
                )
                suffix = "pane recovered" if recovered else "connection is unresponsive"
                raise SessionError(f"remote run failed: {exc}; {suffix}.") from exc

            if result.timed_out:
                recovered = await recover_pane(conn.pane_id)
                result.pane_recovered = recovered
                self._record_recovery(
                    conn,
                    recovered,
                    f"{operation_name} timed out after {timeout:g}s",
                )
            elif result.cwd is not None:
                conn.cwd = result.cwd
            return result

    @staticmethod
    def _record_error(conn: Connection, message: str) -> None:
        conn.last_error = message[-LAST_ERROR_LIMIT:]

    def _record_recovery(self, conn: Connection, recovered: bool, message: str) -> None:
        self._record_error(conn, message)
        if not recovered:
            conn.state = "unresponsive"

    @staticmethod
    def _ensure_usable(conn: Connection) -> None:
        if conn.state == "unresponsive":
            raise SessionError(
                f"Connection {conn.connection_id!r} is unresponsive after failed "
                "pane recovery. Call remote_disconnect, then remote_connect."
            )
        if conn.state == "closing":
            raise SessionError(f"Connection {conn.connection_id!r} is closing.")

    async def disconnect(self, connection_id: str) -> dict:
        conn = self._connections.get(connection_id)
        if conn is None:
            return {"closed": False, "reason": "no such connection"}

        async with conn.lock:
            conn.state = "closing"
            conn.current_operation = "disconnect"
            conn.last_activity_at = _now()
            task = asyncio.create_task(kill_window(conn.window_id))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                self._connections.pop(connection_id, None)
                raise
            except (TmuxError, ValueError) as exc:
                conn.state = "unresponsive"
                conn.current_operation = None
                self._record_error(conn, f"disconnect failed: {exc}")
                raise SessionError(f"Could not close connection: {exc}") from exc
            self._connections.pop(connection_id, None)
        return {"closed": True}

    def get(self, connection_id: str) -> Connection:
        conn = self._connections.get(connection_id)
        if conn is None:
            raise SessionError(
                f"No active connection {connection_id!r}. "
                f"Call remote_connect first, or check remote_status()."
            )
        self._ensure_usable(conn)
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
                "state": c.state,
                "current_operation": c.current_operation,
                "last_activity_at": c.last_activity_at,
                "last_error": c.last_error,
            }
            for c in self._connections.values()
        ]
