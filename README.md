# remote-ssh-mcp

An MCP server that lets coding agents work on a remote host through a
persistent tmux+SSH session. Every tool call (run, read, write, edit, grep,
glob) is routed through the same tmux pane on the remote, so you can
`tmux attach` and watch the agent work in real time.

## Why

Most coding-agent clients operate on the local filesystem and a fresh
non-interactive shell per call. That makes "I want the agent to work on my
remote server" awkward: you end up wrapping every command in `ssh host
'<cmd>'`, losing shell state (cwd, venv, env vars) between calls, and having
no way to watch what's happening.

`remote-ssh-mcp` fixes that by:

- Opening one persistent tmux session per remote host, using `ssh -A` by
  default so forwarded-agent operations are available when your local agent is
  usable, and keeping shell state across calls.
- Giving each agent (parent + subagents) its own tmux **window**, so
  parallel work doesn't race on the same shell.
- Exposing a full toolkit (`remote_run`, `remote_read`, `remote_write`,
  `remote_edit`, `remote_grep`, `remote_glob`, …) that mirrors local agent
  tools — but every operation flows through the visible tmux pane.

You can `tmux attach -t remote-ssh-mcp/<host>` at any time to watch.

## Status

Alpha. v1 ships with single-window-per-connection serialization and a 1MB cap
on single-file reads. See [Limitations](#limitations).

## Install

### Prerequisites

- `tmux` 3.0+ on your laptop.
- An SSH config entry for the remote host (i.e. `ssh <host>` works in a normal
  terminal). Agent forwarding is requested by default via `ssh -A`; pass
  `agent_forwarding=false` to `remote_connect` to disable it.
- `python3` on the **remote** host (used for atomic file writes via base64).
- `uv` or `pipx` on your laptop.

### As a plugin with bundled skills

The plugin bundles the MCP server and skills that brief MCP-capable agents on
how to use it. In Claude Code, install it with:

```
/plugin marketplace add Square596/remote-ssh-mcp
/plugin install remote-ssh-mcp@Square596
```

The MCP server is launched via `uvx`, which downloads and caches the
Python entrypoint on first run — no separate `pip install` step needed.

The same plugin directory includes Codex metadata
(`plugins/remote-ssh-mcp/.codex-plugin/plugin.json`) and a repo-local Codex
marketplace entry (`.agents/plugins/marketplace.json`). Both Claude Code and
Codex use the bundled `.mcp.json` plus the `remote-ssh` skill.

To upgrade later: `uvx --refresh --from git+https://github.com/Square596/remote-ssh-mcp remote-ssh-mcp --help` (any `uvx` invocation with `--refresh` re-pulls).

### As an MCP server (any MCP client)

If you don't want the skill or you're on Codex, Cursor, or another MCP client,
use the stdio server directly:

```json
{
  "mcpServers": {
    "remote-ssh": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/Square596/remote-ssh-mcp",
        "remote-ssh-mcp"
      ]
    }
  }
}
```

You can also install the Python package directly:

```bash
uv tool install git+https://github.com/Square596/remote-ssh-mcp
```

Then add the installed command to your MCP client config:

```json
{
  "mcpServers": {
    "remote-ssh": {
      "command": "remote-ssh-mcp"
    }
  }
}
```

For Claude Code CLI:

```bash
claude mcp add remote-ssh remote-ssh-mcp
```

## Usage

Once installed, ask your agent to connect to a host with the `remote-ssh` skill
or call the tool directly:

```
remote_connect(host="<host>", project_path="/home/me/myproject")
```

`<host>` is whatever alias you use in `~/.ssh/config` — the same string
that works for plain `ssh <host>`.

The skill will:
1. Run local `ssh-add`, then connect via `ssh -A <host>`, opening a fresh tmux window in the
   `remote-ssh-mcp/<host>` session.
2. `cd` into your project path.
3. Tell the agent to use `remote_*` tools for **all** subsequent file/exec
   work, and to brief subagents to do the same.

To watch:

```bash
tmux attach -t remote-ssh-mcp/<host>
```

`Ctrl-b w` lists windows (one per active connection — parent + each subagent).

## Tools

All file/exec tools take a `connection_id` returned by `remote_connect`.

| Tool | Local equivalent | Notes |
|---|---|---|
| `remote_connect(host, project_path?, label?, agent_forwarding?, ssh_add_paths?)` | — | Opens new tmux window. Returns `{connection_id, host, cwd, agent_warning, forwarded_agent_present, ssh_add_paths, ssh_add_exit_code, ssh_add_output}`. |
| `remote_disconnect(connection_id)` | — | Closes window. Closes session if last window. |
| `remote_status()` | — | Lists active connections. |
| `remote_run(connection_id, cmd, timeout?)` | Bash | Persistent shell. Returns `{stdout, exit_code, duration_ms}`. |
| `remote_read(connection_id, path, offset?, limit?)` | Read | Base64 round-trip through tmux. ≤1 MB. |
| `remote_write(connection_id, path, content)` | Write | Atomic via tempfile + rename. |
| `remote_edit(connection_id, path, old, new, replace_all?)` | Edit | Exact match, errors if `old` is non-unique unless `replace_all=true`. |
| `remote_grep(connection_id, pattern, path?, glob?)` | Grep | Uses `rg` if available, else `grep -r`. |
| `remote_glob(connection_id, pattern, path?)` | Glob | Uses `find`. |

## Troubleshooting

**`Couldn't connect to <host>`.** Check in this order:
1. `ssh <host>` works in a regular terminal (host is in `~/.ssh/config`).
2. The host supports non-interactive key-based auth (`BatchMode=yes`).
3. If you need forwarded-agent operations from the remote host, local `ssh-add`
   can load your keys and agent forwarding is allowed.

**`agent_warning` is present.** The SSH connection worked, but the MCP server
could not confirm a usable forwarded ssh-agent. Normal remote commands can
still work through `IdentityFile` or other OpenSSH config. Private git fetches
from the remote that rely on forwarded agent keys may fail. Pass
`agent_forwarding=false` if you do not want the MCP server to run local
`ssh-add`, connect with `ssh -A`, or check forwarded-agent readiness.

**Explicit `ssh_add_paths` partially fail.** `remote_connect` still proceeds if
bulk `ssh-add <paths...>` returns non-zero. The response includes the expanded
`ssh_add_paths`, `ssh_add_exit_code`, and `ssh_add_output` so the agent can tell
you which requested paths may need checking before reconnecting.

**The pane gets noisy with base64 blobs.** Yes — that's the cost of routing
file writes through the visible terminal. The skill prefixes blobs with a
`# remote-ssh-mcp: writing N bytes to <path>` comment so it's at least
labelled.

**Subagents seem to share my window.** The skill instructs subagents to call
`remote_connect` themselves at task start. If they don't, they fall through
to your window. Check the parent's prompt to subagents.

## Limitations

- **One pane per connection, serialized calls.** Parallel calls on the same
  `connection_id` queue. Use separate connections (subagents) for parallelism,
  or `nohup … &` for true background work.
- **Single `remote_read` calls are capped at ~1 MB.** Read larger files in
  chunks with `offset` and `limit`; `remote_edit` chunks internally for UTF-8
  text files.
- **No interactive TUI driving.** Things that need a TTY (vim, less in
  interactive mode, sudo password prompts) won't work cleanly. Use
  non-interactive equivalents.
- **Binary file edits via `remote_edit`** treat content as UTF-8 strings.
  For true binary edits, use `remote_write` with the full new content.

## License

MIT — see [LICENSE](./LICENSE).
