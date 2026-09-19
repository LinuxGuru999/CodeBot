"""Tests for codebot/prompt_optimizer.py — self-improvement evolution loop."""

import json
import sys
import time
from pathlib import Path

import pytest

# Ensure project root is on sys.path for direct imports
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import codebot.prompt_optimizer as po


# ---------------------------------------------------------------------------
# Constants verification
# ---------------------------------------------------------------------------

class TestConstants:
    def test_max_evolutions_per_prompt(self):
        assert po.MAX_EVOLUTIONS_PER_PROMPT == 5

    def test_max_prompt_chars(self):
        assert po.MAX_PROMPT_CHARS == 15000

    def test_evolution_header_marker(self):
        assert po.EVOLUTION_HEADER == "<!-- CODEBOT EVOLUTION -->"


# ---------------------------------------------------------------------------
# PATTERN_HINTS lookup tests
# ---------------------------------------------------------------------------

class TestPatternHints:
    def test_all_expected_patterns_exist(self):
        expected = [
            "add_file_paths",
            "add_examples",
            "reword_instructions",
            "add_constraints",
            "tighten_heartbeat_format",
            "tighten_rebellion_filter",
            "spec_verification_gate",
            "fp_exclusion_patterns",
            "canonical_path_mirror",
            "heartbeat_max_gap_enforcement",
        ]
        for pattern in expected:
            assert pattern in po.PATTERN_HINTS, f"Missing pattern: {pattern}"

    def test_hints_are_non_empty_strings(self):
        for key, value in po.PATTERN_HINTS.items():
            assert isinstance(value, str)
            assert len(value) > 10, f"Hint for {key} too short"


# ---------------------------------------------------------------------------
# MAX_EVOLUTIONS_PER_PROMPT cap enforcement tests
# ---------------------------------------------------------------------------

class TestEvolutionCap:
    def test_select_best_pattern_returns_none_at_cap(self, tmp_path):
        prompt_path = tmp_path / "tester.md"
        # Write a prompt with exactly 5 evolution headers
        content = "## Mission\nDo stuff.\n"
        for i in range(5):
            content += f"\n{po.EVOLUTION_HEADER}\n## Evolution\npattern: p{i}\n"
        prompt_path.write_text(content, encoding="utf-8")

        q_values = {"add_file_paths": 1.0}
        result = po._select_best_pattern(q_values, prompt_path)
        assert result is None

    def test_select_best_pattern_works_under_cap(self, tmp_path):
        prompt_path = tmp_path / "tester.md"
        content = "## Mission\nDo stuff.\n"
        for i in range(3):
            content += f"\n{po.EVOLUTION_HEADER}\n## Evolution\npattern: p{i}\n"
        prompt_path.write_text(content, encoding="utf-8")

        q_values = {"add_file_paths": 1.0}
        result = po._select_best_pattern(q_values, prompt_path)
        assert result == "add_file_paths"


# ---------------------------------------------------------------------------
# MAX_PROMPT_CHARS truncation tests
# ---------------------------------------------------------------------------

class TestMaxPromptChars:
    def test_append_evolution_skips_when_too_large(self, tmp_path):
        prompt_path = tmp_path / "big.md"
        # Create a prompt near the max size
        prompt_path.write_text("x" * (po.MAX_PROMPT_CHARS - 10), encoding="utf-8")
        result = po._append_evolution(
            prompt_path, "test_pattern", "A" * 100,
            "evolve", "test", 50, 0.5,
        )
        assert result is False

    def test_append_evolution_succeeds_under_limit(self, tmp_path):
        prompt_path = tmp_path / "small.md"
        prompt_path.write_text("## Mission\nSmall prompt.\n", encoding="utf-8")
        result = po._append_evolution(
            prompt_path, "add_examples", "Include examples.",
            "evolve", "stagnation", 40, 0.3,
        )
        assert result is True
        content = prompt_path.read_text(encoding="utf-8")
        assert "Include examples." in content


