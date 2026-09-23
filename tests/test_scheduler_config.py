"""Tests for scheduler_config.py — defaults, validation, serialization."""

import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from codebot.scheduler_config import (
    BacklogConfig,
    DiscoveryConfig,
    ImplementationConfig,
    ReviewConfig,
    VerificationConfig,
    WorkerConfig,
    CostConfig,
    HysteresisConfig,
    PriorityAgingConfig,
    IntegrationConfig,
    SchedulerConfig,
    _parse_scalar,
    _parse_simple_yaml,
    load_scheduler_config,
)


class TestBacklogConfig:
    def test_defaults(self):
        c = BacklogConfig()
        assert c.low_watermark == 20
        assert c.target == 50
        assert c.high_watermark == 100

    def test_valid(self):
        BacklogConfig(low_watermark=10, target=50, high_watermark=100).validate()

    def test_invalid_low_gt_target(self):
        with pytest.raises(ValueError):
            BacklogConfig(low_watermark=60, target=50, high_watermark=100).validate()

    def test_invalid_target_gt_high(self):
        with pytest.raises(ValueError):
            BacklogConfig(low_watermark=20, target=110, high_watermark=100).validate()

    def test_zero_low_invalid(self):
        with pytest.raises(ValueError):
            BacklogConfig(low_watermark=0, target=50, high_watermark=100).validate()


class TestDiscoveryConfig:
    def test_defaults(self):
        c = DiscoveryConfig()
        assert c.minimum_slots == 2
        assert c.maximum_fraction == 0.75

    def test_valid(self):
        DiscoveryConfig(minimum_slots=1, maximum_fraction=0.5, floor_fraction=0.1).validate()

    def test_floor_gt_max_invalid(self):
        with pytest.raises(ValueError):
            DiscoveryConfig(maximum_fraction=0.3, floor_fraction=0.5).validate()

    def test_negative_minimum_invalid(self):
        with pytest.raises(ValueError):
            DiscoveryConfig(minimum_slots=-1).validate()

    def test_fraction_out_of_range(self):
        with pytest.raises(ValueError):
            DiscoveryConfig(maximum_fraction=1.5).validate()


class TestImplementationConfig:
    def test_valid(self):
        ImplementationConfig(maximum_fraction=0.5).validate()

    def test_zero_invalid(self):
        with pytest.raises(ValueError):
            ImplementationConfig(maximum_fraction=0).validate()

    def test_over_one_invalid(self):
        with pytest.raises(ValueError):
            ImplementationConfig(maximum_fraction=1.5).validate()


class TestReviewConfig:
    def test_valid(self):
        ReviewConfig(minimum_when_pending=2).validate()

    def test_negative_invalid(self):
        with pytest.raises(ValueError):
            ReviewConfig(minimum_when_pending=-1).validate()


class TestVerificationConfig:
    def test_valid(self):
        VerificationConfig(minimum_when_pending=1).validate()

    def test_negative_invalid(self):
        with pytest.raises(ValueError):
            VerificationConfig(minimum_when_pending=-1).validate()


class TestWorkerConfig:
    def test_defaults(self):
        c = WorkerConfig()
        assert c.heartbeat_timeout_seconds == 600
        assert c.lease_duration_seconds == 1800
        assert c.max_retries == 3

    def test_zero_heartbeat_invalid(self):
        with pytest.raises(ValueError):
            WorkerConfig(heartbeat_timeout_seconds=0).validate()

    def test_zero_lease_invalid(self):
        with pytest.raises(ValueError):
            WorkerConfig(lease_duration_seconds=0).validate()

    def test_zero_retries_invalid(self):
        with pytest.raises(ValueError):
            WorkerConfig(max_retries=0).validate()


class TestCostConfig:
    def test_defaults(self):
        c = CostConfig()
        assert c.hourly_limit_usd == 10.0
        assert c.daily_limit_usd == 100.0

    def test_zero_hourly_invalid(self):
        with pytest.raises(ValueError):
            CostConfig(hourly_limit_usd=0).validate()

    def test_zero_daily_invalid(self):
        with pytest.raises(ValueError):
            CostConfig(daily_limit_usd=0).validate()


class TestHysteresisConfig:
    def test_valid(self):
        HysteresisConfig(min_allocation_duration_seconds=300, shift_threshold=0.15, rolling_window_ticks=5).validate()

    def test_negative_duration_invalid(self):
        with pytest.raises(ValueError):
            HysteresisConfig(min_allocation_duration_seconds=-1).validate()

    def test_zero_threshold_invalid(self):
        with pytest.raises(ValueError):
            HysteresisConfig(shift_threshold=0).validate()

    def test_over_one_threshold_invalid(self):
        with pytest.raises(ValueError):
            HysteresisConfig(shift_threshold=1.5).validate()


class TestPriorityAgingConfig:
    def test_valid(self):
        PriorityAgingConfig(aging_rate=0.1).validate()

    def test_negative_rate_invalid(self):
        with pytest.raises(ValueError):
            PriorityAgingConfig(aging_rate=-0.1).validate()

    def test_zero_rate_valid(self):
        PriorityAgingConfig(aging_rate=0).validate()


