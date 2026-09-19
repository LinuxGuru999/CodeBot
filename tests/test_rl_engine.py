"""Tests for rl_engine.py — bandit selection, reward, Q-values, state persistence."""

import json
import random
import time
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import codebot.rl_engine as rl
from codebot.rl_engine import (
    DEFAULT_ALPHA,
    DEFAULT_EPSILON,
    DEFAULT_EPSILON_DECAY,
    DEFAULT_EPSILON_MIN,
    DEFAULT_Q_VALUES,
    REWARD_HISTORY_MAX,
    SCHEMA_VERSION,
    choose_pattern,
    decay_epsilon,
    ensure_bot,
    is_self_target,
    load_rl_state,
    reward_from_score,
    record_event_reward,
    save_rl_state,
    update_q_value,
)


class TestConstants:
    def test_q_values_count_is_10(self):
        assert len(DEFAULT_Q_VALUES) == 10

    def test_q_values_in_range(self):
        for v in DEFAULT_Q_VALUES.values():
            assert 0.0 <= v <= 1.0

    def test_epsilon_defaults(self):
        assert 0.0 <= DEFAULT_EPSILON <= 1.0
        assert 0.0 <= DEFAULT_EPSILON_MIN <= 1.0
        assert 0.0 <= DEFAULT_EPSILON_DECAY <= 1.0


class TestStatePersistence:
    def test_seed_when_missing(self, tmp_path):
        p = tmp_path / "rl_state.json"
        state = load_rl_state(p)
        assert state["version"] == SCHEMA_VERSION
        assert state["bots"] == {}
        assert "global" in state

    def test_save_and_load_roundtrip(self, tmp_path):
        p = tmp_path / "rl_state.json"
        state = load_rl_state(p)
        ensure_bot(state, "test_bot")
        save_rl_state(state, p)
        loaded = load_rl_state(p)
        assert "test_bot" in loaded["bots"]
        assert loaded["bots"]["test_bot"]["epsilon"] == DEFAULT_EPSILON

    def test_save_updates_timestamp(self, tmp_path):
        p = tmp_path / "rl_state.json"
        state = load_rl_state(p)
        save_rl_state(state, p)
        first = state["updated_at"]
        time.sleep(0.01)
        save_rl_state(state, p)
        assert state["updated_at"] >= first

    def test_corrupt_file_seeds_fresh(self, tmp_path):
        p = tmp_path / "rl_state.json"
        p.write_text("not json", encoding="utf-8")
        state = load_rl_state(p)
        assert state["version"] == SCHEMA_VERSION
        assert state["bots"] == {}

    def test_missing_keys_migrated(self, tmp_path):
        p = tmp_path / "rl_state.json"
        p.write_text(json.dumps({"something": 1}), encoding="utf-8")
        state = load_rl_state(p)
        assert "version" in state
        assert "bots" in state
        assert "global" in state

    def test_atomic_write_no_partial(self, tmp_path):
        p = tmp_path / "rl_state.json"
        state = load_rl_state(p)
        ensure_bot(state, "bot1")
        save_rl_state(state, p)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["version"] == SCHEMA_VERSION

    def test_ensure_bot_idempotent(self, tmp_path):
        p = tmp_path / "rl_state.json"
        state = load_rl_state(p)
        b1 = ensure_bot(state, "bot_x")
        b1["epsilon"] = 0.99
        b2 = ensure_bot(state, "bot_x")
        assert b2["epsilon"] == 0.99

    def test_ensure_bot_backfills_new_patterns(self, tmp_path):
        state = load_rl_state(tmp_path / "rl.json")
        bot = ensure_bot(state, "fresh")
        bot["q_values"].pop("add_examples")
        ensure_bot(state, "fresh")
        assert "add_examples" in bot["q_values"]

    def test_global_avg_recomputed_on_save(self, tmp_path):
        p = tmp_path / "rl_state.json"
        state = load_rl_state(p)
        state["global"] = {"total_events": 2, "total_rewards": 1.0, "avg_reward_global": 0.0}
        save_rl_state(state, p)
        assert state["global"]["avg_reward_global"] == pytest.approx(0.5)


