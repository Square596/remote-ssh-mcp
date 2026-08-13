# Release checklist

1. Choose a semantic version and update it in `pyproject.toml` and both plugin
   manifests under `plugins/remote-ssh-mcp/`.
2. Add the release notes to `CHANGELOG.md` and refresh `uv.lock`.
3. Run the formatter, linter, unit suite, real tmux suite, MCP SDK compatibility
   matrix, wheel smoke test, and plugin validation.
4. Open a focused pull request, complete the independent review, and wait for
   all required checks to pass before merging.
5. Tag the merge commit as `v<version>` and create a GitHub release using the
   matching changelog entry.

The bundled plugin installs from GitHub on client startup, so verify the merged
revision before tagging. Never include local host aliases, paths, credentials,
or environment-specific test data in release notes or repository metadata.
