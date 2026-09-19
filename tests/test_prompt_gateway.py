"""Tests for codebot/prompt_gateway.py — spawn gating and prompt compression."""

import os
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from codebot.prompt_gateway import (
    _common_contract,
    _infer_state_dir,
    build_message,
    compress_prompt,
    estimate_tokens,
    note_spawn,
    running_count,
    set_project_adapter,
    spawn_allowed,
)


# ---------------------------------------------------------------------------
# compress_prompt tests
# ---------------------------------------------------------------------------

class TestCompressPrompt:
    """Verify compress_prompt strips only _STRIP_PREFIXES headings while preserving mission content."""

    def test_compress_prompt_preserves_non_stripped_headings(self):
        text = "## Mission\nDo the thing.\n## Role\nBe good."
        core, removed = compress_prompt(text)
        assert "## Mission" in core
        assert "## Role" in core
        assert removed == []

    def test_compress_prompt_strips_heartbeat_protocol(self):
        text = "## Mission\nDo stuff.\n## Heartbeat Protocol\nWrite heartbeats.\n## Goals\nWin."
        core, removed = compress_prompt(text)
        assert "## Mission" in core
        assert "## Goals" in core
        assert "Heartbeat Protocol" not in core
        assert "Write heartbeats." not in core
        assert "Heartbeat Protocol" in removed

    def test_compress_prompt_strips_multiple_prefixes(self):
        text = (
            "## Intro\nHello.\n"
            "## Tool Usage Rules\nRule 1.\n"
            "## Safe-Update Protocol\nSafe.\n"
            "## Outro\nBye."
        )
        core, removed = compress_prompt(text)
        assert "## Intro" in core
        assert "## Outro" in core
        assert "Tool Usage Rules" not in core
        assert "Safe-Update Protocol" not in core
        assert "Rule 1." not in core
        assert set(removed) == {"Tool Usage Rules", "Safe-Update Protocol"}

    def test_compress_prompt_empty_input(self):
        core, removed = compress_prompt("")
        assert core == ""
        assert removed == []

    def test_compress_prompt_no_headings(self):
        text = "Just plain text\nwith multiple lines."
        core, removed = compress_prompt(text)
        assert "Just plain text" in core
        assert removed == []

    def test_compress_prompt_collapses_excess_newlines(self):
        text = "## Mission\nDo stuff.\n\n\n\n\n## Goals\nWin."
        core, removed = compress_prompt(text)
        # Should collapse 4+ newlines down to 2
        assert "\n\n\n" not in core

    def test_compress_prompt_strips_evolution_heading(self):
        text = "## Evolution\nEvolve yourself.\n## Mission\nStay."
        core, removed = compress_prompt(text)
        assert "## Mission" in core
        assert "Evolution" not in core
        assert "Evolution" in removed


# ---------------------------------------------------------------------------
# _infer_state_dir tests
# ---------------------------------------------------------------------------

class TestInferStateDir:
    """Verify _infer_state_dir correctly resolves state directories from paths."""

    def test_infer_from_heartbeat_path_with_state_parent(self):
        hb = "/project/.codebot/state/bot1.heartbeat"
        ckpt = ""
        result = _infer_state_dir(hb, ckpt)
        assert result.name == "state"
        assert str(result) == "/project/.codebot/state"

    def test_infer_from_checkpoint_path_with_state_parent(self):
        hb = ""
        ckpt = "/project/.codebot/state/bot1.checkpoint.json"
        result = _infer_state_dir(hb, ckpt)
        assert result.name == "state"

    def test_infer_from_heartbeats_subdir(self):
        hb = "/project/.codebot/state/heartbeats/bot1.hb"
        ckpt = ""
        result = _infer_state_dir(hb, ckpt)
        assert result.name == "state"

    def test_infer_from_checkpoints_subdir(self):
        hb = ""
        ckpt = "/project/.codebot/state/checkpoints/bot1.ckpt"
        result = _infer_state_dir(hb, ckpt)
        assert result.name == "state"

    def test_infer_from_alignment_triggers_subdir(self):
        hb = "/project/.codebot/state/alignment_triggers/bot1.evolve.json"
        ckpt = ""
        result = _infer_state_dir(hb, ckpt)
        assert result.name == "state"

    def test_fallback_to_parent_when_no_state_dir(self):
        hb = "/some/random/path/bot1.heartbeat"
        ckpt = ""
        result = _infer_state_dir(hb, ckpt)
        assert str(result) == "/some/random/path"

    def test_both_empty_falls_back_to_adapter_or_default(self):
        # With no adapter set, should return default path
        set_project_adapter(None)
        result = _infer_state_dir("", "")
        assert result is not None

    def test_uses_adapter_when_paths_empty(self):
        mock_adapter = SimpleNamespace(
            paths=lambda: SimpleNamespace(state_dir=__import__('pathlib').Path("/adapter/state"))
        )
        set_project_adapter(mock_adapter)
        try:
            result = _infer_state_dir("", "")
            assert str(result) == "/adapter/state"
        finally:
            set_project_adapter(None)