# ---------------------------------------------------------------------------
# Idempotency tests (skips if hint already present)
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_append_evolution_skips_duplicate_pattern(self, tmp_path):
        prompt_path = tmp_path / "idem.md"
        prompt_path.write_text("## Mission\nWork.\nPattern: add_file_paths\n", encoding="utf-8")
        result = po._append_evolution(
            prompt_path, "add_file_paths", "Always use full paths.",
            "evolve", "test", 50, 0.5,
        )
        assert result is False

    def test_append_evolution_skips_lowercase_pattern_match(self, tmp_path):
        prompt_path = tmp_path / "idem2.md"
        prompt_path.write_text("## Mission\nWork.\npattern: add_examples\n", encoding="utf-8")
        result = po._append_evolution(
            prompt_path, "add_examples", "Add examples.",
            "evolve", "test", 50, 0.5,
        )
        assert result is False

    def test_select_best_pattern_skips_already_applied(self, tmp_path):
        prompt_path = tmp_path / "skip.md"
        prompt_path.write_text("## Mission\nWork.\npattern: add_file_paths\n", encoding="utf-8")
        q_values = {"add_file_paths": 10.0, "add_examples": 5.0}
        result = po._select_best_pattern(q_values, prompt_path)
        assert result == "add_examples"


# ---------------------------------------------------------------------------
# Trigger file deletion after consumption tests
# ---------------------------------------------------------------------------

class TestTriggerFileDeletion:
    def test_consume_triggers_deletes_processed_file(self, tmp_path):
        triggers_dir = tmp_path / "triggers"
        roles_dir = tmp_path / "roles"
        triggers_dir.mkdir()
        roles_dir.mkdir()

        # Create a role prompt
        (roles_dir / "tester.md").write_text("## Mission\nTest things.\n", encoding="utf-8")

        # Create trigger file
        trigger_data = {
            "bot": "tester",
            "rl": {"q_values": {"add_examples": 1.0}},
            "verdict": "evolve",
            "reason": "low score",
            "score": 30,
            "reward": -0.5,
        }
        trigger_file = triggers_dir / "tester.evolve.json"
        trigger_file.write_text(json.dumps(trigger_data), encoding="utf-8")

        consumed = po.consume_triggers(triggers_dir, roles_dir)
        assert consumed == 1
        assert not trigger_file.exists()

    def test_consume_triggers_deletes_file_even_with_no_q_values(self, tmp_path):
        triggers_dir = tmp_path / "triggers"
        roles_dir = tmp_path / "roles"
        triggers_dir.mkdir()
        roles_dir.mkdir()

        (roles_dir / "tester.md").write_text("## Mission\nWork.\n", encoding="utf-8")

        trigger_file = triggers_dir / "tester.evolve.json"
        trigger_file.write_text(json.dumps({"bot": "tester"}), encoding="utf-8")

        po.consume_triggers(triggers_dir, roles_dir)
        assert not trigger_file.exists()

    def test_consume_triggers_deletes_self_modification_trigger(self, tmp_path):
        triggers_dir = tmp_path / "triggers"
        roles_dir = tmp_path / "roles"
        triggers_dir.mkdir()
        roles_dir.mkdir()

        trigger_file = triggers_dir / "prompt_optimizer.evolve.json"
        trigger_file.write_text(json.dumps({"bot": "prompt_optimizer"}), encoding="utf-8")

        po.consume_triggers(triggers_dir, roles_dir)
        assert not trigger_file.exists()

    def test_missing_triggers_dir_returns_zero(self, tmp_path):
        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        result = po.consume_triggers(tmp_path / "nonexistent", roles_dir)
        assert result == 0


# ---------------------------------------------------------------------------
# Append-only behavior tests (no existing content removed)
# ---------------------------------------------------------------------------

class TestAppendOnlyBehavior:
    def test_existing_content_preserved_after_evolution(self, tmp_path):
        prompt_path = tmp_path / "preserve.md"
        original = "## Mission\nOriginal mission content.\n## Identity\nTester.\n"
        prompt_path.write_text(original, encoding="utf-8")

        po._append_evolution(
            prompt_path, "add_constraints", "State constraints clearly.",
            "evolve", "test", 50, 0.5,
        )

        content = prompt_path.read_text(encoding="utf-8")
        assert "## Mission" in content
        assert "Original mission content." in content
        assert "## Identity" in content
        assert "Tester." in content
        assert "State constraints clearly." in content

    def test_evolution_header_inserted(self, tmp_path):
        prompt_path = tmp_path / "header.md"
        prompt_path.write_text("## Mission\nWork.\n", encoding="utf-8")

        po._append_evolution(
            prompt_path, "add_examples", "Use examples.",
            "evolve", "test", 50, 0.5,
        )

        content = prompt_path.read_text(encoding="utf-8")
        assert po.EVOLUTION_HEADER in content
        assert "<!-- END EVOLUTION -->" in content


# ---------------------------------------------------------------------------
# Self-prompt modification guard tests
# ---------------------------------------------------------------------------

