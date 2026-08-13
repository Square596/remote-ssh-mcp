# Release checklist

The stable plugin and package use one semantic version. A plugin release must
change the version in `pyproject.toml`, `uv.lock`, both plugin manifests, the
stable source tag and SDK pin in `.mcp.json`, and `CHANGELOG.md`.

## Prepare and review

1. Start a focused release branch from the current default branch.
2. Update every version location and make the stable plugin install
   `git+https://github.com/Square596/remote-ssh-mcp@v<version>` with the exact
   MCP SDK tested for that release. Keep moving-branch installation opt-in.
3. Run formatting, linting, the unit and real-tmux suites, every supported
   Python and MCP SDK job, the wheel smoke test, and both plugin validators.
4. Open a pull request, complete the independent review, and wait for every
   required check to pass. Keep release metadata free of local host aliases,
   paths, credentials, and environment-specific test data.

## Tag and merge

The stable plugin refers to its release tag, so publish the reviewed tag before
merging the manifest that consumes it:

1. Confirm the release branch is current, the intended tag does not exist, and
   the working tree is clean.
2. Create an annotated `v<version>` tag on the reviewed pull-request head and
   push it. Wait for the tag's CI run to pass.
3. Merge the pull request with a merge commit. Verify the tagged commit is
   reachable from the updated default branch.

Do not retarget or reuse a published version. If anything changes after the
tag is created, discard the unpublished tag and repeat the validation with a
new patch version.

## Build and publish

1. Enable repository release immutability and verify it before publishing.
2. Build the wheel and source distribution from a clean checkout of the tag.
3. Smoke-test the wheel, then generate `SHA256SUMS` for both distributions.
4. Create a draft GitHub release with the distributions and checksum file.
   Release notes must use the matching changelog entry and link the tagged
   source commit, the merged pull request or merge commit, and the relevant
   commit history.
5. Inspect the complete draft, then publish it as the latest release. Published
   immutable tags and assets cannot be replaced, so corrections require a new
   patch release.

## Verify delivery

1. Install from the published tag into isolated uv tool and executable
   directories. Verify the CLI version, MCP initialization, and tool inventory.
2. Start the bundled launcher twice and confirm the second start works offline
   without invoking installation.
3. Refresh each supported plugin marketplace, install the released plugin, and
   verify its manifest and MCP server version.
4. Run the public smoke workflow on representative remote servers. Always
   remove and verify removal of temporary remote files, windows, and processes.
5. Confirm the GitHub release is non-draft, marked latest, immutable, and has
   the expected tag and assets.

Rollback means selecting an older immutable tag manually or publishing a
corrected patch release. Never move an existing release tag or replace a
published asset.
