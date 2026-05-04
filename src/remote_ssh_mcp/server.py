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
    require_agent_forwarding: bool = False,
) -> dict:
    """Open a new tmux window on the per-host session, ssh -A into `host`, and
    optionally `cd` into `project_path`. Returns a `connection_id` to pass to all
    subsequent remote_* calls. Each call to remote_connect creates a fresh
    window — parent and subagents should each call remote_connect for isolation.

    The response includes `cwd` (the actual current directory after the cd
    attempt) and `cwd_warning` (non-null if `project_path` was provided but the
    cd failed — the shell will be in $HOME or whatever default the login shell
    sets). When cwd_warning is set, surface it to the user verbatim and stop —
    don't proceed silently from $HOME.

    By default, SSH config authentication is enough to connect. If
    `require_agent_forwarding` is true, the connection also requires a reachable
    ssh-agent with loaded keys. The response includes `agent_warning` when the
    host is reachable but forwarded-agent operations may fail.
    """
    try:
        conn = await sessions.connect(
            host=host,
            project_path=project_path,
            label=label,
            require_agent_forwarding=require_agent_forwarding,
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
        session_name=conn.session_name,
        label=conn.label,
        attach_hint=f"tmux attach -t {conn.session_name}",
    )


@mcp.tool()
async def remote_disconnect(connection_id: str) -> dict:
    """Close the tmux window for this connection. If it was the last window in
    the per-host session, the session itself is torn down."""
    info = await sessions.disconnect(connection_id)
    return _ok(**info)


@mcp.tool()
async def remote_status() -> dict:
    """List all active connections (across all hosts)."""
    return _ok(connections=sessions.list_connections())


@mcp.tool()
async def remote_run(connection_id: str, cmd: str, timeout: int = 60) -> dict:
    """Run a SINGLE-LINE shell command in the persistent SSH session. Shell
    state (cwd, env, activated venvs) is preserved across calls on the same
    connection_id. Returns {ok, stdout, exit_code, duration_ms, timed_out}.

    Multi-line scripts and heredocs are rejected — the runner sends commands
    through the tmux paste-buffer which converts \\n to CR mid-paste, breaking
    line continuation. For multi-line content, write a script with remote_write
    then execute it with remote_run. For compound statements use ';' or '&&'
    on a single line.
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

    try:
        conn = sessions.get(connection_id)
    except SessionError as e:
        return _err(str(e))

    async with conn.lock:
        result = await run_in_pane(conn.pane_id, cmd, timeout=float(timeout))

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
    """Read a file from the remote host (≤1 MB per call). The read flows through
    the visible tmux pane as a base64 round-trip. Returns the decoded text
    (with replacement chars for invalid UTF-8) plus byte_size and total_size.
    For binary content, use offset/limit to chunk."""
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
    """Write `content` (UTF-8) to `path` atomically. Creates parent dirs if
    missing. Replaces the file if it exists. Use remote_edit for surgical
    in-place edits."""
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
    """Exact-string replacement in a remote file (mirrors Claude's local Edit).
    Errors if `old` is not present, or if it is non-unique and replace_all is
    False. Read-modify-write through the visible tmux pane."""
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
    """Search for `pattern` (regex) under `path`. Uses ripgrep if available on
    the remote, falls back to `grep -rE`. Optional `glob` filters file names
    (e.g. '*.py')."""
    try:
        conn = sessions.get(connection_id)
    except SessionError as e:
        return _err(str(e))

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
        f"rg {rg_args} -m {max_results} {shlex.quote(pattern)} {shlex.quote(path)} "
        f"|| true; "
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
    return _ok(matches=matches, count=len(matches), truncated=len(matches) >= max_results)


@mcp.tool()
async def remote_glob(
    connection_id: str,
    pattern: str,
    path: str = ".",
    max_results: int = 500,
) -> dict:
    """List files matching `pattern` under `path` (uses `find -name`). Pattern
    is a shell glob like '*.py' or 'test_*.json' — not a full path glob."""
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