class TestSelfModificationGuard:
    def test_prompt_optimizer_trigger_is_deleted_not_applied(self, tmp_path):
        triggers_dir = tmp_path / "triggers"
        roles_dir = tmp_path / "roles"
        triggers_dir.mkdir()
        roles_dir.mkdir()

        # Even if prompt_optimizer.md exists, it should NOT be evolved
        (roles_dir / "prompt_optimizer.md").write_text("## Mission\nOptimize.\n", encoding="utf-8")

        trigger_data = {
            "bot": "prompt_optimizer",
            "rl": {"q_values": {"add_examples": 1.0}},
            "verdict": "evolve",
            "reason": "test",
            "score": 10,
            "reward": -1.0,
        }
        trigger_file = triggers_dir / "prompt_optimizer.evolve.json"
        trigger_file.write_text(json.dumps(trigger_data), encoding="utf-8")

        consumed = po.consume_triggers(triggers_dir, roles_dir)
        assert consumed == 0  # Should not evolve itself
        assert not trigger_file.exists()  # But trigger should be cleaned up

        # Verify prompt was NOT modified
        content = (roles_dir / "prompt_optimizer.md").read_text(encoding="utf-8")
        assert po.EVOLUTION_HEADER not in content


# ---------------------------------------------------------------------------
# _generate_feedback_hint tests
# ---------------------------------------------------------------------------

class TestGenerateFeedbackHint:
    def test_empty_feedback_returns_empty(self):
        assert po._generate_feedback_hint([]) == ""

    def test_feedback_with_description_and_recommendation(self):
        fb = [{"description": "Missing tests", "recommendation": "Add unit tests"}]
        hint = po._generate_feedback_hint(fb)
        assert "Missing tests" in hint
        assert "Add unit tests" in hint

    def test_feedback_capped_at_5_items(self):
        fb = [{"description": f"Issue {i}"} for i in range(10)]
        hint = po._generate_feedback_hint(fb)
        # Should only contain first 5
        assert "Issue 0" in hint
        assert "Issue 4" in hint
        assert "Issue 9" not in hint

    def test_feedback_without_description_or_recommendation(self):
        fb = [{"other_field": "value"}]
        hint = po._generate_feedback_hint(fb)
        assert hint == ""


# ---------------------------------------------------------------------------
# consume_triggers with reviewer_feedback tests
# ---------------------------------------------------------------------------

class TestConsumeTriggersWithFeedback:
    def test_reviewer_feedback_generates_hint(self, tmp_path):
        triggers_dir = tmp_path / "triggers"
        roles_dir = tmp_path / "roles"
        triggers_dir.mkdir()
        roles_dir.mkdir()

        (roles_dir / "tester.md").write_text("## Mission\nWork.\n", encoding="utf-8")

        trigger_data = {
            "bot": "tester",
            "reviewer_feedback": [
                {"description": "Fix indentation", "recommendation": "Use 4 spaces"}
            ],
            "verdict": "rework",
            "reason": "reviewer rejected",
            "score": 40,
            "reward": -0.3,
        }
        trigger_file = triggers_dir / "tester.evolve.json"
        trigger_file.write_text(json.dumps(trigger_data), encoding="utf-8")

        consumed = po.consume_triggers(triggers_dir, roles_dir)
        assert consumed == 1

        content = (roles_dir / "tester.md").read_text(encoding="utf-8")
        assert "Fix indentation" in content
        assert "Use 4 spaces" in content

    def test_corrupt_trigger_file_is_skipped(self, tmp_path):
        triggers_dir = tmp_path / "triggers"
        roles_dir = tmp_path / "roles"
        triggers_dir.mkdir()
        roles_dir.mkdir()

        trigger_file = triggers_dir / "bad.evolve.json"
        trigger_file.write_text("NOT VALID JSON{{{", encoding="utf-8")

        consumed = po.consume_triggers(triggers_dir, roles_dir)
        assert consumed == 0

    def test_bot_name_with_dash_uses_base_name(self, tmp_path):
        triggers_dir = tmp_path / "triggers"
        roles_dir = tmp_path / "roles"
        triggers_dir.mkdir()
        roles_dir.mkdir()

        (roles_dir / "tester.md").write_text("## Mission\nWork.\n", encoding="utf-8")

        trigger_data = {
            "bot": "tester-2",
            "rl": {"q_values": {"add_examples": 1.0}},
            "verdict": "evolve",
            "reason": "test",
            "score": 30,
            "reward": 0.1,
        }
        trigger_file = triggers_dir / "tester-2.evolve.json"
        trigger_file.write_text(json.dumps(trigger_data), encoding="utf-8")

        consumed = po.consume_triggers(triggers_dir, roles_dir)
        assert consumed == 1
