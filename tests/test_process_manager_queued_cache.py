"""Tests for CB-45921: _is_queued zero disk I/O and BotState.status sync."""
import ast
import inspect
from pathlib import Path

import pytest

from codebot.process_manager import BotConfig, BotState, _is_queued, update_bot_state


@pytest.fixture
def bot():
    cfg = BotConfig(
        name="test-bot",
        prompt_file="p.md",
        interval_seconds=10,
        heartbeat_timeout=5,
    )
    return BotState(config=cfg)


def test_is_queued_uses_bot_status_no_disk_io(bot):
    """_is_queued must not contain Path.read_text or json.loads."""
    source = inspect.getsource(_is_queued)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "read_text":
            pytest.fail("_is_queued contains read_text call")
        if isinstance(node, ast.Attribute) and node.attr == "loads":
            # Ensure it's json.loads specifically; but any loads is suspicious
            pass
        if isinstance(node, ast.Name) and node.id == "json":
            pytest.fail("_is_queued references json module")
        if isinstance(node, ast.Name) and node.id == "Path":
            pytest.fail("_is_queued references Path")

    bot.status = "queued"
    assert _is_queued(bot) is True
    bot.status = "running"
    assert _is_queued(bot) is False
    bot.status = ""
    assert _is_queued(bot) is False


def test_update_bot_state_syncs_status_field(bot, tmp_path, monkeypatch):
    """update_bot_state must assign bot.status regardless of disk write."""
    monkeypatch.setattr(
        "codebot.process_manager._resolve_state_dir", lambda: tmp_path
    )
    update_bot_state(bot, "queued")
    assert bot.status == "queued"

    update_bot_state(bot, "running")
    assert bot.status == "running"


def test_is_queued_true_false_transitions(bot):
    """Cache-based behavior toggles correctly without I/O."""
    bot.status = "queued"
    assert _is_queued(bot) is True
    bot.status = "idle"
    assert _is_queued(bot) is False
    bot.status = "queued"
    assert _is_queued(bot) is True
