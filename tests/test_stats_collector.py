"""Tests for codebot/stats_collector.py — per-model statistics collection."""
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from codebot.stats_collector import StatsCollector


@pytest.fixture
def collector(tmp_path):
    return StatsCollector(state_dir=str(tmp_path / "state"))


class TestRecordCall:
    def test_records_new_model_task(self, collector):
        collector.record_call("gpt-4", "code_generation", True, 0.05, 100, 200)
        stats = collector.get_stats(model_name="gpt-4", task_type="code_generation")
        assert "gpt-4:code_generation" in stats
        assert stats["gpt-4:code_generation"]["total_calls"] == 1
        assert stats["gpt-4:code_generation"]["successful_calls"] == 1
        assert stats["gpt-4:code_generation"]["failed_calls"] == 0

    def test_records_failed_call(self, collector):
        collector.record_call("gpt-4", "code_generation", False, 0.02, 50, 10)
        stats = collector.get_stats()
        assert stats["gpt-4:code_generation"]["failed_calls"] == 1
        assert stats["gpt-4:code_generation"]["successful_calls"] == 0

    def test_accumulates_multiple_calls(self, collector):
        collector.record_call("m", "t", True, 0.01, 10, 20)
        collector.record_call("m", "t", False, 0.02, 5, 5)
        collector.record_call("m", "t", True, 0.03, 15, 25)
        s = collector.get_stats()["m:t"]
        assert s["total_calls"] == 3
        assert s["successful_calls"] == 2
        assert s["failed_calls"] == 1
        assert s["total_cost"] == pytest.approx(0.06)
        assert s["total_tokens_in"] == 30
        assert s["total_tokens_out"] == 50

    def test_cost_and_tokens_accumulated(self, collector):
        collector.record_call("a", "b", True, 1.5, 1000, 2000)
        s = collector.get_stats()["a:b"]
        assert s["total_cost"] == pytest.approx(1.5)
        assert s["total_tokens_in"] == 1000
        assert s["total_tokens_out"] == 2000

    def test_last_updated_set(self, collector):
        before = time.time()
        collector.record_call("m", "t", True, 0.01, 1, 1)
        after = time.time()
        s = collector.get_stats()["m:t"]
        assert before <= s["last_updated"] <= after

    def test_different_models_separate_keys(self, collector):
        collector.record_call("model-a", "t", True, 0.01, 10, 10)
        collector.record_call("model-b", "t", True, 0.02, 20, 20)
        stats = collector.get_stats()
        assert "model-a:t" in stats
        assert "model-b:t" in stats

    def test_different_task_types_separate_keys(self, collector):
        collector.record_call("m", "task1", True, 0.01, 10, 10)
        collector.record_call("m", "task2", True, 0.01, 10, 10)
        stats = collector.get_stats()
        assert "m:task1" in stats
        assert "m:task2" in stats

    def test_zero_cost_and_tokens(self, collector):
        collector.record_call("m", "t", True, 0.0, 0, 0)
        s = collector.get_stats()["m:t"]
        assert s["total_cost"] == pytest.approx(0.0)
        assert s["total_tokens_in"] == 0


class TestGetStats:
    def test_get_all_no_filter(self, collector):
        collector.record_call("a", "t1", True, 0.01, 1, 1)
        collector.record_call("b", "t2", True, 0.01, 1, 1)
        stats = collector.get_stats()
        assert len(stats) == 2

    def test_filter_by_model(self, collector):
        collector.record_call("gpt-4", "t1", True, 0.01, 1, 1)
        collector.record_call("claude", "t1", True, 0.01, 1, 1)
        result = collector.get_stats(model_name="gpt-4")
        assert all(k.startswith("gpt-4:") for k in result)
        assert len(result) == 1

    def test_filter_by_task_type(self, collector):
        collector.record_call("m", "code", True, 0.01, 1, 1)
        collector.record_call("m", "review", True, 0.01, 1, 1)
        result = collector.get_stats(task_type="code")
        assert all(k.endswith(":code") for k in result)
        assert len(result) == 1

    def test_filter_by_both(self, collector):
        collector.record_call("m", "t1", True, 0.01, 1, 1)
        collector.record_call("m", "t2", True, 0.01, 1, 1)
        collector.record_call("other", "t1", True, 0.01, 1, 1)
        result = collector.get_stats(model_name="m", task_type="t1")
        assert len(result) == 1
        assert "m:t1" in result

    def test_filter_no_match_returns_empty(self, collector):
        collector.record_call("m", "t", True, 0.01, 1, 1)
        assert collector.get_stats(model_name="nonexistent") == {}

    def test_empty_collector_returns_empty(self, collector):
        assert collector.get_stats() == {}

    def test_skips_malformed_keys(self, collector):
        collector._cache["no_colon_key"] = {"total_calls": 1}
        collector._cache["good:key"] = {"total_calls": 1}
        result = collector.get_stats()
        assert "no_colon_key" not in result
        assert "good:key" in result

    def test_colon_in_task_type_handled(self, collector):
        collector.record_call("m", "t:with:colons", True, 0.01, 1, 1)
        result = collector.get_stats(task_type="t:with:colons")
        assert "m:t:with:colons" in result