class TestChoosePattern:
    def test_exploit_picks_max_q(self):
        bot_state = {
            "epsilon": 0.0,
            "q_values": {"a": 0.9, "b": 0.5, "c": 0.7},
            "q_counts": {},
        }
        pattern, strategy = choose_pattern(bot_state)
        assert pattern == "a"
        assert strategy == "exploit"

    def test_explore_random(self):
        bot_state = {
            "epsilon": 1.0,
            "q_values": {"a": 0.9, "b": 0.1},
            "q_counts": {},
        }
        results = {choose_pattern(bot_state)[0] for _ in range(20)}
        assert "a" in results or "b" in results

    def test_exploit_tie_broken_by_counts(self):
        bot_state = {
            "epsilon": 0.0,
            "q_values": {"a": 0.5, "b": 0.5},
            "q_counts": {"a": 10, "b": 0},
        }
        pattern, _ = choose_pattern(bot_state)
        assert pattern == "b"

    def test_empty_q_values_falls_back(self):
        bot_state = {"epsilon": 0.0, "q_values": {}, "q_counts": {}}
        pattern, strategy = choose_pattern(bot_state)
        assert pattern in DEFAULT_Q_VALUES
        assert strategy == "exploit"

    def test_candidate_q_overrides(self):
        bot_state = {"epsilon": 0.0, "q_values": {"a": 0.1}, "q_counts": {}}
        candidate = {"x": 0.99, "y": 0.01}
        pattern, _ = choose_pattern(bot_state, candidate_q=candidate)
        assert pattern == "x"

    def test_epsilon_zero_always_exploit(self):
        bot_state = {"epsilon": 0.0, "q_values": {"a": 0.1, "b": 0.9}, "q_counts": {}}
        for _ in range(5):
            _, strat = choose_pattern(bot_state)
            assert strat == "exploit"

    def test_epsilon_one_always_explore(self):
        bot_state = {"epsilon": 1.0, "q_values": {"a": 0.1, "b": 0.9}, "q_counts": {}}
        for _ in range(5):
            _, strat = choose_pattern(bot_state)
            assert strat == "explore"


class TestUpdateQValue:
    def test_basic_update(self):
        bot_state = {"q_values": {"a": 0.5}, "q_counts": {}, "alpha": 0.5}
        new_q = update_q_value(bot_state, "a", 1.0, alpha=0.5)
        assert new_q == pytest.approx(0.75)
        assert bot_state["q_counts"]["a"] == 1

    def test_clamped_to_range(self):
        bot_state = {"q_values": {"a": 0.9}, "q_counts": {}, "alpha": 1.0}
        new_q = update_q_value(bot_state, "a", 5.0)
        assert 0.0 <= new_q <= 1.0
        new_q2 = update_q_value(bot_state, "a", -5.0, alpha=1.0)
        assert 0.0 <= new_q2 <= 1.0

    def test_new_pattern_defaults_to_0_5(self):
        bot_state = {"q_values": {}, "q_counts": {}, "alpha": 0.5}
        new_q = update_q_value(bot_state, "unknown_pat", 0.8, alpha=0.5)
        assert new_q == pytest.approx(0.65)

    def test_counts_increment(self):
        bot_state = {"q_values": {"a": 0.5}, "q_counts": {"a": 5}, "alpha": 0.2}
        update_q_value(bot_state, "a", 0.6)
        assert bot_state["q_counts"]["a"] == 6

    def test_alpha_from_state(self):
        bot_state = {"q_values": {"a": 0.5}, "q_counts": {}, "alpha": 0.2}
        new_q = update_q_value(bot_state, "a", 1.0)
        assert new_q == pytest.approx(0.6)

    def test_rounded_to_4_decimals(self):
        bot_state = {"q_values": {"a": 0.333333}, "q_counts": {}, "alpha": 0.33}
        new_q = update_q_value(bot_state, "a", 0.777777)
        assert new_q == round(new_q, 4)


class TestDecayEpsilon:
    def test_no_prev_no_change(self):
        bot_state = {"epsilon": 0.3, "epsilon_min": 0.05, "epsilon_decay": 0.995}
        eps = decay_epsilon(bot_state, 0.5, None)
        assert eps == pytest.approx(0.3)

    def test_improvement_decays(self):
        bot_state = {"epsilon": 0.3, "epsilon_min": 0.05, "epsilon_decay": 0.995}
        decay_epsilon(bot_state, 0.8, 0.5)
        assert bot_state["epsilon"] < 0.3

    def test_regression_increases(self):
        bot_state = {"epsilon": 0.3, "epsilon_min": 0.05, "epsilon_decay": 0.995}
        decay_epsilon(bot_state, 0.2, 0.5)
        assert bot_state["epsilon"] > 0.3

    def test_small_delta_slight_decay(self):
        bot_state = {"epsilon": 0.3, "epsilon_min": 0.05, "epsilon_decay": 0.995}
        decay_epsilon(bot_state, 0.52, 0.5)
        assert bot_state["epsilon"] <= 0.3
        assert bot_state["epsilon"] >= 0.05

    def test_never_below_min(self):
        bot_state = {"epsilon": 0.06, "epsilon_min": 0.05, "epsilon_decay": 0.5}
        decay_epsilon(bot_state, 0.9, 0.1)
        assert bot_state["epsilon"] >= 0.05

    def test_never_above_half_on_increase(self):
        bot_state = {"epsilon": 0.49, "epsilon_min": 0.05, "epsilon_decay": 0.995}
        decay_epsilon(bot_state, 0.0, 0.5)
        assert bot_state["epsilon"] <= 0.5


