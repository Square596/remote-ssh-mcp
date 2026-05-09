---
name: remote-ssh
description: Work on a remote SSH host through remote-ssh-mcp. Use when the user asks an agent to inspect, edit, test, or run commands on a remote server or SSH host. Routes remote project work through persistent, watchable tmux+SSH sessions.
---

# remote-ssh

You are operating on a remote host until the user switches context. Use
`remote_*` tools for remote project filesystem and execution work.

## Connect

If host or project path is missing, ask for it. The host should be the alias
that works with `ssh <host>`.

Call:

```
remote_connect(host="<host>", project_path="<path>", label="<task>")
```

Track `host`, `project_path`, `connection_id`, `cwd`, and `session_name`.
If connection fails, paste the error verbatim. On success, tell the user the
host/cwd and `tmux attach -t <session_name>` command.

Report SSH agent status briefly from `forwarded_agent_present`. If
`agent_warning` is present, include it. If `cwd_warning` is present, stop,
paste it verbatim, and ask for the correct project path before doing work.

## Work

| Local action | Use instead |
|---|---|
| Run a shell command | `remote_run(connection_id, cmd, timeout?)` |
| Read a file | `remote_read(connection_id, path, offset?, limit?)` |
| Write a file | `remote_write(connection_id, path, content)` |
| Edit a file | `remote_edit(connection_id, path, old, new, replace_all?)` |
| Search text | `remote_grep(connection_id, pattern, path?, glob?)` |
| Find files | `remote_glob(connection_id, pattern, path?)` |

`remote_run` keeps cwd/env state and accepts one shell line only. Use
`remote_write` to create scripts or heredoc bodies, then run them with
`remote_run`. `remote_edit` is exact-string replacement; make `old` unique or
use `replace_all=true`.

Local shell tools are only for local-side work, such as `scp`/`rsync` config
transfer. Do not use local filesystem or shell tools to inspect or edit the
remote project itself.

## Multi-Host And Subagents

Keep a separate connection note per host/project. Before each remote tool call,
choose the matching `connection_id`.

Every subagent must call `remote_connect` with the same host/project and its
own `label`, use the returned `connection_id`, and call `remote_disconnect`
when finished.

For transferring remote agent configs, rules, hooks, or skills to a local
directory, use the `remote-agent-config-sync` skill.

## Finish

When the session ends, or when the user says the remote work is done, call
`remote_disconnect(connection_id)`. If `remote_run` times out, the command may
still be running; rerun with a longer timeout only when that is clearly safe.
