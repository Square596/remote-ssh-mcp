from __future__ import annotations

from pathlib import Path


SKILLS_DIR = Path("plugins/remote-ssh-mcp/skills")


def read_skill(name: str) -> str:
    return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")


def frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n")
    _, raw, _ = text.split("---", 2)
    fields: dict[str, str] = {}
    for line in raw.strip().splitlines():
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def test_bundled_skills_have_expected_frontmatter() -> None:
    remote = frontmatter(read_skill("remote-ssh"))
    sync = frontmatter(read_skill("remote-agent-config-sync"))

    assert remote["name"] == "remote-ssh"
    assert "remote SSH host" in remote["description"]
    assert sync["name"] == "remote-agent-config-sync"
    assert "adapt only the local copies" in sync["description"]


def test_remote_skill_stays_concise() -> None:
    text = read_skill("remote-ssh")

    assert len(text) < 3500
    assert "remote-agent-config-sync" in text


def test_config_sync_skill_never_modifies_remote_files() -> None:
    text = read_skill("remote-agent-config-sync")

    assert "Never\nmodify the remote files" in text
    assert "Edit only local files" in text
    assert "remote files were not modified" in text
    assert "rsync" in text
    assert "scp -rp" in text
