"""Tests for rl_engine.py — bandit selection, reward, Q-values, state persistence."""

import json
import random
import time
from pathlib import Path
from typing import Any

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
    award_ticket_completion_rewards,
    choose_pattern,
    decay_epsilon,
    ensure_bot,
    is_self_target,
    load_rl_state,
    reward_from_score,
    record_event_reward,
    save_rl_state,
    set_project_adapter,
    update_q_value,
)
from codebot.lifecycle_packet import LifecyclePacketStore


class _FakePaths:
    """Minimal adapter paths stub for testing."""
    def __init__(self, state_dir: Path, repository_root: Optional[Path] = None) -> None:
        self.state_dir = state_dir
        # Default repository_root to parent of state_dir if not provided,
        # assuming standard layout where state_dir is inside repo.
        # For tests using tmp_path as state_dir, we often want tmp_path to also be root
        # or have a specific structure. We'll let tests configure this explicitly if needed,
        # but defaulting to state_dir.parent is safer than hardcoding.
        # However, many existing tests pass tmp_path directly as state_dir.
        # In those cases, repository_root should probably be tmp_path itself or tmp_path.parent.
        # To maintain compatibility with existing tests that treat tmp_path as the "project root"
        # for file creation, we will default repository_root to state_dir if it looks like a temp dir,
        # OR simply require explicit passing. 
        # Safest backward compat: if repository_root is None, use state_dir.parent.
        # But wait, in TestDynamicBotDiscovery, they create roles at tmp_path/codebot/roles
        # and set BOTS_DIR = tmp_path/codebot. This implies tmp_path IS the repository root.
        # So if state_dir was tmp_path, repository_root should be tmp_path.
        # Let's make repository_root required or default sensibly.
        # Given the ticket removes hardcoded paths, tests MUST be explicit.
        # I will default to state_dir.parent to avoid breaking ALL existing tests immediately,
        # but new tests for discovery should specify it.
        self.repository_root = repository_root if repository_root is not None else state_dir.parent


class _FakeAdapter:
    """Minimal adapter stub for testing."""
    def __init__(self, state_dir: Path, repository_root: Optional[Path] = None) -> None:
        self._paths = _FakePaths(state_dir, repository_root)

    def paths(self) -> _FakePaths:
        return self._paths


@pytest.fixture(autouse=True)
def _reset_adapter():
    """Reset adapter before each test to ensure isolation."""
    rl._adapter_instance = None
    yield
    rl._adapter_instance = None


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

    def test_ticket_completion_rewards_each_participant_once(self, tmp_path):
        """award_ticket_completion_rewards should process each participant once."""
        repo_root = tmp_path / "repo"
        (repo_root / "codebot" / "roles").mkdir(parents=True)
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        set_project_adapter(_FakeAdapter(state_dir, repository_root=repo_root))
        
        packets = LifecyclePacketStore(state_dir)
        packets.record_participants("CB-1", ["implementer", "reviewer", "implementer"])

        assert award_ticket_completion_rewards("CB-1") == ["implementer", "reviewer"]
        assert award_ticket_completion_rewards("CB-1") == []

        state = load_rl_state()
        assert state["bots"]["implementer"]["total_runs"] == 1
        assert state["bots"]["reviewer"]["total_runs"] == 1
        assert state["global"]["total_events"] == 2


class TestIsSelfTarget:
    def test_self_bot(self):
        assert is_self_target("prompt_optimizer") is True

    def test_self_prompt_file(self):
        assert is_self_target("other", "codebot/roles/prompt_optimizer.md") is True

    def test_not_self(self):
        assert is_self_target("bug_hunter") is False
        assert is_self_target("general_implementer", "codebot/roles/general_implementer.md") is False


