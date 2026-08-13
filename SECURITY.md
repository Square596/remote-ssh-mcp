# Security policy

## Supported versions

Security fixes are made on the latest release and the `main` branch. Upgrade
before reporting an issue that only affects an older release.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting flow under the repository's
Security tab. Do not open a public issue with credentials, host details, proof
of concept payloads, or other sensitive information.

Include the affected version, impact, reproduction steps, and any suggested
mitigation.

## Trust boundary

remote-ssh-mcp executes agent-supplied commands on SSH hosts configured by the
user. Treat access to the MCP server as equivalent to access to those remote
shells. Keep SSH keys in an agent, review forwarding policy, and avoid exposing
the stdio server to untrusted clients.