# ---------------------------------------------------------------------------
# spawn_allowed / running_count tests
# ---------------------------------------------------------------------------

class TestSpawnGating:
    """Verify spawn gating respects MAX_CONCURRENT and MIN_SPAWN_GAP."""

    def test_running_count_no_bots(self):
        assert running_count({}) == 0

    def test_running_count_with_alive_process(self):
        proc = SimpleNamespace(poll=lambda: None)  # poll() returns None = alive
        bot = SimpleNamespace(process=proc)
        assert running_count({"b1": bot}) == 1

    def test_running_count_with_dead_process(self):
        proc = SimpleNamespace(poll=lambda: 0)  # poll() returns 0 = dead
        bot = SimpleNamespace(process=proc)
        assert running_count({"b1": bot}) == 0

    def test_running_count_mixed(self):
        alive = SimpleNamespace(process=SimpleNamespace(poll=lambda: None))
        dead = SimpleNamespace(process=SimpleNamespace(poll=lambda: 1))
        no_proc = SimpleNamespace()
        assert running_count({"a": alive, "d": dead, "n": no_proc}) == 1

    @patch("codebot.prompt_gateway.MAX_CONCURRENT", 2)
    @patch("codebot.prompt_gateway.MIN_SPAWN_GAP", 0)
    def test_spawn_allowed_under_cap(self):
        proc = SimpleNamespace(poll=lambda: None)
        bots = {"b1": SimpleNamespace(process=proc)}
        allowed, reason = spawn_allowed(bots)
        assert allowed is True
        assert "slot available" in reason

    @patch("codebot.prompt_gateway.MAX_CONCURRENT", 1)
    @patch("codebot.prompt_gateway.MIN_SPAWN_GAP", 0)
    def test_spawn_deferred_at_capacity(self):
        proc = SimpleNamespace(poll=lambda: None)
        bots = {"b1": SimpleNamespace(process=proc)}
        allowed, reason = spawn_allowed(bots)
        assert allowed is False
        assert "cap" in reason

    @patch("codebot.prompt_gateway.MAX_CONCURRENT", 10)
    @patch("codebot.prompt_gateway.MIN_SPAWN_GAP", 60)
    def test_spawn_deferred_by_gap(self):
        import codebot.prompt_gateway as pg
        old_ts = pg._last_spawn_ts
        try:
            note_spawn()  # sets _last_spawn_ts to now
            bots = {}
            allowed, reason = spawn_allowed(bots)
            assert allowed is False
            assert "spawn gap" in reason
        finally:
            pg._last_spawn_ts = old_ts

    @patch("codebot.prompt_gateway.MAX_CONCURRENT", 10)
    @patch("codebot.prompt_gateway.MIN_SPAWN_GAP", 0)
    def test_spawn_allowed_when_gap_satisfied(self):
        import codebot.prompt_gateway as pg
        old_ts = pg._last_spawn_ts
        try:
            pg._last_spawn_ts = 0.0  # long ago
            bots = {}
            allowed, reason = spawn_allowed(bots)
            assert allowed is True
        finally:
            pg._last_spawn_ts = old_ts


