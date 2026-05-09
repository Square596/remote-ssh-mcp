"""MCP server entry point. Registers the remote_* tools and dispatches to the
SessionManager / runner / files modules."""

from __future__ import annotations

import shlex
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .files import (
    MAX_READ_BYTES,
    FileOpError,
    edit_remote_file,
    read_remote_file,
    write_remote_file,
)
from .runner import run_in_pane
from .session import SessionError, SessionManager

mcp = FastMCP("remote-ssh-mcp")
sessions = SessionManager()


def _err(message: str, **extra) -> dict:
    return {"ok": False, "error": message, **extra}


def _ok(**fields) -> dict:
    return {"ok": True, **fields}


@mcp.tool()
async def remote_connect(
    host: str,
    project_path: Optional[str] = None,
    label: Optional[str] = None,
    agent_forwarding: bool = True,
    ssh_add_paths: Optional[list[str]] = None,
) -> dict:
    """Open a fresh tmux+SSH window for a host and optional project path.
    Returns a `connection_id`, cwd, attach hint, and SSH agent status. If
    `cwd_warning` is set, stop and ask for the correct path before working.
    """
    try:
        conn = await sessions.connect(
            host=host,
            project_path=project_path,
            label=label,
            agent_forwarding=agent_forwarding,
            ssh_add_paths=ssh_add_paths,
        )
    except SessionError as e:
        return _err(str(e))
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


@mcp.tool()
async def remote_disconnect(connection_id: str) -> dict:
    """Close this connection's tmux window."""
    info = await sessions.disconnect(connection_id)
    return _ok(**info)


@mcp.tool()
async def remote_status() -> dict:
    """List active remote connections."""
    return _ok(connections=sessions.list_connections())


@mcp.tool()
async def remote_run(connection_id: str, cmd: str, timeout: int = 60) -> dict:
    """Run one shell line in the persistent remote session. Cwd/env state is
    preserved. For scripts or heredocs, write a file first with `remote_write`.
    """
    if "\n" in cmd or "\r" in cmd:
        return _err(
            "remote_run rejects multi-line commands. The tmux paste-buffer "
            "converts newlines to CR mid-paste, which corrupts heredocs and "
            "command continuation — the shell will wedge at the `>` "
            "secondary prompt and require remote_disconnect to recover.\n\n"
            "Workarounds:\n"
            "  - Compound on one line:  cmd1 && cmd2 && cmd3\n"
            "  - For multi-line scripts: remote_write the script to a file, "
            "then remote_run to execute it.\n"
            "  - For heredocs: same — write the document body via remote_write."
        )
    if not cmd.strip():
        return _err("remote_run rejects empty commands.")

    try:
        conn = sessions.get(connection_id)
    except SessionError as e:
        return _err(str(e))

    async with conn.lock:
        try:
            result = await run_in_pane(conn.pane_id, cmd, timeout=float(timeout))
        except ValueError as e:
            return _err(str(e))

    return _ok(
        stdout=result.stdout,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        timed_out=result.timed_out,
    )


@mcp.tool()
async def remote_read(
    connection_id: str,
    path: str,
    offset: int = 0,
    limit: int = MAX_READ_BYTES,
) -> dict:
    """Read up to 1 MB from a remote file. Use `offset`/`limit` for chunks."""
    try:
        conn = sessions.get(connection_id)
    except SessionError as e:
        return _err(str(e))

    async with conn.lock:
        try:
            data, total = await read_remote_file(
                conn.pane_id, path, offset=offset, limit=limit
            )
        except FileOpError as e:
            return _err(str(e))

    text = data.decode("utf-8", errors="replace")
    return _ok(content=text, byte_size=len(data), total_size=total, offset=offset)


@mcp.tool()
async def remote_write(connection_id: str, path: str, content: str) -> dict:
    """Atomically write UTF-8 text to a remote path, creating parents."""
    try:
        conn = sessions.get(connection_id)
    except SessionError as e:
        return _err(str(e))

    async with conn.lock:
        try:
            n = await write_remote_file(conn.pane_id, path, content.encode("utf-8"))
        except FileOpError as e:
            return _err(str(e))

    return _ok(path=path, bytes_written=n)


@mcp.tool()
async def remote_edit(
    connection_id: str,
    path: str,
    old: str,
    new: str,
    replace_all: bool = False,
) -> dict:
    """Replace exact text in a remote UTF-8 file; `old` must be unique unless
    `replace_all=true`."""
    try:
        conn = sessions.get(connection_id)
    except SessionError as e:
        return _err(str(e))

    async with conn.lock:
        try:
            res = await edit_remote_file(
                conn.pane_id, path, old=old, new=new, replace_all=replace_all
            )
        except FileOpError as e:
            return _err(str(e))

    return _ok(
        path=res.path,
        occurrences_replaced=res.occurrences_replaced,
        bytes_after=res.bytes_after,
    )


@mcp.tool()
async def remote_grep(
    connection_id: str,
    pattern: str,
    path: str = ".",
    glob: Optional[str] = None,
    case_insensitive: bool = False,
    max_results: int = 200,
) -> dict:
    """Search remote files with ripgrep or grep fallback."""
    try:
        conn = sessions.get(connection_id)
    except SessionError as e:
        return _err(str(e))
    if max_results <= 0:
        return _err("max_results must be > 0")

    flags = ["-n", "--color=never"]
    if case_insensitive:
        flags.append("-i")
    if glob:
        flags += ["-g", glob]

    rg_args = " ".join(shlex.quote(f) for f in flags)
    grep_glob = f"--include={shlex.quote(glob)}" if glob else ""
    grep_case = "i" if case_insensitive else ""

    cmd = (
        f"if command -v rg >/dev/null 2>&1; then "
        f"rg {rg_args} {shlex.quote(pattern)} {shlex.quote(path)} "
        f"| head -n {max_results} || true; "
        f"else "
        f"grep -rnE{grep_case} {grep_glob} {shlex.quote(pattern)} {shlex.quote(path)} "
        f"| head -n {max_results} || true; "
        f"fi"
    )

    async with conn.lock:
        result = await run_in_pane(conn.pane_id, cmd, timeout=120)

    if result.timed_out:
        return _err("grep timed out", partial_stdout=result.stdout)

    matches = [line for line in result.stdout.splitlines() if line.strip()]
    return _ok(
        matches=matches, count=len(matches), truncated=len(matches) >= max_results
    )


@mcp.tool()
async def remote_glob(
    connection_id: str,
    pattern: str,
    path: str = ".",
    max_results: int = 500,
) -> dict:
    """List remote files matching a `find -name` shell glob."""
    try:
        conn = sessions.get(connection_id)
    except SessionError as e:
        return _err(str(e))

    cmd = (
        f"find {shlex.quote(path)} -type f -name {shlex.quote(pattern)} "
        f"2>/dev/null | head -n {max_results}"
    )

    async with conn.lock:
        result = await run_in_pane(conn.pane_id, cmd, timeout=60)

    if result.timed_out:
        return _err("glob timed out", partial_stdout=result.stdout)

    files = [line for line in result.stdout.splitlines() if line.strip()]
    return _ok(files=files, count=len(files), truncated=len(files) >= max_results)


def main() -> None:
    """Entry point for the `remote-ssh-mcp` console script."""
    mcp.run()


if __name__ == "__main__":
    main()
