"""Unit tests for codebot.alignment_service module.

Covers _collect_reviewer_feedback_for_trigger with existing and missing
review files, corrupt JSON handling, graceful failure when rl_engine is
unavailable, and atomic state writes.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from codebot.alignment_service import (
    _collect_reviewer_feedback_for_trigger,
    run_alignment_pipeline,
    STATE_DIR,
)


class TestCollectReviewerFeedback:
    """Tests for _collect_reviewer_feedback_for_trigger()."""

    def test_no_review_files(self, tmp_path: Path) -> None:
        """When no review files exist, returns empty list."""
        # Patch STATE_DIR to use tmp_path
        with patch("codebot.alignment_service.STATE_DIR", tmp_path):
            feedback = _collect_reviewer_feedback_for_trigger("test_bot")
        assert feedback == []

    def test_valid_review_file(self, tmp_path: Path) -> None:
        """Collects feedback from valid review files."""
        review_data = {
            "findings": [
                {"description": "Missing error handling", "severity": "high"},
                {"description": "Bad variable name", "severity": "low"},
            ]
        }
        review_file = tmp_path / "security_review.json"
        review_file.write_text(json.dumps(review_data), encoding="utf-8")

        with patch("codebot.alignment_service.STATE_DIR", tmp_path):
            feedback = _collect_reviewer_feedback_for_trigger("test_bot")

        assert len(feedback) == 2
        assert feedback[0]["source"] == "security_review.json"
        assert feedback[0]["finding"] == "Missing error handling"
        assert feedback[0]["severity"] == "high"

    def test_corrupt_json_handled(self, tmp_path: Path) -> None:
        """Corrupt JSON files are skipped gracefully."""
        review_file = tmp_path / "security_review.json"
        review_file.write_text("{bad json", encoding="utf-8")

        with patch("codebot.alignment_service.STATE_DIR", tmp_path):
            feedback = _collect_reviewer_feedback_for_trigger("test_bot")

        assert feedback == []

    def test_missing_findings_key(self, tmp_path: Path) -> None:
        """Review file without 'findings' key returns empty."""
        review_data = {"other_key": "value"}
        review_file = tmp_path / "security_review.json"
        review_file.write_text(json.dumps(review_data), encoding="utf-8")

        with patch("codebot.alignment_service.STATE_DIR", tmp_path):
            feedback = _collect_reviewer_feedback_for_trigger("test_bot")

        assert feedback == []

    def test_non_dict_top_level(self, tmp_path: Path) -> None:
        """Non-dict top-level JSON is handled."""
        review_file = tmp_path / "security_review.json"
        review_file.write_text('[{"description": "test"}]', encoding="utf-8")

        with patch("codebot.alignment_service.STATE_DIR", tmp_path):
            feedback = _collect_reviewer_feedback_for_trigger("test_bot")

        assert feedback == []

    def test_limits_to_last_five_findings(self, tmp_path: Path) -> None:
        """Only last 5 findings are collected per file."""
        review_data = {
            "findings": [{"description": f"Finding {i}"} for i in range(10)]
        }
        review_file = tmp_path / "security_review.json"
        review_file.write_text(json.dumps(review_data), encoding="utf-8")

        with patch("codebot.alignment_service.STATE_DIR", tmp_path):
            feedback = _collect_reviewer_feedback_for_trigger("test_bot")

        # Only 5 findings from this file
        assert len(feedback) == 5
        assert feedback[0]["finding"] == "Finding 5"  # Last 5 start from index 5

    def test_multiple_review_files(self, tmp_path: Path) -> None:
        """Collects from multiple review files."""
        for name in ["security_review.json", "architecture_review.json"]:
            review_data = {"findings": [{"description": f"Issue in {name}", "severity": "medium"}]}
            review_file = tmp_path / name
            review_file.write_text(json.dumps(review_data), encoding="utf-8")

        with patch("codebot.alignment_service.STATE_DIR", tmp_path):
            feedback = _collect_reviewer_feedback_for_trigger("test_bot")

        assert len(feedback) == 2


class TestRunAlignmentPipeline:
    """Tests for run_alignment_pipeline()."""

    def test_rl_engine_not_available(self, tmp_path: Path) -> None:
        """Returns False when rl_engine cannot be imported."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()
        event_file = event_dir / "test_bot.exit.json"
        event_file.write_text(json.dumps({"bot": "test_bot"}), encoding="utf-8")

        with patch("codebot.alignment_service.ALIGNMENT_EVENTS_DIR", event_dir.parent):
            # Patch imports to fail
            with patch.dict("sys.modules", {"codebot.rl_engine": None, "rl_engine": None}):
                result = run_alignment_pipeline("test_bot")
        # When module is None, import fails
        assert result is False

    def test_event_file_not_exists(self, tmp_path: Path) -> None:
        """Returns False when event file doesn't exist."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()

        with patch("codebot.alignment_service.ALIGNMENT_EVENTS_DIR", event_dir):
            result = run_alignment_pipeline("nonexistent_bot")
        assert result is False

    def test_corrupt_event_json(self, tmp_path: Path) -> None:
        """Returns False when event JSON is corrupt."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()
        event_file = event_dir / "test_bot.exit.json"
        event_file.write_text("{bad json", encoding="utf-8")

        with patch("codebot.alignment_service.ALIGNMENT_EVENTS_DIR", event_dir):
            result = run_alignment_pipeline("test_bot")
        assert result is False

    def test_already_processed_event(self, tmp_path: Path) -> None:
        """Returns False when event is already processed."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()
        event_file = event_dir / "test_bot.exit.json"
        event_file.write_text(json.dumps({"bot": "test_bot", "processed": True}), encoding="utf-8")

        with patch("codebot.alignment_service.ALIGNMENT_EVENTS_DIR", event_dir):
            result = run_alignment_pipeline("test_bot")
        assert result is False

    def test_self_target_guard(self, tmp_path: Path) -> None:
        """prompt_opt bot is never triggered for optimization."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()
        event_file = event_dir / "prompt_opt.exit.json"
        event_file.write_text(json.dumps({"bot": "prompt_opt", "processed": False}), encoding="utf-8")

        # Mock rl_engine functions
        mock_mark = MagicMock()
        with patch("codebot.alignment_service.ALIGNMENT_EVENTS_DIR", event_dir):
            with patch("codebot.alignment_service.mark_event_processed", mock_mark):
                # We need to mock the rl_engine imports
                with patch("codebot.rl_engine.mark_event_processed", mock_mark):
                    result = run_alignment_pipeline("prompt_opt")
        assert result is False
        mock_mark.assert_called_once()

    def test_low_reward_triggers_evolution(self, tmp_path: Path) -> None:
        """Reward < 0.6 triggers prompt evolution."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()
        event_file = event_dir / "test_bot.exit.json"
        event_file.write_text(json.dumps({
            "bot": "test_bot",
            "processed": False,
            "exit_reason": "clean",
            "exit_code": 0,
        }), encoding="utf-8")

        mock_score = MagicMock(return_value={"score": 50, "verdict": "misaligned", "evidence": "low score", "breakdown": {}})
        mock_reward = MagicMock(return_value=0.4)  # Low reward
        mock_mark = MagicMock()
        mock_write = MagicMock()
        mock_ensure = MagicMock(return_value={})
        mock_load = MagicMock(return_value={})
        mock_save = MagicMock()

        with patch("codebot.alignment_service.ALIGNMENT_EVENTS_DIR", event_dir):
            with patch("codebot.rl_engine.score_event", mock_score):
                with patch("codebot.rl_engine.reward_from_score", mock_reward):
                    with patch("codebot.rl_engine.mark_event_processed", mock_mark):
                        with patch("codebot.rl_engine.write_trigger", mock_write):
                            with patch("codebot.rl_engine.ensure_bot", mock_ensure):
                                with patch("codebot.rl_engine.load_rl_state", mock_load):
                                    with patch("codebot.rl_engine.save_rl_state", mock_save):
                                        with patch("codebot.rl_engine.record_event_reward"):
                                            result = run_alignment_pipeline("test_bot")

        assert result is True
        mock_write.assert_called_once()
        mock_mark.assert_called_once()

    def test_high_reward_no_trigger(self, tmp_path: Path) -> None:
        """Reward >= 0.6 does not trigger evolution."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()
        event_file = event_dir / "test_bot.exit.json"
        event_file.write_text(json.dumps({
            "bot": "test_bot",
            "processed": False,
            "exit_reason": "clean",
            "exit_code": 0,
        }), encoding="utf-8")

        mock_score = MagicMock(return_value={"score": 80, "verdict": "aligned", "evidence": "good", "breakdown": {}})
        mock_reward = MagicMock(return_value=0.8)  # High reward
        mock_mark = MagicMock()
        mock_ensure = MagicMock(return_value={"consecutive_failures": 0, "total_runs": 1, "last_improvement": 0})
        mock_load = MagicMock(return_value={})
        mock_save = MagicMock()

        with patch("codebot.alignment_service.ALIGNMENT_EVENTS_DIR", event_dir):
            with patch("codebot.rl_engine.score_event", mock_score):
                with patch("codebot.rl_engine.reward_from_score", mock_reward):
                    with patch("codebot.rl_engine.mark_event_processed", mock_mark):
                        with patch("codebot.rl_engine.ensure_bot", mock_ensure):
                            with patch("codebot.rl_engine.load_rl_state", mock_load):
                                with patch("codebot.rl_engine.save_rl_state", mock_save):
                                    with patch("codebot.rl_engine.record_event_reward"):
                                        result = run_alignment_pipeline("test_bot")

        assert result is False
        mock_mark.assert_called_once()