class TestScoreEventNoTicketsPenalty:
    """Tests for score_event no_tickets_penalty path in discovery roles."""

    def test_discovery_role_with_marker_applies_penalty(self, tmp_path):
        """score_event must apply 15-point penalty when no_tickets marker exists."""
        # Arrange: inject adapter so _get_state_dir() returns tmp_path
        set_project_adapter(_FakeAdapter(tmp_path))
        # Create the marker file that triggers the penalty
        marker = tmp_path / "bug_hunter.no_tickets"
        marker.write_text("no tickets found", encoding="utf-8")
        # Create required dirs/files to avoid exceptions in score_event
        (tmp_path / "logs").mkdir(exist_ok=True)
        event = {
            "bot": "bug_hunter",
            "exit_code": 0,
            "exit_reason": "clean",
            "stream_path": "logs/bug_hunter.stream.json",
            "log_path": "logs/bug_hunter.log",
        }
        # Act
        result = rl.score_event(event)
        # Assert: penalty was applied and reflected in evidence
        assert "no_tickets_pen=15" in result["evidence"]
        # Score should be reduced by 15 compared to a run without marker
        assert result["score"] <= 85  # max possible minus 15 penalty

    def test_score_event_idempotent_no_unlink_side_effect(self, tmp_path):
        """Scoring twice must apply penalty both times — no unlink side effect allowed."""
        set_project_adapter(_FakeAdapter(tmp_path))
        marker = tmp_path / "security_auditor.no_tickets"
        marker.write_text("marker", encoding="utf-8")
        (tmp_path / "logs").mkdir(exist_ok=True)
        event = {
            "bot": "security_auditor",
            "exit_code": 0,
            "exit_reason": "clean",
            "stream_path": "logs/security_auditor.stream.json",
            "log_path": "logs/security_auditor.log",
        }
        # First scoring
        result1 = rl.score_event(event)
        # Marker must still exist after first call (no unlink side effect)
        assert marker.exists(), "score_event must not delete the no_tickets marker file"
        # Second scoring must produce identical penalty
        result2 = rl.score_event(event)
        assert "no_tickets_pen=15" in result1["evidence"]
        assert "no_tickets_pen=15" in result2["evidence"]
        assert result1["score"] == result2["score"]

    def test_non_discovery_role_no_penalty(self, tmp_path):
        """Non-discovery roles (e.g., general_implementer) must not get no_tickets penalty."""
        set_project_adapter(_FakeAdapter(tmp_path))
        # Even if marker exists, non-discovery role should ignore it
        marker = tmp_path / "general_implementer.no_tickets"
        marker.write_text("marker", encoding="utf-8")
        (tmp_path / "logs").mkdir(exist_ok=True)
        event = {
            "bot": "general_implementer",
            "exit_code": 0,
            "exit_reason": "clean",
            "stream_path": "logs/general_implementer.stream.json",
            "log_path": "logs/general_implementer.log",
        }
        result = rl.score_event(event)
        assert "no_tickets_pen=0" in result["evidence"]


class TestAdapterInjectionRequired:
    """Verify that state-accessing functions raise RuntimeError without adapter."""

    def test_load_rl_state_raises_without_adapter(self):
        """load_rl_state() with no explicit path must raise when adapter not injected."""
        with pytest.raises(RuntimeError, match="Project adapter not injected"):
            load_rl_state()

    def test_save_rl_state_raises_without_adapter(self):
        """save_rl_state() with no explicit path must raise when adapter not injected."""
        state = {"version": SCHEMA_VERSION, "bots": {}, "global": {}}
        with pytest.raises(RuntimeError, match="Project adapter not injected"):
            save_rl_state(state)

    def test_list_pending_events_raises_without_adapter(self):
        """list_pending_events() with no explicit dir must raise when adapter not injected."""
        with pytest.raises(RuntimeError, match="Project adapter not injected"):
            rl.list_pending_events()

    def test_write_trigger_raises_without_adapter(self):
        """write_trigger() must raise when adapter not injected."""
        with pytest.raises(RuntimeError, match="Project adapter not injected"):
            rl.write_trigger(
                bot_name="test",
                score=50,
                reward=0.5,
                verdict="aligned",
                reason="test",
                breakdown={},
                event={},
                bot_state={},
            )

    def test_score_event_raises_without_adapter(self):
        """score_event() accesses _get_state_dir() internally; must raise without adapter."""
        event = {
            "bot": "bug_hunter",
            "exit_code": 0,
            "exit_reason": "clean",
        }
        with pytest.raises(RuntimeError, match="Project adapter not injected"):
            rl.score_event(event)

    def test_explicit_path_bypasses_adapter_requirement(self, tmp_path):
        """Passing explicit path to load_rl_state should work without adapter."""
        p = tmp_path / "rl_state.json"
        state = load_rl_state(p)
        assert state["version"] == SCHEMA_VERSION

    def test_explicit_path_save_bypasses_adapter(self, tmp_path):
        """Passing explicit path to save_rl_state should work without adapter."""
        p = tmp_path / "rl_state.json"
        state = {"version": SCHEMA_VERSION, "bots": {}, "global": {"total_events": 0, "total_rewards": 0.0, "avg_reward_global": 0.0}}
        save_rl_state(state, p)
        assert p.exists()


