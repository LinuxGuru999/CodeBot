"""Tests for codebot/metrics_collector.py — telemetry gathering + incremental mode."""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import codebot.metrics_collector as mc


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    bots_dir = tmp_path / "bots"
    state_dir = bots_dir / "state"
    logs_dir = bots_dir / "logs"
    state_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)
    monkeypatch.setattr(mc, "BOTS_DIR", bots_dir)
    monkeypatch.setattr(mc, "STATE_DIR", state_dir)
    monkeypatch.setattr(mc, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(mc, "METRICS_FILE", state_dir / "bot_metrics.json")
    monkeypatch.setattr(mc, "HISTORY_FILE", state_dir / "bot_metrics_history.jsonl")
    monkeypatch.setattr(mc, "KNOWN_BOTS", ["worker-1", "worker-2", "issues"])
    yield {"bots_dir": bots_dir, "state_dir": state_dir, "logs_dir": logs_dir}


class TestReadJson:
    def test_read_valid_json(self, isolated):
        p = isolated["state_dir"] / "test.json"
        p.write_text(json.dumps({"a": 1}))
        assert mc._read_json(p) == {"a": 1}

    def test_read_nonexistent_returns_none(self, isolated):
        assert mc._read_json(isolated["state_dir"] / "nope.json") is None

    def test_read_invalid_json_returns_none(self, isolated):
        p = isolated["state_dir"] / "bad.json"
        p.write_text("not json {{{")
        assert mc._read_json(p) is None

    def test_read_non_dict_returns_none(self, isolated):
        p = isolated["state_dir"] / "arr.json"
        p.write_text(json.dumps([1, 2, 3]))
        assert mc._read_json(p) is None

    def test_read_empty_dict(self, isolated):
        p = isolated["state_dir"] / "empty.json"
        p.write_text(json.dumps({}))
        assert mc._read_json(p) == {}


class TestCollectTokens:
    def test_fleet_tokens_empty_ledger(self, isolated):
        assert mc._collect_tokens(None) == {"fleet_prompt_actual": 0, "fleet_completion_actual": 0}

    def test_fleet_tokens_with_data(self, isolated):
        ledger = {"by_model": {"m1": {"prompt_actual": 100, "completion_actual": 50}, "m2": {"prompt_actual": 200, "completion_actual": 30}}}
        result = mc._collect_tokens(ledger)
        assert result["fleet_prompt_actual"] == 300
        assert result["fleet_completion_actual"] == 80

    def test_fleet_tokens_missing_by_model(self, isolated):
        assert mc._collect_tokens({}) == {"fleet_prompt_actual": 0, "fleet_completion_actual": 0}

    def test_fleet_tokens_non_int_handled(self, isolated):
        ledger = {"by_model": {"m1": {"prompt_actual": "bad", "completion_actual": None}}}
        result = mc._collect_tokens(ledger)
        assert result["fleet_prompt_actual"] == 0

    def test_bot_tokens_no_stream(self, isolated):
        result = mc._collect_bot_tokens("worker-1")
        assert result["total_tokens"] == 0
        assert result["source"] == "none"

    def test_bot_tokens_with_usage(self, isolated):
        isolated["logs_dir"].mkdir(exist_ok=True)
        (isolated["logs_dir"] / "worker-1.stream.json").write_text(json.dumps({
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            "model": "gpt-4",
            "api_calls": 3,
        }))
        result = mc._collect_bot_tokens("worker-1")
        assert result["prompt_tokens"] == 100
        assert result["completion_tokens"] == 50
        assert result["total_tokens"] == 150
        assert result["model"] == "gpt-4"
        assert result["api_calls"] == 3
        assert result["source"] == "stream_usage"

    def test_bot_tokens_char_estimate_fallback(self, isolated):
        (isolated["logs_dir"] / "worker-1.stream.json").write_text(json.dumps({
            "messages": [{"content": "a" * 400}, {"content": "b" * 400}],
        }))
        result = mc._collect_bot_tokens("worker-1")
        assert result["source"] == "char_estimate"
        assert result["total_tokens"] > 0

    def test_bot_tokens_invalid_json(self, isolated):
        (isolated["logs_dir"] / "worker-1.stream.json").write_text("bad json")
        result = mc._collect_bot_tokens("worker-1")
        assert result["total_tokens"] == 0

    def test_bot_tokens_non_dict_json(self, isolated):
        (isolated["logs_dir"] / "worker-1.stream.json").write_text(json.dumps([1, 2, 3]))
        result = mc._collect_bot_tokens("worker-1")
        assert result["total_tokens"] == 0


class TestCollectExecution:
    def test_no_checkpoint_no_events(self, isolated):
        result = mc._collect_execution("worker-1")
        assert result["runs"] == 0
        assert result["completed"] == 0
        assert result["errors"] == 0

    def test_checkpoint_completed(self, isolated):
        (isolated["state_dir"] / "worker-1.checkpoint.json").write_text(json.dumps({"reason": "completed", "scan_iteration": 5}))
        result = mc._collect_execution("worker-1")
        assert result["runs"] == 1
        assert result["completed"] == 1
        assert result["max_iterations"] == 5

    def test_checkpoint_error(self, isolated):
        (isolated["state_dir"] / "worker-1.checkpoint.json").write_text(json.dumps({"reason": "error: failed", "tool_iterations": 3}))
        result = mc._collect_execution("worker-1")
        assert result["errors"] == 1

    def test_exit_events_counted(self, isolated):
        events_dir = isolated["state_dir"] / "alignment_events"
        events_dir.mkdir()
        (events_dir / "worker-1.exit.json").write_text(json.dumps({"exit_code": 0, "exit_reason": "clean"}))
        result = mc._collect_execution("worker-1")
        assert result["runs"] == 1
        assert result["completed"] == 1

    def test_exit_event_restart(self, isolated):
        events_dir = isolated["state_dir"] / "alignment_events"
        events_dir.mkdir()
        (events_dir / "worker-1.exit.json").write_text(json.dumps({"exit_code": 1, "exit_reason": "restart needed"}))
        result = mc._collect_execution("worker-1")
        assert result["restarts"] == 1

    def test_completion_rate(self, isolated):
        (isolated["state_dir"] / "worker-1.checkpoint.json").write_text(json.dumps({"reason": "completed"}))
        events_dir = isolated["state_dir"] / "alignment_events"
        events_dir.mkdir()
        (events_dir / "worker-1.exit.json").write_text(json.dumps({"exit_code": 1}))
        result = mc._collect_execution("worker-1")
        assert result["runs"] == 2
        assert result["completion_rate"] == pytest.approx(0.5)


class TestCollectAlignment:
    def test_no_files(self, isolated):
        result = mc._collect_alignment("worker-1")
        assert result["avg_reward"] == 0.0
        assert result["trigger_active"] is False

    def test_with_rl_state(self, isolated):
        (isolated["state_dir"] / "rl_state.json").write_text(json.dumps({
            "bots": {"worker-1": {"avg_reward": 0.75, "last_reward": 0.9, "last_score": 0.8, "total_runs": 10, "epsilon": 0.1, "q_values": {"a": 1.0, "b": 0.5}}}
        }))
        result = mc._collect_alignment("worker-1")
        assert result["avg_reward"] == pytest.approx(0.75)
        assert result["epsilon"] == pytest.approx(0.1)
        assert result["q_arms"] == 2

    def test_trigger_active(self, isolated):
        trig_dir = isolated["state_dir"] / "alignment_triggers"
        trig_dir.mkdir()
        (trig_dir / "worker-1.evolve.json").write_text("{}")
        result = mc._collect_alignment("worker-1")
        assert result["trigger_active"] is True

    def test_alignment_scores_dict_entry(self, isolated):
        (isolated["state_dir"] / "alignment_scores.json").write_text(json.dumps({"worker-1": {"score": 0.9}}))
        result = mc._collect_alignment("worker-1")
        assert result["alignment_score"] == pytest.approx(0.9)

    def test_alignment_scores_numeric_entry(self, isolated):
        (isolated["state_dir"] / "alignment_scores.json").write_text(json.dumps({"worker-1": 0.85}))
        result = mc._collect_alignment("worker-1")
        assert result["alignment_score"] == pytest.approx(0.85)

    def test_alignment_scores_bots_nested(self, isolated):
        (isolated["state_dir"] / "alignment_scores.json").write_text(json.dumps({"bots": {"worker-1": {"score": 0.5}}}))
        result = mc._collect_alignment("worker-1")
        assert result["alignment_score"] == pytest.approx(0.5)


class TestCollectProgress:
    def test_no_files(self, isolated):
        result = mc._collect_progress("worker-1", time.time())
        assert result["tasklog_lines"] == 0
        assert result["scratchpad_lines"] == 0

    def test_tasklog_lines(self, isolated):
        (isolated["logs_dir"] / "worker-1.tasklog").write_text("line1\nline2\nline3")
        result = mc._collect_progress("worker-1", time.time())
        assert result["tasklog_lines"] == 3
        assert result["tasklog_last"] == "line3"

    def test_scratchpad_lines(self, isolated):
        (isolated["state_dir"] / "worker-1.scratchpad.md").write_text("a\nb\nc\nd")
        result = mc._collect_progress("worker-1", time.time())
        assert result["scratchpad_lines"] == 4

    def test_checkpoint_age(self, isolated):
        p = isolated["state_dir"] / "worker-1.checkpoint.json"
        p.write_text("{}")
        result = mc._collect_progress("worker-1", time.time())
        assert result["checkpoint_age_s"] is not None
        assert result["checkpoint_age_s"] >= 0


class TestCollectLiveness:
    def test_no_files(self, isolated):
        result = mc._collect_liveness("worker-1", time.time())
        assert result["heartbeat_age_s"] is None
        assert result["log_age_s"] is None

    def test_heartbeat_age(self, isolated):
        now = time.time()
        hb = isolated["state_dir"] / "worker-1.heartbeat"
        hb.write_text(str(now - 30))
        result = mc._collect_liveness("worker-1", now)
        assert result["heartbeat_age_s"] == pytest.approx(30, abs=1)

    def test_heartbeat_invalid_graceful(self, isolated):
        (isolated["state_dir"] / "worker-1.heartbeat").write_text("not_a_time")
        result = mc._collect_liveness("worker-1", time.time())
        assert result["heartbeat_age_s"] is None

    def test_status_current_task(self, isolated):
        (isolated["state_dir"] / "worker-1.status.json").write_text(json.dumps({"current_task": "testing", "iteration": 5, "updated_at": time.time()}))
        result = mc._collect_liveness("worker-1", time.time())
        assert result["current_task"] == "testing"
        assert result["iteration"] == 5


class TestCollectQuality:
    def test_no_measurements_no_runs(self, isolated):
        result = mc._collect_quality("worker-1", None)
        assert result["fp_rate"] == pytest.approx(0.0)
        assert result["fp_source"] == "no_runs"

    def test_with_measurements(self, isolated):
        measurements = {"false_positive_rate": {"worker-1": 0.2}, "coverage": {"worker-1": {"coverage_pct": 85}}, "error_rate": {"worker-1": 0.1}, "findings_per_scan": {"worker-1": 5}}
        result = mc._collect_quality("worker-1", measurements)
        assert result["fp_rate"] == pytest.approx(0.2)
        assert result["coverage_pct"] == 85
        assert result["fp_source"] == "measurements"

    def test_fallback_to_rl_failures(self, isolated):
        (isolated["state_dir"] / "rl_state.json").write_text(json.dumps({"bots": {"worker-1": {"failures": 2, "total_runs": 10}}}))
        result = mc._collect_quality("worker-1", None)
        assert result["fp_rate"] == pytest.approx(0.2)
        assert result["fp_source"] == "rl_failures"


class TestCollectThroughput:
    def test_no_queue(self, isolated):
        result = mc._collect_throughput("worker-1")
        assert result["claimed"] == 0
        assert result["completion_rate"] == pytest.approx(0.0)


class TestCollectEconomics:
    def test_tokens_per_completion(self, isolated):
        tok = {"total_tokens": 1000, "model": "qwen-3.8-max"}
        exe = {"completed": 2}
        result = mc._collect_economics("worker-1", tok, exe)
        assert result["tokens_per_completion"] == pytest.approx(500.0)
        assert result["est_cost_usd"] >= 0

    def test_zero_completed(self, isolated):
        tok = {"total_tokens": 100, "model": "unknown-model"}
        exe = {"completed": 0}
        result = mc._collect_economics("worker-1", tok, exe)
        assert result["tokens_per_completion"] == pytest.approx(100.0)

    def test_noop_flag(self, isolated):
        (isolated["state_dir"] / "worker-1.checkpoint.json").write_text(json.dumps({"reason": "noop: nothing to do"}))
        result = mc._collect_economics("worker-1", {"total_tokens": 0, "model": None}, {"completed": 0})
        assert result["noop_flag"] == pytest.approx(1.0)

    def test_no_noop(self, isolated):
        (isolated["state_dir"] / "worker-1.checkpoint.json").write_text(json.dumps({"reason": "completed"}))
        result = mc._collect_economics("worker-1", {"total_tokens": 0, "model": None}, {"completed": 1})
        assert result["noop_flag"] == pytest.approx(0.0)


class TestCollectAutonomy:
    def test_no_data(self, isolated):
        result = mc._collect_autonomy("worker-1", {})
        assert result["human_interventions"] == 0
        assert result["failure_streak"] == 0

    def test_human_interventions(self, isolated):
        data = [{"bot": "worker-1"}, {"bot": "other"}, {"bot": "worker-1"}]
        (isolated["state_dir"] / "human_review_queue.json").write_text(json.dumps(data))
        hq_raw = mc._read_json(isolated["state_dir"] / "human_review_queue.json")
        assert hq_raw is None
        from pathlib import Path as _P
        import json as _j
        p = isolated["state_dir"] / "human_review_queue.json"
        raw = _j.loads(p.read_text())
        assert isinstance(raw, list)
        with patch.object(mc, "_read_json", return_value=raw):
            result = mc._collect_autonomy("worker-1", {})
            assert result["human_interventions"] == 2

    def test_failure_streak(self, isolated):
        (isolated["state_dir"] / "rl_state.json").write_text(json.dumps({"bots": {"worker-1": {"consecutive_failures": 4}}}))
        result = mc._collect_autonomy("worker-1", {})
        assert result["failure_streak"] == 4
        assert result["auto_disabled"] is True

    def test_auto_disabled_false(self, isolated):
        (isolated["state_dir"] / "rl_state.json").write_text(json.dumps({"bots": {"worker-1": {"consecutive_failures": 1}}}))
        result = mc._collect_autonomy("worker-1", {})
        assert result["auto_disabled"] is False


class TestCollectLearning:
    def test_no_data(self, isolated):
        result = mc._collect_learning("worker-1", {"epsilon": None, "q_arms": 0})
        assert result["q_spread"] is None

    def test_q_spread(self, isolated):
        (isolated["state_dir"] / "rl_state.json").write_text(json.dumps({"bots": {"worker-1": {"q_values": {"a": 1.0, "b": 0.5, "c": 0.8}}}}))
        result = mc._collect_learning("worker-1", {"epsilon": 0.1, "q_arms": 3})
        assert result["q_spread"] == pytest.approx(0.5)

    def test_needs_exploration_reset_low_epsilon(self, isolated):
        result = mc._collect_learning("worker-1", {"epsilon": 0.01, "q_arms": 3})
        assert result["needs_exploration_reset"] is True

    def test_needs_exploration_reset_high_epsilon(self, isolated):
        result = mc._collect_learning("worker-1", {"epsilon": 0.2, "q_arms": 3})
        assert result["needs_exploration_reset"] is False


class TestCollectScrutiny:
    def test_no_files(self, isolated):
        result = mc._collect_scrutiny()
        assert result["flagged_count"] == 0

    def test_flagged_items(self, isolated):
        lp = isolated["state_dir"] / "scrutiny_log.jsonl"
        lp.write_text(json.dumps({"item_id": "Q-1"}) + "\n" + json.dumps({"item_id": "Q-2"}) + "\n" + json.dumps({"item_id": "Q-1"}) + "\n")
        result = mc._collect_scrutiny()
        assert result["flagged_count"] == 2
        assert "Q-1" in result["flagged_items"]

    def test_approved_ids_list(self, isolated):
        (isolated["state_dir"] / "approved_ids.json").write_text(json.dumps(["Q-1", "Q-2"]))
        result = mc._collect_scrutiny()
        assert result["human_approved_count"] == 2

    def test_approved_ids_dict(self, isolated):
        (isolated["state_dir"] / "approved_ids.json").write_text(json.dumps({"Q-1": True, "Q-2": False}))
        result = mc._collect_scrutiny()
        assert result["human_approved_count"] == 1

    def test_scrutiny_mode(self, isolated):
        result = mc._collect_scrutiny()
        assert result["mode"] == "scrutiny-never-blocks"


class TestCollectAll:
    def test_returns_expected_keys(self, isolated):
        snap = mc.collect_all()
        assert "timestamp" in snap
        assert "timestamp_human" in snap
        assert "fleet_tokens" in snap
        assert "bots" in snap
        assert "summary" in snap
        assert "scrutiny" in snap

    def test_bots_tracked(self, isolated):
        snap = mc.collect_all()
        assert snap["summary"]["bots_tracked"] == 3

    def test_each_bot_has_dimensions(self, isolated):
        snap = mc.collect_all()
        for bot, data in snap["bots"].items():
            assert "execution" in data
            assert "alignment" in data
            assert "progress" in data
            assert "liveness" in data
            assert "quality" in data
            assert "tokens" in data

    def test_only_fresh_filters_stale(self, isolated):
        now = time.time()
        for bot in ["worker-1", "worker-2"]:
            p = isolated["state_dir"] / f"{bot}.heartbeat"
            p.write_text(str(now))
        with patch("codebot.metrics_collector._get_bots_with_fresh_heartbeats", return_value=["worker-1"]):
            snap = mc.collect_all(only_fresh=True)
            assert "worker-1" in snap["bots"]

    def test_only_fresh_fallback_to_all_when_none_fresh(self, isolated):
        with patch("codebot.metrics_collector._get_bots_with_fresh_heartbeats", return_value=[]):
            snap = mc.collect_all(only_fresh=True)
            assert len(snap["bots"]) == 3

    def test_active_idle_stale_counts(self, isolated):
        now = time.time()
        (isolated["state_dir"] / "worker-1.heartbeat").write_text(str(now - 10))
        (isolated["state_dir"] / "worker-2.heartbeat").write_text(str(now - 500))
        snap = mc.collect_all()
        assert snap["summary"]["active"] == 1
        assert snap["summary"]["idle"] == 1

    def test_reward_trend_improving(self, isolated):
        (isolated["state_dir"] / "rl_state.json").write_text(json.dumps({
            "bots": {"worker-1": {"avg_reward": 0.5, "last_reward": 0.8}}
        }))
        snap = mc.collect_all()
        assert snap["bots"]["worker-1"]["reward_trend"] == "improving"

    def test_reward_trend_regressing(self, isolated):
        (isolated["state_dir"] / "rl_state.json").write_text(json.dumps({
            "bots": {"worker-1": {"avg_reward": 0.8, "last_reward": 0.5}}
        }))
        snap = mc.collect_all()
        assert snap["bots"]["worker-1"]["reward_trend"] == "regressing"

    def test_reward_trend_flat(self, isolated):
        (isolated["state_dir"] / "rl_state.json").write_text(json.dumps({
            "bots": {"worker-1": {"avg_reward": 0.5, "last_reward": 0.51}}
        }))
        snap = mc.collect_all()
        assert snap["bots"]["worker-1"]["reward_trend"] == "flat"

    def test_ledger_fields(self, isolated):
        (isolated["state_dir"] / "token_ledger.json").write_text(json.dumps({"day_utc": "2026-01-01", "total_actual": 12345, "by_model": {}}))
        snap = mc.collect_all()
        assert snap["ledger_day"] == "2026-01-01"
        assert snap["ledger_total_actual"] == 12345

    def test_timestamp_human_is_iso(self, isolated):
        snap = mc.collect_all()
        dt = datetime.fromisoformat(snap["timestamp_human"])
        assert dt.tzinfo is not None


class TestSaveSnapshot:
    def test_creates_files(self, isolated):
        snap = mc.collect_all()
        mc.save_snapshot(snap)
        assert (isolated["state_dir"] / "bot_metrics.json").exists()
        assert (isolated["state_dir"] / "bot_metrics_history.jsonl").exists()

    def test_snapshot_valid_json(self, isolated):
        snap = mc.collect_all()
        mc.save_snapshot(snap)
        data = json.loads((isolated["state_dir"] / "bot_metrics.json").read_text())
        assert "bots" in data

    def test_history_appended(self, isolated):
        snap = mc.collect_all()
        mc.save_snapshot(snap)
        mc.save_snapshot(snap)
        lines = (isolated["state_dir"] / "bot_metrics_history.jsonl").read_text().splitlines()
        assert len(lines) == 2

    def test_history_capped(self, isolated, monkeypatch):
        monkeypatch.setattr(mc, "MAX_HISTORY_LINES", 3)
        for _ in range(5):
            mc.save_snapshot(mc.collect_all())
        lines = (isolated["state_dir"] / "bot_metrics_history.jsonl").read_text().splitlines()
        assert len(lines) == 3

    def test_atomic_write(self, isolated):
        snap = mc.collect_all()
        mc.save_snapshot(snap)
        assert not (isolated["state_dir"] / "bot_metrics.json.tmp").exists()


class TestMain:
    def test_main_default(self, isolated, capsys):
        with patch.object(sys, "argv", ["metrics_collector"]):
            mc.main()
        out = capsys.readouterr().out
        assert "Bot Metrics" in out
        assert (isolated["state_dir"] / "bot_metrics.json").exists()

    def test_main_incremental_flag(self, isolated):
        with patch.object(sys, "argv", ["metrics_collector", "--incremental"]):
            orig = mc.collect_all
            called = {}
            def fake_collect(**kwargs):
                called.update(kwargs)
                return orig(only_fresh=kwargs.get("only_fresh", False))
            with patch.object(mc, "collect_all", side_effect=fake_collect):
                mc.main()
            assert called.get("only_fresh") is True

    def test_main_only_fresh_flag(self, isolated):
        with patch.object(sys, "argv", ["metrics_collector", "--only-fresh"]):
            orig = mc.collect_all
            called = {}
            def fake_collect(**kwargs):
                called.update(kwargs)
                return orig(only_fresh=kwargs.get("only_fresh", False))
            with patch.object(mc, "collect_all", side_effect=fake_collect):
                mc.main()
            assert called.get("only_fresh") is True


class TestGetBotsWithFreshHeartbeats:
    def test_fresh_and_stale(self, isolated):
        now = time.time()
        fresh = isolated["state_dir"] / "bot-a.heartbeat"
        fresh.write_text("x")
        stale = isolated["state_dir"] / "bot-b.heartbeat"
        stale.write_text("x")
        now_real = time.time()
        fresh.touch()
        old_time = now_real - 1000
        import os
        os.utime(str(stale), (old_time, old_time))
        fresh_bots = mc._get_bots_with_fresh_heartbeats(max_age_s=300)
        assert "bot-a" in fresh_bots
        assert "bot-b" not in fresh_bots

    def test_no_heartbeats(self, isolated):
        assert mc._get_bots_with_fresh_heartbeats() == []


class TestDiscoverWorkers:
    def test_discovers_from_heartbeat_files(self, isolated):
        (isolated["state_dir"] / "worker-3.heartbeat").write_text("x")
        (isolated["state_dir"] / "worker-7.heartbeat").write_text("x")
        workers = mc._discover_workers()
        assert "worker-3" in workers
        assert "worker-7" in workers

    def test_ignores_non_numeric_worker(self, isolated):
        (isolated["state_dir"] / "worker-abc.heartbeat").write_text("x")
        workers = mc._discover_workers()
        assert "worker-abc" not in workers

    def test_fallback_to_default_when_no_workers(self, isolated):
        workers = mc._discover_workers()
        assert len(workers) == 12
        assert "worker-1" in workers
