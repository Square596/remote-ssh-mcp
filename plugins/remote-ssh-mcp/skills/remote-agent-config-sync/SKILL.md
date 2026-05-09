---
name: remote-agent-config-sync
description: Copy agent configuration, rules, hooks, and skills from a remote SSH project to a local directory, then adapt only the local copies for remote-ssh-mcp usage. Use when the user asks to import, sync, migrate, or adapt remote agent configs.
---

# remote-agent-config-sync

Copy remote agent instructions to a local directory, then adapt the local files
so agents working on that remote project use `remote_*` tools correctly. Never
modify the remote files during this workflow.

## Inputs

Ask for any missing value:

- `host`: SSH alias that works with `ssh <host>`.
- `remote_project_path`: project directory on the remote host.
- `local_destination`: local directory where configs should be copied.

If `local_destination` already exists and is non-empty, ask before overwriting
or merging.

## Discover

Prefer `remote_connect` plus `remote_glob`/`remote_run` to inspect candidate
paths under `remote_project_path`. Look for:

- `AGENTS.md`, `CLAUDE.md`, `.cursorrules`
- `.agents/**`, `.codex/**`, `.claude/**`, `.cursor/rules/**`
- `skills/**`, `hooks/**`, files mentioning agent rules or tool use

Ignore dependency directories, build outputs, virtualenvs, and VCS internals.

## Copy

Use the local shell for transfer because this is local-side file movement, not
remote project editing. Prefer `rsync` when available; otherwise use `scp -rp`.

Examples:

```bash
rsync -av --relative <host>:/abs/remote/path/./AGENTS.md <local_destination>/
scp -rp <host>:/abs/remote/path/.agents <local_destination>/
```

Do not create separate raw/adapted trees by default. The copied files in
`local_destination` are the working local copies.

## Adapt Local Copies

Read the copied local files. Edit only local files, and only when the change is
clearly needed for remote-ssh-mcp use.

Adapt instructions that tell agents to use local tools for remote project work:

- local shell/file reads -> `remote_run`, `remote_read`, `remote_grep`,
  `remote_glob`
- local writes/patches -> `remote_write` or `remote_edit`
- generic "work in this repo" instructions -> mention the remote host/project
  context when needed

Ask before changing hooks, install scripts, absolute paths, secrets, credentials,
destructive commands, or unclear tool-specific behavior. In the final response,
list copied paths, adapted local paths, skipped ambiguous files, and confirm that
the remote files were not modified.