class TestAdapterPathsUsedAfterInjection:
    """Verify all I/O uses adapter.paths().state_dir after injection."""

    def test_load_rl_state_uses_adapter_path(self, tmp_path):
        """After adapter injection, load_rl_state() reads from adapter's state_dir."""
        set_project_adapter(_FakeAdapter(tmp_path))
        # Create a state file in the adapter's state_dir
        state_file = tmp_path / "rl_state.json"
        state_file.write_text(json.dumps({
            "version": SCHEMA_VERSION,
            "bots": {"injected_bot": {}},
            "global": {"total_events": 0, "total_rewards": 0.0, "avg_reward_global": 0.0},
        }), encoding="utf-8")
        state = load_rl_state()
        assert "injected_bot" in state["bots"]

    def test_save_rl_state_uses_adapter_path(self, tmp_path):
        """After adapter injection, save_rl_state() writes to adapter's state_dir."""
        set_project_adapter(_FakeAdapter(tmp_path))
        state = load_rl_state()
        ensure_bot(state, "adapter_test_bot")
        save_rl_state(state)
        assert (tmp_path / "rl_state.json").exists()
        loaded = json.loads((tmp_path / "rl_state.json").read_text(encoding="utf-8"))
        assert "adapter_test_bot" in loaded["bots"]

    def test_list_pending_events_uses_adapter_events_dir(self, tmp_path):
        """After adapter injection, list_pending_events() reads from adapter's events dir."""
        set_project_adapter(_FakeAdapter(tmp_path))
        events_dir = tmp_path / "alignment_events"
        events_dir.mkdir(parents=True)
        event_file = events_dir / "test.exit.json"
        event_file.write_text(json.dumps({
            "exit_reason": "clean",
            "processed": False,
            "exit_time": time.time(),
        }), encoding="utf-8")
        pending = rl.list_pending_events()
        assert len(pending) == 1
        assert pending[0][0].name == "test.exit.json"

    def test_write_trigger_uses_adapter_triggers_dir(self, tmp_path):
        """After adapter injection, write_trigger() writes to adapter's triggers dir."""
        set_project_adapter(_FakeAdapter(tmp_path))
        path = rl.write_trigger(
            bot_name="test_bot",
            score=70,
            reward=0.7,
            verdict="aligned",
            reason="test",
            breakdown={"on_task": 30},
            event={"exit_code": 0},
            bot_state={"epsilon": 0.3, "q_values": {}, "total_runs": 1, "consecutive_failures": 0},
        )
        assert path.parent == tmp_path / "alignment_triggers"
        assert path.exists()


