---
name: remote-server
description: Compatibility alias for the remote-ssh skill. Switches an MCP-capable coding agent into remote-host mode through the remote-ssh-mcp tools.
---

# remote-server

This is a compatibility alias for `remote-ssh`.

Follow the instructions in `../remote-ssh/SKILL.md`: connect with
`remote_connect(host=<host>, project_path=<path>)`, use the returned
`connection_id` for all `remote_*` tools, surface `cwd_warning` verbatim, and
report any non-null `agent_warning` as a warning about forwarded-agent
operations.
