"""MCP server entry point. Registers the remote_* tools and dispatches to the
SessionManager / runner / files modules."""

from __future__ import annotations

import argparse
from importlib.metadata import version as distribution_version
from typing import Optional, Sequence

from ._mcp_compat import MCPServer, tool
from .contracts import (
    ConnectResponse,
    DisconnectResponse,
    EditResponse,
    GlobResponse,
    GrepResponse,
    ReadResponse,
    RunResponse,
    StatusResponse,
    WriteResponse,
)
from .files import (
    MAX_READ_BYTES,
    FileOpError,
    edit_remote_file,
    read_remote_file,
    write_remote_file,
)
from .session import SessionError, SessionManager
from .search import glob_command, grep_command

mcp = MCPServer("remote-ssh-mcp")
sessions = SessionManager()
MAX_RUN_OUTPUT_BYTES = 1_048_576
MAX_RUN_TIMEOUT_SECONDS = 86_400
MAX_SEARCH_RESULTS = 10_000
_TRUNCATION_MARKER = "\n... remote output truncated ...\n"


def _err(message: str, **extra) -> dict:
    return {"ok": False, "error": message, **extra}


def _ok(**fields) -> dict:
    return {"ok": True, **fields}


def _exception_response(exc: Exception) -> dict:
    details = getattr(exc, "details", {})
    if not isinstance(details, dict):
        details = {}
    return _err(str(exc), **details)


def _text_validation_error(name: str, value: str, *, allow_empty: bool) -> str | None:
    if not allow_empty and not value:
        return f"{name} must not be empty."
    if "\x00" in value:
        return f"{name} must not contain NUL characters."
    return None


def _positive_limit_error(name: str, value: int, maximum: int) -> str | None:
    if value <= 0:
        return f"{name} must be > 0."
    if value > maximum:
        return f"{name}={value} exceeds max {maximum}."
    return None


def _truncate_output(text: str, limit: int = MAX_RUN_OUTPUT_BYTES) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False

    marker = _TRUNCATION_MARKER.encode("utf-8")
    available = max(0, limit - len(marker))
    head_size = available // 2
    tail_size = available - head_size
    head = raw[:head_size].decode("utf-8", errors="ignore")
    tail = raw[-tail_size:].decode("utf-8", errors="ignore") if tail_size else ""
    return head + _TRUNCATION_MARKER + tail, True


def _command_failure(operation: str, result, *, timed_out: bool) -> dict:
    partial_stdout, _ = _truncate_output(result.stdout)
    details = {"partial_stdout": partial_stdout}
    if not timed_out:
        details["exit_code"] = result.exit_code
    if result.pane_recovered is not None:
        details["pane_recovered"] = result.pane_recovered
    suffix = "timed out" if timed_out else "failed"
    return _err(f"{operation} {suffix}", **details)


@tool(
    mcp,
    read_only=False,
    destructive=False,
    idempotent=False,
    open_world=True,
)
async def remote_connect(
    host: str,
    project_path: Optional[str] = None,
    label: Optional[str] = None,
    agent_forwarding: bool = True,
    ssh_add_paths: Optional[list[str]] = None,
) -> ConnectResponse:
    """Open a persistent SSH connection for a host and optional project path."""
    try:
        conn = await sessions.connect(
            host=host,
            project_path=project_path,
            label=label,
            agent_forwarding=agent_forwarding,
            ssh_add_paths=ssh_add_paths,
        )
    except SessionError as exc:
        return _exception_response(exc)
    return _ok(
        connection_id=conn.connection_id,
        host=conn.host,
        project_path=conn.project_path,
        cwd=conn.cwd,
        cwd_warning=conn.cwd_warning,
        agent_warning=conn.agent_warning,
        agent_forwarding=conn.agent_forwarding,
        ssh_add_paths=conn.ssh_add_paths,
        ssh_add_exit_code=conn.ssh_add_exit_code,
        ssh_add_output=conn.ssh_add_output,
        forwarded_agent_present=conn.forwarded_agent_present,
        session_name=conn.session_name,
        label=conn.label,
        attach_hint=f"tmux attach -t {conn.session_name}",
    )