class TestPersistence:
    def test_persists_to_disk(self, tmp_path):
        c1 = StatsCollector(state_dir=str(tmp_path / "state"))
        c1.record_call("m", "t", True, 0.1, 10, 20)
        c2 = StatsCollector(state_dir=str(tmp_path / "state"))
        assert "m:t" in c2.get_stats()

    def test_loads_existing_file(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "model_stats.json").write_text(json.dumps({
            "model:x": {"total_calls": 5, "successful_calls": 3, "failed_calls": 2,
                        "total_cost": 1.0, "total_tokens_in": 100, "total_tokens_out": 200, "last_updated": 123.0}
        }))
        c = StatsCollector(state_dir=str(state_dir))
        assert "model:x" in c.get_stats()
        assert c.get_stats()["model:x"]["total_calls"] == 5

    def test_corrupt_file_resets_gracefully(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "model_stats.json").write_text("not json {{{")
        c = StatsCollector(state_dir=str(state_dir))
        assert c.get_stats() == {}
        c.record_call("m", "t", True, 0.01, 1, 1)
        assert "m:t" in c.get_stats()

    def test_non_dict_file_resets(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "model_stats.json").write_text(json.dumps([1, 2, 3]))
        c = StatsCollector(state_dir=str(state_dir))
        assert c.get_stats() == {}

    def test_atomic_write_creates_file(self, collector, tmp_path):
        collector.record_call("m", "t", True, 0.01, 1, 1)
        assert (Path(collector.state_dir) / "model_stats.json").exists()
        data = json.loads((Path(collector.state_dir) / "model_stats.json").read_text())
        assert "m:t" in data

    def test_save_failure_is_fail_open(self, collector, tmp_path):
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            collector.record_call("m", "t", True, 0.01, 1, 1)
            assert "m:t" in collector.get_stats()

    def test_creates_state_dir(self, tmp_path):
        d = tmp_path / "new" / "nested" / "state"
        c = StatsCollector(state_dir=str(d))
        assert d.exists()
        c.record_call("m", "t", True, 0.01, 1, 1)
        assert (d / "model_stats.json").exists()


class TestEdgeCases:
    def test_large_number_of_models(self, collector):
        for i in range(20):
            collector.record_call(f"model-{i}", "task", True, 0.01, 10, 10)
        assert len(collector.get_stats()) == 20

    def test_special_characters_in_names(self, collector):
        collector.record_call("model/with/slash", "task-with-dash", True, 0.01, 1, 1)
        stats = collector.get_stats()
        assert any("model/with/slash" in k for k in stats)

    def test_record_overwrites_not_duplicate(self, collector):
        collector.record_call("m", "t", True, 0.01, 1, 1)
        collector.record_call("m", "t", True, 0.02, 2, 2)
        stats = collector.get_stats()
        assert len([k for k in stats if k == "m:t"]) == 1
        assert stats["m:t"]["total_calls"] == 2

    def test_get_stats_returns_reference_not_copy_isolation(self, collector):
        collector.record_call("m", "t", True, 0.01, 1, 1)
        r1 = collector.get_stats()
        r1["m:t"]["total_calls"] = 999
        r2 = collector.get_stats()
        assert r2["m:t"]["total_calls"] == 999

    def test_empty_task_type_filter(self, collector):
        collector.record_call("m", "t", True, 0.01, 1, 1)
        result = collector.get_stats(task_type="t")
        assert len(result) == 1

    def test_model_name_none_task_type_none_returns_all(self, collector):
        collector.record_call("a", "t1", True, 0.01, 1, 1)
        collector.record_call("b", "t2", True, 0.01, 1, 1)
        assert len(collector.get_stats(model_name=None, task_type=None)) == 2