class TestDynamicBotDiscovery:
    """Tests for dynamic bot prompt discovery and refresh functionality.
    
    All tests must inject an adapter since hardcoded paths are removed.
    """

    def test_refresh_bot_prompts_requires_adapter(self):
        """refresh_bot_prompts() must raise RuntimeError without adapter."""
        with pytest.raises(RuntimeError, match="Project adapter not injected"):
            rl.refresh_bot_prompts()

    def test_discover_bot_prompts_requires_adapter(self):
        """_discover_bot_prompts() must raise RuntimeError without adapter."""
        with pytest.raises(RuntimeError, match="Project adapter not injected"):
            rl._discover_bot_prompts()

    def test_refresh_bot_prompts_updates_global_map(self, tmp_path):
        """refresh_bot_prompts() should update the global BOT_PROMPT_MAP using adapter."""
        # Create a roles directory structure relative to repository_root
        repo_root = tmp_path / "repo"
        roles_dir = repo_root / "codebot" / "roles"
        roles_dir.mkdir(parents=True)
        new_bot_file = roles_dir / "new_bot.md"
        new_bot_file.write_text("# New Bot Prompt", encoding="utf-8")

        # Inject adapter with explicit repository_root
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        set_project_adapter(_FakeAdapter(state_dir, repository_root=repo_root))

        # Refresh should discover the new bot
        updated_map = rl.refresh_bot_prompts()
        assert "new_bot" in updated_map
        assert updated_map["new_bot"] == "codebot/roles/new_bot.md"
        assert "new_bot" in rl.BOT_PROMPT_MAP

    def test_get_prompt_file_cached_bot(self, tmp_path):
        """_get_prompt_file should return cached path for known bots."""
        # Setup adapter and pre-populate cache
        repo_root = tmp_path / "repo"
        (repo_root / "codebot" / "roles").mkdir(parents=True)
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        set_project_adapter(_FakeAdapter(state_dir, repository_root=repo_root))
        
        # Pre-populate cache
        rl.BOT_PROMPT_MAP["bug_hunter"] = "codebot/roles/bug_hunter.md"
        
        path = rl._get_prompt_file("bug_hunter")
        assert path == "codebot/roles/bug_hunter.md"

    def test_get_prompt_file_hyphenated_bot_base_match(self, tmp_path):
        """_get_prompt_file should handle hyphenated bot names by trying base name."""
        repo_root = tmp_path / "repo"
        (repo_root / "codebot" / "roles").mkdir(parents=True)
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        set_project_adapter(_FakeAdapter(state_dir, repository_root=repo_root))
        
        # Pre-populate cache with base name
        rl.BOT_PROMPT_MAP["implementer"] = "codebot/roles/implementer.md"
        
        # implementer-CB-91447 should fall back to implementer
        path = rl._get_prompt_file("implementer-CB-91447")
        assert path == "codebot/roles/implementer.md"

    def test_get_prompt_file_on_demand_discovery(self, tmp_path):
        """_get_prompt_file should discover new bots on-demand via adapter."""
        repo_root = tmp_path / "repo"
        roles_dir = repo_root / "codebot" / "roles"
        roles_dir.mkdir(parents=True)
        new_bot_file = roles_dir / "dynamic_bot.md"
        new_bot_file.write_text("# Dynamic Bot", encoding="utf-8")

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        set_project_adapter(_FakeAdapter(state_dir, repository_root=repo_root))

        # Clear the cache to simulate a bot added after import
        rl.BOT_PROMPT_MAP = {}

        # Should discover the new bot on-demand
        path = rl._get_prompt_file("dynamic_bot")
        assert path == "codebot/roles/dynamic_bot.md"

    def test_get_prompt_file_on_demand_hyphenated(self, tmp_path):
        """_get_prompt_file should discover hyphenated bots by base name on-demand."""
        repo_root = tmp_path / "repo"
        roles_dir = repo_root / "codebot" / "roles"
        roles_dir.mkdir(parents=True)
        base_bot_file = roles_dir / "testbot.md"
        base_bot_file.write_text("# Test Bot", encoding="utf-8")

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        set_project_adapter(_FakeAdapter(state_dir, repository_root=repo_root))

        rl.BOT_PROMPT_MAP = {}
        path = rl._get_prompt_file("testbot-CB-123")
        assert path == "codebot/roles/testbot.md"

    def test_get_prompt_file_fallback_path(self, tmp_path):
        """_get_prompt_file should return constructed path for non-existent bots."""
        repo_root = tmp_path / "repo"
        (repo_root / "codebot" / "roles").mkdir(parents=True)
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        set_project_adapter(_FakeAdapter(state_dir, repository_root=repo_root))
        
        path = rl._get_prompt_file("nonexistent_bot_xyz")
        assert path == "codebot/roles/nonexistent_bot_xyz.md"

    def test_get_prompt_file_self_bot_unchanged(self, tmp_path):
        """_get_prompt_file should still work for prompt_optimizer (self bot)."""
        repo_root = tmp_path / "repo"
        (repo_root / "codebot" / "roles").mkdir(parents=True)
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        set_project_adapter(_FakeAdapter(state_dir, repository_root=repo_root))
        
        # Pre-populate cache
        rl.BOT_PROMPT_MAP["prompt_optimizer"] = "codebot/roles/prompt_optimizer.md"
        
        path = rl._get_prompt_file("prompt_optimizer")
        assert path == "codebot/roles/prompt_optimizer.md"


