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
    run_alignment_pipeline_for_all,
)
from codebot.state_manager import PathConfig


def _make_paths(tmp_path: Path, event_dir: Path | None = None) -> PathConfig:
    """Build a PathConfig rooted at tmp_path for patching state_manager._paths."""
    return PathConfig(
        bots_dir=tmp_path,
        state_dir=tmp_path,
        logs_dir=tmp_path / "logs",
        backup_dir=tmp_path / "backup",
        alignment_events_dir=event_dir if event_dir is not None else tmp_path / "alignment_events",
        drain_file=tmp_path / ".drain",
        update_lock=tmp_path / ".update_lock",
        restart_file=tmp_path / ".restart",
    )


class TestCollectReviewerFeedback:
    """Tests for _collect_reviewer_feedback_for_trigger()."""

    def test_no_review_files(self, tmp_path: Path) -> None:
        """When no review files exist, returns empty list."""
        # Patch state_manager._paths to use tmp_path
        paths_config = PathConfig(
            bots_dir=tmp_path,
            state_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            backup_dir=tmp_path / "backup",
            alignment_events_dir=tmp_path / "alignment_events",
            drain_file=tmp_path / ".drain",
            update_lock=tmp_path / ".update_lock",
            restart_file=tmp_path / ".restart",
        )
        with patch("codebot.state_manager._paths", paths_config):
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

        paths_config = PathConfig(
            bots_dir=tmp_path,
            state_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            backup_dir=tmp_path / "backup",
            alignment_events_dir=tmp_path / "alignment_events",
            drain_file=tmp_path / ".drain",
            update_lock=tmp_path / ".update_lock",
            restart_file=tmp_path / ".restart",
        )
        with patch("codebot.state_manager._paths", paths_config):
            feedback = _collect_reviewer_feedback_for_trigger("test_bot")

        assert len(feedback) == 2
        assert feedback[0]["source"] == "security_review.json"
        assert feedback[0]["finding"] == "Missing error handling"
        assert feedback[0]["severity"] == "high"

    def test_corrupt_json_handled(self, tmp_path: Path) -> None:
        """Corrupt JSON files are skipped gracefully."""
        review_file = tmp_path / "security_review.json"
        review_file.write_text("{bad json", encoding="utf-8")

        paths_config = PathConfig(
            bots_dir=tmp_path,
            state_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            backup_dir=tmp_path / "backup",
            alignment_events_dir=tmp_path / "alignment_events",
            drain_file=tmp_path / ".drain",
            update_lock=tmp_path / ".update_lock",
            restart_file=tmp_path / ".restart",
        )
        with patch("codebot.state_manager._paths", paths_config):
            feedback = _collect_reviewer_feedback_for_trigger("test_bot")

        assert feedback == []

    def test_missing_findings_key(self, tmp_path: Path) -> None:
        """Review file without 'findings' key returns empty."""
        review_data = {"other_key": "value"}
        review_file = tmp_path / "security_review.json"
        review_file.write_text(json.dumps(review_data), encoding="utf-8")

        paths_config = PathConfig(
            bots_dir=tmp_path,
            state_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            backup_dir=tmp_path / "backup",
            alignment_events_dir=tmp_path / "alignment_events",
            drain_file=tmp_path / ".drain",
            update_lock=tmp_path / ".update_lock",
            restart_file=tmp_path / ".restart",
        )
        with patch("codebot.state_manager._paths", paths_config):
            feedback = _collect_reviewer_feedback_for_trigger("test_bot")

        assert feedback == []

    def test_non_dict_top_level(self, tmp_path: Path) -> None:
        """Non-dict top-level JSON is handled."""
        review_file = tmp_path / "security_review.json"
        review_file.write_text('[{"description": "test"}]', encoding="utf-8")

        paths_config = PathConfig(
            bots_dir=tmp_path,
            state_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            backup_dir=tmp_path / "backup",
            alignment_events_dir=tmp_path / "alignment_events",
            drain_file=tmp_path / ".drain",
            update_lock=tmp_path / ".update_lock",
            restart_file=tmp_path / ".restart",
        )
        with patch("codebot.state_manager._paths", paths_config):
            feedback = _collect_reviewer_feedback_for_trigger("test_bot")

        assert feedback == []

    def test_limits_to_last_five_findings(self, tmp_path: Path) -> None:
        """Only last 5 findings are collected per file."""
        review_data = {
            "findings": [{"description": f"Finding {i}"} for i in range(10)]
        }
        review_file = tmp_path / "security_review.json"
        review_file.write_text(json.dumps(review_data), encoding="utf-8")

        paths_config = PathConfig(
            bots_dir=tmp_path,
            state_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            backup_dir=tmp_path / "backup",
            alignment_events_dir=tmp_path / "alignment_events",
            drain_file=tmp_path / ".drain",
            update_lock=tmp_path / ".update_lock",
            restart_file=tmp_path / ".restart",
        )
        with patch("codebot.state_manager._paths", paths_config):
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

        paths_config = PathConfig(
            bots_dir=tmp_path,
            state_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            backup_dir=tmp_path / "backup",
            alignment_events_dir=tmp_path / "alignment_events",
            drain_file=tmp_path / ".drain",
            update_lock=tmp_path / ".update_lock",
            restart_file=tmp_path / ".restart",
        )
        with patch("codebot.state_manager._paths", paths_config):
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

        paths_config = PathConfig(
            bots_dir=tmp_path,
            state_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            backup_dir=tmp_path / "backup",
            alignment_events_dir=event_dir,
            drain_file=tmp_path / ".drain",
            update_lock=tmp_path / ".update_lock",
            restart_file=tmp_path / ".restart",
        )
        with patch("codebot.state_manager._paths", paths_config):
            # Patch imports to fail
            with patch.dict("sys.modules", {"codebot.rl_engine": None, "rl_engine": None}):
                result = run_alignment_pipeline("test_bot")
        # When module is None, import fails
        assert result is False

    def test_event_file_not_exists(self, tmp_path: Path) -> None:
        """Returns False when event file doesn't exist."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()

        paths_config = PathConfig(
            bots_dir=tmp_path,
            state_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            backup_dir=tmp_path / "backup",
            alignment_events_dir=event_dir,
            drain_file=tmp_path / ".drain",
            update_lock=tmp_path / ".update_lock",
            restart_file=tmp_path / ".restart",
        )
        with patch("codebot.state_manager._paths", paths_config):
            result = run_alignment_pipeline("nonexistent_bot")
        assert result is False

    def test_corrupt_event_json(self, tmp_path: Path) -> None:
        """Returns False when event JSON is corrupt."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()
        event_file = event_dir / "test_bot.exit.json"
        event_file.write_text("{bad json", encoding="utf-8")

        paths_config = PathConfig(
            bots_dir=tmp_path,
            state_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            backup_dir=tmp_path / "backup",
            alignment_events_dir=event_dir,
            drain_file=tmp_path / ".drain",
            update_lock=tmp_path / ".update_lock",
            restart_file=tmp_path / ".restart",
        )
        with patch("codebot.state_manager._paths", paths_config):
            result = run_alignment_pipeline("test_bot")
        assert result is False

    def test_already_processed_event(self, tmp_path: Path) -> None:
        """Returns False when event is already processed."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()
        event_file = event_dir / "test_bot.exit.json"
        event_file.write_text(json.dumps({"bot": "test_bot", "processed": True}), encoding="utf-8")

        paths_config = PathConfig(
            bots_dir=tmp_path,
            state_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            backup_dir=tmp_path / "backup",
            alignment_events_dir=event_dir,
            drain_file=tmp_path / ".drain",
            update_lock=tmp_path / ".update_lock",
            restart_file=tmp_path / ".restart",
        )
        with patch("codebot.state_manager._paths", paths_config):
            result = run_alignment_pipeline("test_bot")
        assert result is False

    def test_self_target_guard(self, tmp_path: Path) -> None:
        """prompt_opt bot is never triggered for optimization."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()
        event_file = event_dir / "prompt_opt.exit.json"
        event_file.write_text(json.dumps({"bot": "prompt_opt", "processed": False}), encoding="utf-8")

        paths_config = PathConfig(
            bots_dir=tmp_path,
            state_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            backup_dir=tmp_path / "backup",
            alignment_events_dir=event_dir,
            drain_file=tmp_path / ".drain",
            update_lock=tmp_path / ".update_lock",
            restart_file=tmp_path / ".restart",
        )
        # Mock rl_engine functions — isolate all imports used in run_alignment_pipeline
        mock_mark = MagicMock()
        mock_score = MagicMock()
        mock_reward = MagicMock()
        mock_record = MagicMock()
        mock_write = MagicMock()
        mock_load = MagicMock(return_value={})
        mock_save = MagicMock()
        mock_ensure = MagicMock(return_value={})
        mock_list = MagicMock(return_value=[])
        # Mock metrics_collector to prevent any side effects
        mock_mc = MagicMock()
        mock_mc.collect_all.return_value = {}
        with patch("codebot.state_manager._paths", paths_config):
            with patch("codebot.rl_engine.mark_event_processed", mock_mark):
                with patch("codebot.rl_engine.score_event", mock_score):
                    with patch("codebot.rl_engine.reward_from_score", mock_reward):
                        with patch("codebot.rl_engine.record_event_reward", mock_record):
                            with patch("codebot.rl_engine.write_trigger", mock_write):
                                with patch("codebot.rl_engine.load_rl_state", mock_load):
                                    with patch("codebot.rl_engine.save_rl_state", mock_save):
                                        with patch("codebot.rl_engine.ensure_bot", mock_ensure):
                                            with patch("codebot.rl_engine.list_pending_events", mock_list):
                                                with patch.dict("sys.modules", {"codebot.metrics_collector": mock_mc}):
                                                    result = run_alignment_pipeline("prompt_opt")
        assert result is False
        mock_mark.assert_called_once()
        mock_score.assert_not_called()
        mock_reward.assert_not_called()

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

        paths_config = PathConfig(
            bots_dir=tmp_path,
            state_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            backup_dir=tmp_path / "backup",
            alignment_events_dir=event_dir,
            drain_file=tmp_path / ".drain",
            update_lock=tmp_path / ".update_lock",
            restart_file=tmp_path / ".restart",
        )
        mock_score = MagicMock(return_value={"score": 50, "verdict": "misaligned", "evidence": "low score", "breakdown": {}})
        mock_reward = MagicMock(return_value=0.4)  # Low reward
        mock_mark = MagicMock()
        mock_write = MagicMock()
        mock_ensure = MagicMock(return_value={})
        mock_load = MagicMock(return_value={})
        mock_save = MagicMock()

        with patch("codebot.state_manager._paths", paths_config):
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

        paths_config = PathConfig(
            bots_dir=tmp_path,
            state_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            backup_dir=tmp_path / "backup",
            alignment_events_dir=event_dir,
            drain_file=tmp_path / ".drain",
            update_lock=tmp_path / ".update_lock",
            restart_file=tmp_path / ".restart",
        )
        mock_score = MagicMock(return_value={"score": 80, "verdict": "aligned", "evidence": "good", "breakdown": {}})
        mock_reward = MagicMock(return_value=0.8)  # High reward
        mock_mark = MagicMock()
        mock_ensure = MagicMock(return_value={"consecutive_failures": 0, "total_runs": 1, "last_improvement": 0})
        mock_load = MagicMock(return_value={})
        mock_save = MagicMock()

        with patch("codebot.state_manager._paths", paths_config):
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


    def test_evolve_after_retries_trigger(self, tmp_path: Path) -> None:
        """consecutive_failures >= 3 triggers evolve_after_retries with feedback."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()
        event_file = event_dir / "test_bot.exit.json"
        event_file.write_text(json.dumps({
            "bot": "test_bot",
            "processed": False,
            "exit_reason": "clean",
            "exit_code": 0,
        }), encoding="utf-8")

        paths_config = PathConfig(
            bots_dir=tmp_path,
            state_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            backup_dir=tmp_path / "backup",
            alignment_events_dir=event_dir,
            drain_file=tmp_path / ".drain",
            update_lock=tmp_path / ".update_lock",
            restart_file=tmp_path / ".restart",
        )
        mock_score = MagicMock(return_value={"score": 75, "verdict": "needs_attention", "evidence": "ok", "breakdown": {}})
        mock_reward = MagicMock(return_value=0.75)  # Above 0.6 trigger
        mock_mark = MagicMock()
        mock_write = MagicMock()
        mock_ensure = MagicMock(return_value={"consecutive_failures": 3, "total_runs": 4, "last_improvement": 0})
        mock_load = MagicMock(return_value={})
        mock_save = MagicMock()

        with patch("codebot.state_manager._paths", paths_config):
            with patch("codebot.rl_engine.score_event", mock_score):
                with patch("codebot.rl_engine.reward_from_score", mock_reward):
                    with patch("codebot.rl_engine.mark_event_processed", mock_mark):
                        with patch("codebot.rl_engine.write_trigger", mock_write):
                            with patch("codebot.rl_engine.ensure_bot", mock_ensure):
                                with patch("codebot.rl_engine.load_rl_state", mock_load):
                                    with patch("codebot.rl_engine.save_rl_state", mock_save):
                                        with patch("codebot.rl_engine.record_event_reward"):
                                            with patch("codebot.rl_engine.list_pending_events", MagicMock(return_value=[])):
                                                result = run_alignment_pipeline("test_bot")

        assert result is True
        mock_write.assert_called_once()
        mock_mark.assert_called_once()
        # evolve_after_retries verdict recorded
        assert mock_mark.call_args[0][4] == "evolve_after_retries"

    def test_stagnation_trigger(self, tmp_path: Path) -> None:
        """Stagnation (total_runs >= 5, no recent improvement) triggers evolve."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()
        event_file = event_dir / "test_bot.exit.json"
        event_file.write_text(json.dumps({
            "bot": "test_bot",
            "processed": False,
            "exit_reason": "clean",
            "exit_code": 0,
        }), encoding="utf-8")

        paths_config = PathConfig(
            bots_dir=tmp_path,
            state_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            backup_dir=tmp_path / "backup",
            alignment_events_dir=event_dir,
            drain_file=tmp_path / ".drain",
            update_lock=tmp_path / ".update_lock",
            restart_file=tmp_path / ".restart",
        )
        mock_score = MagicMock(return_value={"score": 75, "verdict": "needs_attention", "evidence": "ok", "breakdown": {}})
        mock_reward = MagicMock(return_value=0.75)
        mock_mark = MagicMock()
        mock_write = MagicMock()
        mock_ensure = MagicMock(return_value={
            "consecutive_failures": 0, "total_runs": 6, "last_improvement": 111.0,
        })
        mock_load = MagicMock(return_value={})
        mock_save = MagicMock()

        with patch("codebot.state_manager._paths", paths_config):
            with patch("codebot.rl_engine.score_event", mock_score):
                with patch("codebot.rl_engine.reward_from_score", mock_reward):
                    with patch("codebot.rl_engine.mark_event_processed", mock_mark):
                        with patch("codebot.rl_engine.write_trigger", mock_write):
                            with patch("codebot.rl_engine.ensure_bot", mock_ensure):
                                with patch("codebot.rl_engine.load_rl_state", mock_load):
                                    with patch("codebot.rl_engine.save_rl_state", mock_save):
                                        with patch("codebot.rl_engine.record_event_reward"):
                                            with patch("codebot.rl_engine.list_pending_events", MagicMock(return_value=[])):
                                                result = run_alignment_pipeline("test_bot")

        assert result is True
        mock_write.assert_called_once()
        mock_mark.assert_called_once()
        assert mock_mark.call_args[0][4] == "stagnation_evolve"

    def test_pipeline_exception_returns_false(self, tmp_path: Path) -> None:
        """score_event raising is caught by the outer handler, returns False."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()
        event_file = event_dir / "test_bot.exit.json"
        event_file.write_text(json.dumps({
            "bot": "test_bot",
            "processed": False,
            "exit_reason": "clean",
            "exit_code": 0,
        }), encoding="utf-8")

        paths_config = PathConfig(
            bots_dir=tmp_path,
            state_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            backup_dir=tmp_path / "backup",
            alignment_events_dir=event_dir,
            drain_file=tmp_path / ".drain",
            update_lock=tmp_path / ".update_lock",
            restart_file=tmp_path / ".restart",
        )
        mock_score = MagicMock(side_effect=RuntimeError("boom"))
        mock_mark = MagicMock()

        with patch("codebot.state_manager._paths", paths_config):
            with patch("codebot.rl_engine.score_event", mock_score):
                with patch("codebot.rl_engine.mark_event_processed", mock_mark):
                    with patch("codebot.rl_engine.reward_from_score", MagicMock()):
                        with patch("codebot.rl_engine.record_event_reward", MagicMock()):
                            with patch("codebot.rl_engine.write_trigger", MagicMock()):
                                with patch("codebot.rl_engine.load_rl_state", MagicMock(return_value={})):
                                    with patch("codebot.rl_engine.save_rl_state", MagicMock()):
                                        with patch("codebot.rl_engine.ensure_bot", MagicMock(return_value={})):
                                            with patch("codebot.rl_engine.list_pending_events", MagicMock(return_value=[])):
                                                result = run_alignment_pipeline("test_bot")

        assert result is False
        mock_mark.assert_not_called()

    def test_metrics_collector_failure_degrades(self, tmp_path: Path) -> None:
        """collect_all raising degrades to metrics=None, still runs."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()
        event_file = event_dir / "test_bot.exit.json"
        event_file.write_text(json.dumps({
            "bot": "test_bot",
            "processed": False,
            "exit_reason": "clean",
            "exit_code": 0,
        }), encoding="utf-8")

        paths_config = _make_paths(tmp_path, event_dir)
        mock_score = MagicMock(return_value={"score": 80, "verdict": "aligned", "evidence": "good", "breakdown": {}})
        mock_reward = MagicMock(return_value=0.8)
        mock_mark = MagicMock()
        mock_ensure = MagicMock(return_value={"consecutive_failures": 0, "total_runs": 1, "last_improvement": 0})
        mock_load = MagicMock(return_value={})
        mock_save = MagicMock()

        # Collector whose collect_all raises: pipeline must degrade to metrics=None.
        mock_mc = MagicMock()
        mock_mc.collect_all.side_effect = RuntimeError("metrics down")

        with patch("codebot.state_manager._paths", paths_config):
            with patch("codebot.rl_engine.score_event", mock_score):
                with patch("codebot.rl_engine.reward_from_score", mock_reward) as m_reward:
                    with patch("codebot.rl_engine.mark_event_processed", mock_mark):
                        with patch("codebot.rl_engine.ensure_bot", mock_ensure):
                            with patch("codebot.rl_engine.load_rl_state", mock_load):
                                with patch("codebot.rl_engine.save_rl_state", mock_save):
                                    with patch("codebot.rl_engine.record_event_reward"):
                                        with patch("codebot.rl_engine.list_pending_events", MagicMock(return_value=[])):
                                            with patch.dict("sys.modules", {"codebot.metrics_collector": mock_mc}):
                                                result = run_alignment_pipeline("test_bot")

        assert result is False
        mock_mark.assert_called_once()
        # metrics fell back to None
        assert m_reward.call_args[1].get("metrics") is None


