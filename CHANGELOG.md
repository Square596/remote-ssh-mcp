# Changelog

Notable changes to remote-ssh-mcp are recorded here.

## 0.4.0 - 2026-08-13

- Add stable object-shaped response schemas and accurate tool annotations on
  MCP SDK versions that support them.
- Make MCP SDK 2.x the bundled default while retaining compatibility with MCP
  SDK 1.2 and later 1.x releases.
- Report search failures and distinguish exact result limits from truncated
  results.
- Expand Python, SDK, wheel, tmux, and plugin validation in CI.

## 0.3.6 - 2026-08-13

- Verify remote writes before atomic replacement, preserve modes and symlink
  behavior, and return detailed diagnostics only on failure.

## 0.3.5 - 2026-08-13

- Harden pane framing, lifecycle state, timeout recovery, and tmux integration
  coverage across supported shells.
