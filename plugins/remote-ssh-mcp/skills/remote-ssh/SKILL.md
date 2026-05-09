---
name: remote-ssh
description: Switch an MCP-capable coding agent into remote-host mode. Routes execution and file operations through a persistent tmux+SSH session on the named host so the user can `tmux attach` and watch in real time. Invoke when the user wants the agent to work on a remote machine (`remote_connect <host>`, "work on the remote server", "do this on <host>"). Requires the remote-ssh-mcp MCP server.
---

# remote-ssh

You are now operating on a **remote host** for the rest of this session, or
until the user tells you to switch back. Use the `remote_*` MCP tools for all
project filesystem and execution work on that host.

## Step 1 - Connect

If the user hasn't supplied a host, ask for it. The host should be the alias
from their `~/.ssh/config`.

If the user hasn't supplied a project path, ask before doing anything else:

> "Which directory on `<host>` do you want to work in?"

Then call `remote_connect(host=<host>, project_path=<path>)`.

- Save the returned `connection_id`. Every subsequent `remote_*` call needs it.
- If the call returns `{ok: false, error: ...}`, surface the error verbatim. Do
  not retry blindly. Most errors are SSH config, authentication, or network
  issues the user must fix locally.
- Tell the user: `"Connected to <host> at <cwd>. You can watch with: tmux attach -t <session_name>"`.
- In the same message, always report SSH agent status from the structured
  fields:
  - `forwarded_agent_present=true`: "SSH agent: present on the remote."
  - `forwarded_agent_present=false`: "SSH agent: not present or not accessible
    from the remote."
  - `forwarded_agent_present=null`: "SSH agent: forwarding disabled; not
    checked."
- Also report the key-path arguments passed to local `ssh-add`:
  - If `agent_forwarding=false`: "SSH key paths added: none; ssh-add was not
    run."
  - If `ssh_add_paths=[]`: "SSH key paths added: none; used bare ssh-add."
  - Otherwise list only the path strings from `ssh_add_paths`. Do not list
    fingerprints, key comments, or raw `ssh-add -l` output.
- If `ssh_add_exit_code` is non-zero, use `ssh_add_output` to summarize that
  some requested key paths may be incorrect or failed to add. Tell the user to
  check those paths and reconnect with corrected paths if those keys are
  needed. Do not paste raw `ssh_add_output` unless it contains only path-based
  errors.
- If `agent_warning` is non-null, include it as the warning detail. Continue
  unless the user specifically needs remote commands to use their forwarded
  local SSH agent.
- If `cwd_warning` is non-null, the `cd` into `project_path` failed and the
  shell is in `$HOME`, not where the user asked. Stop after the connection and
  agent-status message, paste the warning verbatim, and ask the user for the
  correct path before doing anything else.

## Step 2 - Use only remote_* tools for the remote project

| Local action | Use instead |
|---|---|
| Run a shell command | `remote_run(connection_id, cmd, timeout?)` |
| Read a file | `remote_read(connection_id, path, offset?, limit?)` |
| Write a file | `remote_write(connection_id, path, content)` |
| Edit a file | `remote_edit(connection_id, path, old, new, replace_all?)` |
| Search text | `remote_grep(connection_id, pattern, path?, glob?)` |
| Find files | `remote_glob(connection_id, pattern, path?)` |

`remote_edit` has exact-string semantics: provide enough surrounding context
for `old` to be unique, or pass `replace_all=true`.

`remote_run` keeps shell state across calls: `cd`, `export`, `source
venv/bin/activate`, and `conda activate` all stick. Do not re-`cd` every call.

`remote_run` is single-line only. Multi-line scripts and heredocs are rejected.
For multi-line content:

- Compound on one line with `;` or `&&`.
- For scripts, write the script with `remote_write`, then execute it with
  `remote_run`.
- For heredocs, write the document body with `remote_write`, then redirect or
  pipe it from a remote file.

## Step 3 - Parallel agents

If you delegate to another agent, give it its own connection. In every delegated
prompt, include:

> "You are working on remote host **`<host>`**. Call `remote_connect(host='<host>', project_path='<path>', label='<task-name>')` at the start of your task and use the returned `connection_id` for **all** filesystem and execution work via `remote_*` tools. Do not use local filesystem or shell tools for the remote project. Call `remote_disconnect` when you finish."

This creates a separate tmux window for that agent. Parallel agents do not race
on the same shell.

## Step 4 - Disconnect when done

When the session ends, or when the user says the remote work is done, call
`remote_disconnect(connection_id)`. The tmux window closes; the per-host
session is torn down automatically when its last window closes.

## Failure modes

If `remote_connect` returns an error, paste it to the user verbatim instead of
paraphrasing.

If `remote_run` returns `timed_out: true`, the command may still be running on
the remote. Either rerun with a longer timeout when appropriate, or ask the
user how to handle the stuck remote command.

## Quick example

```
User: work on myserver in /home/me/myproject

You -> remote_connect(host="myserver", project_path="/home/me/myproject")
   <- {connection_id: "ab12cd", session_name: "remote-ssh-mcp/myserver", ...}

You -> "Connected. tmux attach -t remote-ssh-mcp/myserver to watch."

User: list the python files

You -> remote_glob(connection_id="ab12cd", pattern="*.py")
   <- {files: ["./train.py", "./model.py", ...]}
```
