---
name: remote-server
description: Switch into "work-on-a-remote-host" mode for the rest of the session. Routes all execution and file ops through a persistent tmux+SSH session on the named host so the user can `tmux attach` and watch in real time. Invoke when the user wants you to work on a remote machine (`/remote-server <host>`, "let's work on the remote server", "do this on <host>"). Requires the remote-ssh-mcp MCP server.
---

# remote-server

You are now operating on a **remote host** for the rest of this session (or until the user tells you to switch back). Your local tools (`Bash`, `Read`, `Edit`, `Write`, `Grep`, `Glob`) are **off-limits for project work**. Use the `remote_*` MCP tools instead.

## Step 1 — Connect

If the user hasn't supplied a host, ask for it (the alias from their `~/.ssh/config`).

If the user hasn't supplied a project path, **ask** before doing anything else:

> "Which directory on `<host>` do you want to work in?"

Then call `remote_connect(host=<host>, project_path=<path>)`.

- Save the returned `connection_id`. **Every** subsequent `remote_*` call needs it.
- If the call returns `{ok: false, error: ...}`, surface the error verbatim. Don't retry blindly. Most errors are SSH config issues the user must fix locally.
- **Check `cwd_warning` in the response.** If non-null, the `cd` into `project_path` failed and the shell is in $HOME, not where the user asked. **Stop, paste the warning verbatim, and ask the user for the correct path** before doing anything else. Pressing on silently makes `uv run`, relative paths, and project-scoped configs all behave wrong (we've burned hours on this).
- Tell the user: `"Connected to <host> at <cwd>. You can watch with: tmux attach -t <session_name>"`.

## Step 2 — Use only remote_* tools for the project

| If you would normally use… | Use instead |
|---|---|
| `Bash` | `remote_run(connection_id, cmd, timeout?)` |
| `Read` | `remote_read(connection_id, path, offset?, limit?)` |
| `Write` | `remote_write(connection_id, path, content)` |
| `Edit` | `remote_edit(connection_id, path, old, new, replace_all?)` |
| `Grep` | `remote_grep(connection_id, pattern, path?, glob?)` |
| `Glob` | `remote_glob(connection_id, pattern, path?)` |

`remote_edit` has the same exact-string semantics as your local `Edit` — provide enough surrounding context for `old` to be unique, or pass `replace_all=true`.

`remote_run` keeps shell state across calls: `cd`, `export`, `source venv/bin/activate`, `conda activate` all stick. Don't re-`cd` every call.

**`remote_run` is single-line only.** Multi-line scripts and heredocs are rejected with an error pointing here. For multi-line content:
- **Compound on one line:** chain with `;` or `&&`.
- **Real scripts:** `remote_write` the script to a file, then `remote_run` to execute it.
- **Heredocs:** same — write the document body via `remote_write` to a temp file, then redirect (`cmd < /tmp/foo`) or pipe (`cat /tmp/foo | cmd`).

Don't try to "outsmart" this — the runner sends commands via tmux's paste-buffer which converts `\n` to CR mid-paste, and a multi-line command will wedge the shell at the `>` secondary prompt, requiring `remote_disconnect` to recover.

You can still use `Read`/`Edit`/`Write` against **local** files (e.g. local notes, local git in another repo). The rule is: anything inside the remote project, go through `remote_*`.

## Step 3 — Subagents

If you spawn a subagent (`Agent` tool), the subagent has its **own** connection, not yours. In every Agent prompt, include:

> "You are working on remote host **`<host>`**. Call `remote_connect(host='<host>', project_path='<path>', label='<task-name>')` at the start of your task and use the returned `connection_id` for **all** filesystem and execution work via `remote_*` tools. Do **not** use local `Bash`/`Read`/`Edit`/`Write`/`Grep`/`Glob` for the project. Call `remote_disconnect` when you finish."

This gives the subagent its own tmux window — visible to the user as a separate entry under `Ctrl-b w` when attached. Parallel subagents don't race on the same shell.

## Step 4 — Disconnect when done

When the session ends, or when the user says "we're done with the remote", call `remote_disconnect(connection_id)`. The tmux window closes; the per-host session is torn down automatically when its last window closes.

## Failure modes to surface clearly

If `remote_connect` returns an error, **paste it to the user verbatim** instead of paraphrasing. The error text is already actionable (it names the likely cause: missing `~/.ssh/config` entry, no SSH key loaded, agent forwarding disabled, etc.). Don't retry until the user has fixed the underlying issue.

If `remote_run` returns `timed_out: true`, the command is still running on the remote. Either bump the `timeout` and call again (it'll see the END sentinel once the command finishes) or send `Ctrl-C` via `remote_run(connection_id, "")` plus a manual interrupt — easier path is to ask the user.

## Quick example flow

```
User: /remote-server myserver /home/me/myproject

You → remote_connect(host="myserver", project_path="/home/me/myproject")
   ← {connection_id: "ab12cd", session_name: "remote-ssh-mcp/myserver", ...}

You → "Connected. tmux attach -t remote-ssh-mcp/myserver to watch."

User: list the python files

You → remote_glob(connection_id="ab12cd", pattern="*.py")
   ← {files: ["./train.py", "./model.py", ...]}
```
