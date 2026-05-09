from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from remote_ssh_mcp import server


def tool_fn(tool):
    return getattr(tool, "fn", tool)


@pytest.mark.asyncio
async def test_remote_run_rejects_empty_command() -> None:
    result = await tool_fn(server.remote_run)(connection_id="unused", cmd=" \t")

    assert result["ok"] is False
    assert "empty commands" in result["error"]


@pytest.mark.asyncio
async def test_remote_grep_caps_rg_output_with_head(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class FakeSessions:
        def get(self, connection_id):
            assert connection_id == "conn"
            return SimpleNamespace(pane_id="%1", lock=asyncio.Lock())

    async def fake_run_in_pane(pane_id, cmd, timeout=60):
        assert pane_id == "%1"
        captured["cmd"] = cmd
        return SimpleNamespace(
            stdout="a\nb\nc\n",
            timed_out=False,
        )

    monkeypatch.setattr(server, "sessions", FakeSessions())
    monkeypatch.setattr(server, "run_in_pane", fake_run_in_pane)

    result = await tool_fn(server.remote_grep)(
        connection_id="conn",
        pattern="needle",
        path=".",
        max_results=3,
    )

    assert result["ok"] is True
    assert result["count"] == 3
    assert result["truncated"] is True
    assert "| head -n 3" in captured["cmd"]
    assert " -m 3 " not in captured["cmd"]


@pytest.mark.asyncio
async def test_remote_grep_rejects_non_positive_max_results(monkeypatch) -> None:
    class FakeSessions:
        def get(self, connection_id):
            return SimpleNamespace(pane_id="%1", lock=asyncio.Lock())

    monkeypatch.setattr(server, "sessions", FakeSessions())

    result = await tool_fn(server.remote_grep)(
        connection_id="conn",
        pattern="needle",
        max_results=0,
    )

    assert result["ok"] is False
    assert "max_results must be > 0" in result["error"]