# ---------------------------------------------------------------------------
# build_message tests
# ---------------------------------------------------------------------------

class TestBuildMessage:
    """Verify build_message assembles correct contract text."""

    def test_build_message_contains_identity_line(self):
        msg = build_message(
            bot="test_bot",
            model="gpt-4",
            prompt_text="## Mission\nDo things.",
            heartbeat_file="/state/test_bot.heartbeat",
            ckpt_file="/state/test_bot.checkpoint.json",
            ckpt_block="",
            state_dir="/state",
            logs_dir="/logs",
            prompt_name="test.md",
        )
        assert "Sisyphus" in msg
        assert "test_bot" in msg
        assert "gpt-4" in msg

    def test_build_message_contains_contract(self):
        msg = build_message(
            bot="b1",
            model="m1",
            prompt_text="## Mission\nGo.",
            heartbeat_file="/state/b1.hb",
            ckpt_file="/state/b1.ckpt",
            ckpt_block="",
            state_dir="/state",
            logs_dir="/logs",
            prompt_name="role.md",
        )
        assert "SHARED INFRA CONTRACT" in msg
        assert "Drain:" in msg
        assert "Heartbeat:" in msg

    def test_build_message_includes_checkpoint_block(self):
        ckpt = "[CHECKPOINT HANDOFF]\nTask: resume this."
        msg = build_message(
            bot="b1",
            model="m1",
            prompt_text="## Mission\nGo.",
            heartbeat_file="/state/b1.hb",
            ckpt_file="/state/b1.ckpt",
            ckpt_block=ckpt,
            state_dir="/state",
            logs_dir="/logs",
            prompt_name="role.md",
        )
        assert ckpt in msg

    def test_build_message_strips_boilerplate_from_mission(self):
        prompt = "## Heartbeat Protocol\nWrite HB.\n## Mission\nDo work."
        msg = build_message(
            bot="b1",
            model="m1",
            prompt_text=prompt,
            heartbeat_file="/state/b1.hb",
            ckpt_file="/state/b1.ckpt",
            ckpt_block="",
            state_dir="/state",
            logs_dir="/logs",
            prompt_name="role.md",
        )
        # The compressed mission section should not contain the stripped heading's body
        mission_section = msg.split("--- Mission Spec")[1]
        assert "Write HB." not in mission_section
        assert "## Mission" in mission_section


# ---------------------------------------------------------------------------
# _common_contract tests
# ---------------------------------------------------------------------------

class TestCommonContract:
    """Verify _common_contract generates expected paths."""

    def test_contract_contains_drain_path(self):
        c = _common_contract("bot1", "/s/bot1.hb", "/s/bot1.ckpt", "/my/state")
        assert "/my/state/.drain" in c

    def test_contract_contains_update_lock_path(self):
        c = _common_contract("bot1", "/s/bot1.hb", "/s/bot1.ckpt", "/my/state")
        assert "/my/state/.update_lock" in c

    def test_contract_contains_alignment_trigger(self):
        c = _common_contract("mybot", "/s/hb", "/s/ckpt", "/st")
        assert "mybot.evolve.json" in c


# ---------------------------------------------------------------------------
# estimate_tokens tests
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_estimate_tokens_basic(self):
        assert estimate_tokens("abcd") == 1

    def test_estimate_tokens_minimum_one(self):
        assert estimate_tokens("") == 1

    def test_estimate_tokens_longer_text(self):
        assert estimate_tokens("a" * 100) == 25


# ---------------------------------------------------------------------------
# Adapter fallback tests
# ---------------------------------------------------------------------------

class TestAdapterFallback:
    def test_set_and_get_adapter(self):
        from codebot.prompt_gateway import get_adapter
        sentinel = object()
        set_project_adapter(sentinel)
        assert get_adapter() is sentinel
        set_project_adapter(None)
        assert get_adapter() is None
