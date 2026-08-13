from __future__ import annotations

import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "remote-ssh-mcp"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_package_and_plugin_versions_match() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]

    assert _json(PLUGIN / ".codex-plugin" / "plugin.json")["version"] == version
    assert _json(PLUGIN / ".claude-plugin" / "plugin.json")["version"] == version


def test_plugin_references_existing_components() -> None:
    manifest = _json(PLUGIN / ".codex-plugin" / "plugin.json")

    assert (PLUGIN / manifest["skills"]).is_dir()
    assert (PLUGIN / manifest["mcpServers"]).is_file()
    assert list((PLUGIN / "skills").glob("*/SKILL.md"))


def test_skill_frontmatter_has_required_identity() -> None:
    for skill in (PLUGIN / "skills").glob("*/SKILL.md"):
        text = skill.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        frontmatter = text.split("---\n", 2)[1]
        assert "name:" in frontmatter
        assert "description:" in frontmatter


def test_bundled_server_uses_recommended_sdk_line() -> None:
    config = _json(PLUGIN / ".mcp.json")
    server = config["mcpServers"]["remote-ssh"]
    command = " ".join(server["args"])

    assert server["command"] == "sh"
    assert "mcp>=2,<3" in command
    assert "SSH_AUTH_SOCK" in server["env_vars"]