class TestIntegrationConfig:
    def test_defaults(self):
        c = IntegrationConfig()
        assert c.max_integration_queue == 20
        assert c.rebase_required is True


class TestSchedulerConfig:
    def test_default(self):
        config = SchedulerConfig.default()
        assert config.max_slots == 200
        assert config.scheduler_interval_seconds == 30
        assert config.backlog.low_watermark == 20

    def test_validate_passes_default(self):
        SchedulerConfig().validate()

    def test_zero_slots_invalid(self):
        with pytest.raises(ValueError):
            SchedulerConfig(max_slots=0).validate()

    def test_zero_interval_invalid(self):
        with pytest.raises(ValueError):
            SchedulerConfig(scheduler_interval_seconds=0).validate()

    def test_invalid_backlog_propagates(self):
        with pytest.raises(ValueError):
            SchedulerConfig(backlog=BacklogConfig(low_watermark=100, target=50, high_watermark=200)).validate()

    def test_to_dict_roundtrip(self):
        config = SchedulerConfig.default()
        d = config.to_dict()
        config2 = SchedulerConfig.from_dict(d)
        assert config2.max_slots == config.max_slots
        assert config2.backlog.low_watermark == config.backlog.low_watermark

    def test_to_json_roundtrip(self):
        config = SchedulerConfig.default()
        raw = config.to_json()
        config2 = SchedulerConfig.from_json(raw)
        assert config2.max_slots == 200

    def test_from_dict_partial(self):
        config = SchedulerConfig.from_dict({"max_slots": 10})
        assert config.max_slots == 10
        assert config.backlog.low_watermark == 20

    def test_from_dict_nested(self):
        config = SchedulerConfig.from_dict({"backlog": {"low_watermark": 5, "target": 10, "high_watermark": 20}})
        assert config.backlog.low_watermark == 5

    def test_from_dict_validates(self):
        with pytest.raises(ValueError):
            SchedulerConfig.from_dict({"max_slots": 0})

    def test_frozen(self):
        config = SchedulerConfig.default()
        with pytest.raises((AttributeError, TypeError)):
            config.max_slots = 99  # type: ignore[misc]

    def test_custom_values(self):
        config = SchedulerConfig(max_slots=50, scheduler_interval_seconds=60)
        config.validate()
        assert config.max_slots == 50


class TestParseScalar:
    def test_bool_true(self):
        assert _parse_scalar("true") is True
        assert _parse_scalar("True") is True
        assert _parse_scalar("yes") is True

    def test_bool_false(self):
        assert _parse_scalar("false") is False
        assert _parse_scalar("no") is False

    def test_null(self):
        assert _parse_scalar("null") is None
        assert _parse_scalar("None") is None

    def test_int(self):
        assert _parse_scalar("42") == 42
        assert isinstance(_parse_scalar("42"), int)

    def test_float(self):
        assert _parse_scalar("3.14") == pytest.approx(3.14)

    def test_string(self):
        assert _parse_scalar("hello") == "hello"

    def test_quoted_string(self):
        assert _parse_scalar('"hello"') == "hello"
        assert _parse_scalar("'hello'") == "hello"


class TestParseSimpleYaml:
    def test_basic(self):
        text = "key: value\nnumber: 42\n"
        d = _parse_simple_yaml(text)
        assert d["key"] == "value"
        assert d["number"] == 42

    def test_nested(self):
        text = "outer:\n    inner: 123\n    name: hello\n"
        d = _parse_simple_yaml(text)
        assert d["outer"]["inner"] == 123
        assert d["outer"]["name"] == "hello"

    def test_comments_ignored(self):
        text = "# comment\nkey: value\n# another\n"
        d = _parse_simple_yaml(text)
        assert d["key"] == "value"
        assert len(d) == 1

    def test_empty_lines_ignored(self):
        text = "a: 1\n\nb: 2\n"
        d = _parse_simple_yaml(text)
        assert d["a"] == 1
        assert d["b"] == 2


class TestLoadSchedulerConfig:
    def test_default_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = load_scheduler_config()
        assert config.max_slots == 200

    def test_load_json_file(self, tmp_path):
        p = tmp_path / "scheduler.json"
        p.write_text(json.dumps({"max_slots": 42, "scheduler_interval_seconds": 60}), encoding="utf-8")
        config = load_scheduler_config(p)
        assert config.max_slots == 42

    def test_load_yaml_file(self, tmp_path):
        p = tmp_path / "scheduler.yaml"
        p.write_text("max_slots: 42\nscheduler_interval_seconds: 60\n", encoding="utf-8")
        config = load_scheduler_config(p)
        assert config.max_slots == 42

    def test_load_nonexistent_returns_default(self, tmp_path):
        config = load_scheduler_config(tmp_path / "missing.yaml")
        assert config.max_slots == 200
