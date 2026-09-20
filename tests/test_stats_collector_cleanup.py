"""Tests for StatsCollector stale entry cleanup logic (CB-354A65ECE2E8)."""
import time
import pytest
from codebot.stats_collector import StatsCollector


@pytest.fixture
def collector(tmp_path):
    """Create a StatsCollector with a temporary state directory."""
    return StatsCollector(state_dir=str(tmp_path))


def test_cleanup_after_3_consecutive_successes(collector):
    """Entry removed after 3 successive successful record_calls."""
    model = "test-model"
    task = "code_gen"
    key = f"{model}:{task}"

    # Record 3 consecutive successes
    for _ in range(3):
        collector.record_call(model, task, success=True, cost=0.01, tokens_in=10, tokens_out=20)

    # Entry should be cleaned up since consecutive_successes >= 3
    stats = collector.get_stats(model_name=model, task_type=task)
    assert key not in stats, "Entry should be removed after 3 consecutive successes"


def test_cleanup_after_24h_staleness(collector):
    """Entry with last_updated > 86400s ago is removed regardless of success count."""
    model = "stale-model"
    task = "task"
    key = f"{model}:{task}"

    # Record one failure to create the entry
    collector.record_call(model, task, success=False, cost=0.0, tokens_in=0, tokens_out=0)

    # Manually backdate the entry's last_updated to 25 hours ago
    collector._cache[key]["last_updated"] = time.time() - (25 * 3600)
    collector._save()

    # Trigger cleanup directly
    removed = collector.cleanup_stale_entries(max_age_seconds=86400.0)

    assert removed == 1
    assert key not in collector.get_stats()


def test_no_cleanup_below_thresholds(collector):
    """Entries with <3 consecutive successes AND <24h age are preserved."""
    model = "active-model"
    task = "task"
    key = f"{model}:{task}"

    # Record 2 successes (below threshold of 3)
    for _ in range(2):
        collector.record_call(model, task, success=True, cost=0.01, tokens_in=10, tokens_out=20)

    # Directly call cleanup to ensure it doesn't remove this entry
    removed = collector.cleanup_stale_entries()
    assert removed == 0
    assert key in collector.get_stats()


def test_failure_resets_consecutive_counter(collector):
    """A single failure resets consecutive_successes to 0, preventing premature cleanup."""
    model = "flaky-model"
    task = "task"
    key = f"{model}:{task}"

    # Record 2 successes
    collector.record_call(model, task, success=True, cost=0.01, tokens_in=10, tokens_out=20)
    collector.record_call(model, task, success=True, cost=0.01, tokens_in=10, tokens_out=20)

    # Record a failure - should reset counter
    collector.record_call(model, task, success=False, cost=0.0, tokens_in=0, tokens_out=0)

    # Verify entry still exists and counter was reset
    stats = collector.get_stats(model_name=model, task_type=task)
    assert key in stats
    assert stats[key]["consecutive_successes"] == 0

    # Record 2 more successes - total consecutive is now 2, should NOT be cleaned
    collector.record_call(model, task, success=True, cost=0.01, tokens_in=10, tokens_out=20)
    collector.record_call(model, task, success=True, cost=0.01, tokens_in=10, tokens_out=20)

    stats = collector.get_stats(model_name=model, task_type=task)
    assert key in stats, "Entry should still exist with only 2 consecutive successes after reset"
    assert stats[key]["consecutive_successes"] == 2