class TestRewardFromScore:
    def test_perfect_score_near_one(self):
        r = reward_from_score(100)
        assert 0.9 <= r <= 1.0

    def test_zero_score_near_zero(self):
        r = reward_from_score(0)
        assert 0.0 <= r <= 0.1

    def test_stuck_penalty(self):
        r_clean = reward_from_score(80, exit_reason="clean")
        r_stuck = reward_from_score(80, exit_reason="stuck")
        assert r_stuck < r_clean

    def test_rebellion_penalty(self):
        r_ok = reward_from_score(80, rebellion_count=0)
        r_rebel = reward_from_score(80, rebellion_count=1)
        assert r_rebel < r_ok

    def test_bounded_0_1(self):
        for score in [-10, 0, 50, 100, 200]:
            r = reward_from_score(score)
            assert 0.0 <= r <= 1.0

    def test_differential_with_baseline(self):
        r = reward_from_score(90, bot_avg_reward=0.5)
        assert 0.0 <= r <= 1.0
        r2 = reward_from_score(40, bot_avg_reward=0.5)
        assert r2 < r

    def test_metrics_auto_disabled_zeroes(self):
        r = reward_from_score(90, metrics={"autonomy": {"auto_disabled": True}})
        assert r == 0.0

    def test_metrics_build_gate_penalty(self):
        r_pass = reward_from_score(80, metrics={"output_quality": {"build_gate_pass": True}})
        r_fail = reward_from_score(80, metrics={"output_quality": {"build_gate_pass": False}})
        assert r_fail < r_pass


class TestRecordEventReward:
    def test_basic_record(self, tmp_path):
        p = tmp_path / "rl_state.json"
        state = load_rl_state(p)
        bs = record_event_reward(state, "bot1", 0.8, 80, 0)
        assert bs["total_runs"] == 1
        assert bs["successes"] == 1
        assert bs["last_reward"] == 0.8
        assert bs["last_score"] == 80
        assert state["global"]["total_events"] == 1

    def test_failure_counted(self, tmp_path):
        state = load_rl_state(tmp_path / "rl.json")
        bs = record_event_reward(state, "bot1", 0.2, 20, 1)
        assert bs["failures"] == 1

    def test_stuck_counts_as_failure(self, tmp_path):
        state = load_rl_state(tmp_path / "rl.json")
        bs = record_event_reward(state, "bot1", 0.2, 20, 0, exit_reason="stuck")
        assert bs["failures"] == 1

    def test_reward_history_capped(self, tmp_path):
        state = load_rl_state(tmp_path / "rl.json")
        bot = ensure_bot(state, "bot1")
        bot["reward_history_max"] = 5
        for i in range(10):
            record_event_reward(state, "bot1", 0.5, 50, 0)
        assert len(bot["reward_history"]) == 5

    def test_ema_avg_reward(self, tmp_path):
        state = load_rl_state(tmp_path / "rl.json")
        record_event_reward(state, "bot1", 1.0, 100, 0)
        assert state["bots"]["bot1"]["avg_reward"] == pytest.approx(1.0)
        record_event_reward(state, "bot1", 0.0, 0, 1)
        assert 0.0 < state["bots"]["bot1"]["avg_reward"] < 1.0

    def test_consecutive_success_streak(self, tmp_path):
        state = load_rl_state(tmp_path / "rl.json")
        record_event_reward(state, "bot1", 0.9, 90, 0)
        assert state["bots"]["bot1"]["consecutive_successes"] == 1
        record_event_reward(state, "bot1", 0.9, 90, 0)
        assert state["bots"]["bot1"]["consecutive_successes"] == 2

    def test_consecutive_failure_streak(self, tmp_path):
        state = load_rl_state(tmp_path / "rl.json")
        record_event_reward(state, "bot1", 0.1, 10, 1)
        assert state["bots"]["bot1"]["consecutive_failures"] == 1

    def test_mid_reward_resets_streaks(self, tmp_path):
        state = load_rl_state(tmp_path / "rl.json")
        record_event_reward(state, "bot1", 0.9, 90, 0)
        record_event_reward(state, "bot1", 0.65, 65, 0)
        assert state["bots"]["bot1"]["consecutive_successes"] == 0
        assert state["bots"]["bot1"]["consecutive_failures"] == 0


class TestIsSelfTarget:
    def test_self_bot(self):
        assert is_self_target("prompt_optimizer") is True

    def test_self_prompt_file(self):
        assert is_self_target("other", "codebot/roles/prompt_optimizer.md") is True

    def test_not_self(self):
        assert is_self_target("bug_hunter") is False
        assert is_self_target("general_implementer", "codebot/roles/general_implementer.md") is False