class TestRunAlignmentPipelineForAll:
    """Tests for run_alignment_pipeline_for_all() — uses get_paths()."""

    def test_no_pending_events(self, tmp_path: Path) -> None:
        """Empty events dir returns without calling the pipeline."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()
        paths_config = _make_paths(tmp_path, event_dir)
        with patch("codebot.state_manager._paths", paths_config):
            with patch(
                "codebot.alignment_service.run_alignment_pipeline"
            ) as mock_pipeline:
                run_alignment_pipeline_for_all()
        mock_pipeline.assert_not_called()

    def test_processes_pending_tuples(self, tmp_path: Path) -> None:
        """Tuple entries (path, event) are unpacked and dispatched by bot name."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()
        event_a = event_dir / "bot_a.exit.json"
        event_a.write_text(json.dumps({"bot": "bot_a"}), encoding="utf-8")
        event_b = event_dir / "bot_b.exit.json"
        event_b.write_text(json.dumps({"bot": "bot_b"}), encoding="utf-8")
        paths_config = _make_paths(tmp_path, event_dir)
        pending = [(event_a, {"bot": "bot_a"}), (event_b, {"bot": "bot_b"})]
        with patch("codebot.state_manager._paths", paths_config):
            with patch(
                "codebot.rl_engine.list_pending_events", return_value=pending
            ):
                with patch(
                    "codebot.alignment_service.run_alignment_pipeline"
                ) as mock_pipeline:
                    run_alignment_pipeline_for_all()
        assert mock_pipeline.call_count == 2
        called = [c.args[0] for c in mock_pipeline.call_args_list]
        assert called == ["bot_a", "bot_b"]

    def test_processes_bare_paths_and_skips_corrupt(self, tmp_path: Path) -> None:
        """Bare Path entries are accepted; corrupt JSON is skipped gracefully."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()
        good = event_dir / "good.exit.json"
        good.write_text(json.dumps({"bot": "good_bot"}), encoding="utf-8")
        bad = event_dir / "bad.exit.json"
        bad.write_text("{bad json", encoding="utf-8")
        paths_config = _make_paths(tmp_path, event_dir)
        with patch("codebot.state_manager._paths", paths_config):
            with patch(
                "codebot.rl_engine.list_pending_events", return_value=[good, bad]
            ):
                with patch(
                    "codebot.alignment_service.run_alignment_pipeline"
                ) as mock_pipeline:
                    run_alignment_pipeline_for_all()
        mock_pipeline.assert_called_once_with("good_bot")

    def test_uses_configured_events_dir(self, tmp_path: Path) -> None:
        """list_pending_events receives get_paths().alignment_events_dir."""
        event_dir = tmp_path / "alignment_events"
        event_dir.mkdir()
        paths_config = _make_paths(tmp_path, event_dir)
        with patch("codebot.state_manager._paths", paths_config):
            with patch(
                "codebot.rl_engine.list_pending_events", return_value=[]
            ) as mock_list:
                run_alignment_pipeline_for_all()
        mock_list.assert_called_once_with(event_dir)
