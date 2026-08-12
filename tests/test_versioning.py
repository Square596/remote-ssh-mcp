from __future__ import annotations

import json
import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[1]
MANIFESTS = (
    ROOT / "plugins/remote-ssh-mcp/.claude-plugin/plugin.json",
    ROOT / "plugins/remote-ssh-mcp/.codex-plugin/plugin.json",
)


def test_package_lock_and_plugin_versions_match() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    expected = project["project"]["version"]
    locked_package = next(
        package
        for package in lock["package"]
        if package["name"] == project["project"]["name"]
    )

    assert locked_package["version"] == expected
    for manifest_path in MANIFESTS:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["version"] == expected