class TestHelperFunctions:
    """Tests for internal helper functions to achieve 100% coverage."""

    def test_get_roles_dir_returns_correct_path(self, tmp_path):
        """_get_roles_dir() should return repository_root/codebot/roles."""
        repo_root = tmp_path / "repo"
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        set_project_adapter(_FakeAdapter(state_dir, repository_root=repo_root))
        
        roles_dir = rl._get_roles_dir()
        assert roles_dir == repo_root / "codebot" / "roles"

    def test_list_pending_events_empty_when_no_dir(self, tmp_path):
        """list_pending_events() should return empty list when events dir doesn't exist."""
        set_project_adapter(_FakeAdapter(tmp_path))
        pending = rl.list_pending_events()
        assert pending == []

    def test_list_pending_events_skips_processed(self, tmp_path):
        """list_pending_events() should skip already processed events."""
        set_project_adapter(_FakeAdapter(tmp_path))
        events_dir = tmp_path / "alignment_events"
        events_dir.mkdir()
        # Processed event
        (events_dir / "processed.exit.json").write_text(
            json.dumps({"processed": True}), encoding="utf-8"
        )
        # Unprocessed event
        (events_dir / "pending.exit.json").write_text(
            json.dumps({"processed": False, "exit_reason": "clean"}), encoding="utf-8"
        )
        pending = rl.list_pending_events()
        assert len(pending) == 1
        assert pending[0][0].name == "pending.exit.json"

    def test_list_pending_events_handles_corrupt_json(self, tmp_path):
        """list_pending_events() should skip corrupt JSON files."""
        set_project_adapter(_FakeAdapter(tmp_path))
        events_dir = tmp_path / "alignment_events"
        events_dir.mkdir()
        (events_dir / "corrupt.exit.json").write_text("not json", encoding="utf-8")
        (events_dir / "valid.exit.json").write_text(
            json.dumps({"processed": False}), encoding="utf-8"
        )
        pending = rl.list_pending_events()
        assert len(pending) == 1

    def test_write_trigger_creates_triggers_dir(self, tmp_path):
        """write_trigger() should create triggers dir if it doesn't exist."""
        set_project_adapter(_FakeAdapter(tmp_path))
        # Ensure triggers dir doesn't exist yet
        triggers_dir = tmp_path / "alignment_triggers"
        assert not triggers_dir.exists()
        
        rl.write_trigger(
            bot_name="test",
            score=50,
            reward=0.5,
            verdict="aligned",
            reason="test",
            breakdown={},
            event={},
            bot_state={},
        )
        assert triggers_dir.exists()

    def test_discover_bot_prompts_empty_when_no_roles_dir(self, tmp_path):
        """_discover_bot_prompts() should return empty dict when roles dir doesn't exist."""
        repo_root = tmp_path / "repo"
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # Don't create codebot/roles
        set_project_adapter(_FakeAdapter(state_dir, repository_root=repo_root))
        
        result = rl._discover_bot_prompts()
        assert result == {}

    def test_award_ticket_completion_rewards_no_participants(self, tmp_path):
        """award_ticket_completion_rewards() should return empty list when no participants."""
        repo_root = tmp_path / "repo"
        (repo_root / "codebot" / "roles").mkdir(parents=True)
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        set_project_adapter(_FakeAdapter(state_dir, repository_root=repo_root))
        
        # Create a packet store and record empty participants
        packets = LifecyclePacketStore(state_dir)
        packets.record_participants("CB-EMPTY", [])
        
        result = rl.award_ticket_completion_rewards("CB-EMPTY")
        assert result == []

    def test_metrics_signals_path(self, tmp_path):
        """_metrics_signals_path() should return state_dir/bot_metrics.json."""
        set_project_adapter(_FakeAdapter(tmp_path))
        path = rl._metrics_signals_path()
        assert path == tmp_path / "bot_metrics.json"

    def test_check_output_exists_finds_first_match(self, tmp_path):
        """_check_output_exists() should return first existing candidate."""
        set_project_adapter(_FakeAdapter(tmp_path))
        (tmp_path / "second.txt").write_text("content", encoding="utf-8")
        (tmp_path / "third.txt").write_text("content", encoding="utf-8")
        
        result = rl._check_output_exists(["first.txt", "second.txt", "third.txt"])
        assert result == tmp_path / "second.txt"

    def test_check_output_exists_returns_none_when_missing(self, tmp_path):
        """_check_output_exists() should return None when no candidates exist."""
        set_project_adapter(_FakeAdapter(tmp_path))
        result = rl._check_output_exists(["missing1.txt", "missing2.txt"])
        assert result is None

    def test_resolve_checkpoint_path(self, tmp_path):
        """_resolve_checkpoint_path() should return state_dir/checkpoint_name."""
        set_project_adapter(_FakeAdapter(tmp_path))
        path = rl._resolve_checkpoint_path("my_checkpoint.json")
        assert path == tmp_path / "my_checkpoint.json"

    def test_award_ticket_completion_rewards_with_participants(self, tmp_path):
        """award_ticket_completion_rewards() should update RL state for participants."""
        repo_root = tmp_path / "repo"
        (repo_root / "codebot" / "roles").mkdir(parents=True)
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        set_project_adapter(_FakeAdapter(state_dir, repository_root=repo_root))
        
        packets = LifecyclePacketStore(state_dir)
        packets.record_participants("CB-TEST", ["implementer"])
        
        result = rl.award_ticket_completion_rewards("CB-TEST")
        assert result == ["implementer"]
        
        # Verify RL state was updated
        state = rl.load_rl_state()
        assert state["bots"]["implementer"]["total_runs"] >= 1