@tool(
    mcp,
    read_only=False,
    destructive=True,
    idempotent=True,
    open_world=False,
)
async def remote_disconnect(connection_id: str) -> DisconnectResponse:
    """Close an active remote connection."""
    try:
        info = await sessions.disconnect(connection_id)
    except SessionError as exc:
        return _exception_response(exc)
    return _ok(**info)


@tool(
    mcp,
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def remote_status() -> StatusResponse:
    """List active remote connections."""
    return _ok(connections=sessions.list_connections())


@tool(
    mcp,
    read_only=False,
    destructive=True,
    idempotent=False,
    open_world=True,
)
async def remote_run(connection_id: str, cmd: str, timeout: int = 60) -> RunResponse:
    """Run commands in the persistent remote shell while preserving cwd and
    environment state across calls."""
    if not cmd.strip():
        return _err("remote_run rejects empty commands.")
    if "\x00" in cmd:
        return _err("remote_run rejects commands containing NUL characters.")
    if error := _positive_limit_error("timeout", timeout, MAX_RUN_TIMEOUT_SECONDS):
        return _err(error)

    try:
        conn = sessions.get(connection_id)
    except SessionError as exc:
        return _exception_response(exc)

    try:
        result = await sessions.run_command(conn, cmd, timeout=float(timeout))
    except (SessionError, ValueError) as exc:
        return _exception_response(exc)

    stdout, truncated = _truncate_output(result.stdout)

    response = _ok(
        stdout=stdout,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        timed_out=result.timed_out,
        truncated=truncated,
    )
    if result.pane_recovered is not None:
        response["pane_recovered"] = result.pane_recovered
    return response


@tool(
    mcp,
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=True,
)
async def remote_read(
    connection_id: str,
    path: str,
    offset: int = 0,
    limit: int = MAX_READ_BYTES,
) -> ReadResponse:
    """Read up to 1 MB from a remote file. Use `offset`/`limit` for chunks."""
    if error := _text_validation_error("path", path, allow_empty=False):
        return _err(error)
    if offset < 0:
        return _err("offset must be >= 0.")
    if error := _positive_limit_error("limit", limit, MAX_READ_BYTES):
        if limit > MAX_READ_BYTES:
            return _err(
                f"limit={limit} exceeds max {MAX_READ_BYTES}; read larger files in "
                "pieces using offset/limit."
            )
        return _err(error)

    try:
        conn = sessions.get(connection_id)
    except SessionError as exc:
        return _exception_response(exc)

    try:
        async with sessions.operation(conn, "read", recover_on_failure=True):
            data, total = await read_remote_file(
                conn.pane_id, path, offset=offset, limit=limit
            )
    except (FileOpError, SessionError) as exc:
        return _exception_response(exc)

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
        return _ok(
            content=text,
            byte_size=len(data),
            total_size=total,
            offset=offset,
            encoding_warning=(
                "This chunk contained invalid UTF-8 bytes; content includes "
                "replacement characters. The file was not modified."
            ),
        )
    return _ok(content=text, byte_size=len(data), total_size=total, offset=offset)


@tool(
    mcp,
    read_only=False,
    destructive=True,
    idempotent=False,
    open_world=True,
)
async def remote_write(connection_id: str, path: str, content: str) -> WriteResponse:
    """Verify and atomically write UTF-8 text, creating parent directories."""
    if error := _text_validation_error("path", path, allow_empty=False):
        return _err(error)

    try:
        conn = sessions.get(connection_id)
    except SessionError as exc:
        return _exception_response(exc)

    try:
        async with sessions.operation(conn, "write", recover_on_failure=True):
            n = await write_remote_file(conn.pane_id, path, content.encode("utf-8"))
    except (FileOpError, SessionError) as exc:
        return _exception_response(exc)

    return _ok(path=path, bytes_written=n)


@tool(
    mcp,
    read_only=False,
    destructive=True,
    idempotent=False,
    open_world=True,
)
async def remote_edit(
    connection_id: str,
    path: str,
    old: str,
    new: str,
    replace_all: bool = False,
) -> EditResponse:
    """Replace exact text in a remote UTF-8 file; `old` must be unique unless
    `replace_all=true`."""
    if error := _text_validation_error("path", path, allow_empty=False):
        return _err(error)
    if old == "":
        return _err("old must not be empty.")
    if old == new:
        return _err("old and new are identical — nothing to do.")

    try:
        conn = sessions.get(connection_id)
    except SessionError as exc:
        return _exception_response(exc)

    try:
        async with sessions.operation(conn, "edit", recover_on_failure=True):
            res = await edit_remote_file(
                conn.pane_id, path, old=old, new=new, replace_all=replace_all
            )
    except (FileOpError, SessionError) as exc:
        return _exception_response(exc)

    return _ok(
        path=res.path,
        occurrences_replaced=res.occurrences_replaced,
        bytes_after=res.bytes_after,
    )


@tool(
    mcp,
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=True,
)
async def remote_grep(
    connection_id: str,
    pattern: str,
    path: str = ".",
    glob: Optional[str] = None,
    case_insensitive: bool = False,
    max_results: int = 200,
) -> GrepResponse:
    """Search remote files for a text pattern and return matching lines."""
    for name, value, allow_empty in (
        ("pattern", pattern, True),
        ("path", path, False),
    ):
        if error := _text_validation_error(name, value, allow_empty=allow_empty):
            return _err(error)
    if glob is not None:
        if error := _text_validation_error("glob", glob, allow_empty=True):
            return _err(error)
    if error := _positive_limit_error("max_results", max_results, MAX_SEARCH_RESULTS):
        return _err(error)

    try:
        conn = sessions.get(connection_id)
    except SessionError as exc:
        return _exception_response(exc)

    cmd = grep_command(
        pattern,
        path,
        glob=glob or None,
        case_insensitive=case_insensitive,
        limit=max_results + 1,
    )

    try:
        result = await sessions.run_command(
            conn, cmd, timeout=120, operation_name="grep"
        )
    except SessionError as exc:
        return _exception_response(exc)

    if result.timed_out:
        return _command_failure("grep", result, timed_out=True)

    if result.exit_code != 0:
        return _command_failure("grep", result, timed_out=False)

    matches = [line for line in result.stdout.splitlines() if line.strip()]
    return _ok(
        matches=matches[:max_results],
        count=min(len(matches), max_results),
        truncated=len(matches) > max_results,
    )


@tool(
    mcp,
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=True,
)
async def remote_glob(
    connection_id: str,
    pattern: str,
    path: str = ".",
    max_results: int = 500,
) -> GlobResponse:
    """List remote files matching a shell glob."""
    for name, value in (("pattern", pattern), ("path", path)):
        if error := _text_validation_error(name, value, allow_empty=False):
            return _err(error)
    if error := _positive_limit_error("max_results", max_results, MAX_SEARCH_RESULTS):
        return _err(error)

    try:
        conn = sessions.get(connection_id)
    except SessionError as exc:
        return _exception_response(exc)

    cmd = glob_command(pattern, path, limit=max_results + 1)

    try:
        result = await sessions.run_command(
            conn, cmd, timeout=60, operation_name="glob"
        )
    except SessionError as exc:
        return _exception_response(exc)

    if result.timed_out:
        return _command_failure("glob", result, timed_out=True)

    if result.exit_code != 0:
        return _command_failure("glob", result, timed_out=False)

    files = [line for line in result.stdout.splitlines() if line.strip()]
    return _ok(
        files=files[:max_results],
        count=min(len(files), max_results),
        truncated=len(files) > max_results,
    )


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Entry point for the `remote-ssh-mcp` console script."""
    parser = argparse.ArgumentParser(prog="remote-ssh-mcp")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {distribution_version('remote-ssh-mcp')}",
    )
    parser.parse_args(argv)
    mcp.run()


if __name__ == "__main__":
    main()
