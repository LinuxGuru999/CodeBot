"""Tests for Task 8: lifecycle efficiency rollout configuration."""

import json

from codebot.scheduler_config import (
    LifecycleEfficiencyConfig,
    SchedulerConfig,
)


class TestLifecycleEfficiencyConfig:
    def test_defaults_all_off_except_telemetry(self):
        cfg = LifecycleEfficiencyConfig()
        assert cfg.scoring_dispatch_enabled is False
        assert cfg.extended_cache_enabled is False
        assert cfg.lifecycle_telemetry_enabled is True
        assert cfg.recommendations_enabled is False
        assert cfg.auto_disable_on_quality_regression is True

    def test_validate_accepts_defaults(self):
        cfg = LifecycleEfficiencyConfig()
        cfg.validate()

    def test_validate_rejects_invalid_rework_threshold(self):
        cfg = LifecycleEfficiencyConfig(pilot_rework_rate_threshold=1.5)
        try:
            cfg.validate()
            assert False, "Should have raised ValueError"
        except ValueError:
            pass

    def test_validate_rejects_invalid_audit_threshold(self):
        cfg = LifecycleEfficiencyConfig(pilot_audit_reopen_threshold=-0.1)
        try:
            cfg.validate()
            assert False, "Should have raised ValueError"
        except ValueError:
            pass

    def test_scheduler_config_includes_efficiency(self):
        cfg = SchedulerConfig.default()
        assert hasattr(cfg, "lifecycle_efficiency")
        assert isinstance(cfg.lifecycle_efficiency, LifecycleEfficiencyConfig)
        assert cfg.lifecycle_efficiency.scoring_dispatch_enabled is False

    def test_scheduler_config_serialization_roundtrip(self):
        cfg = SchedulerConfig.default()
        d = cfg.to_dict()
        assert "lifecycle_efficiency" in d
        restored = SchedulerConfig.from_dict(d)
        assert restored.lifecycle_efficiency.scoring_dispatch_enabled == cfg.lifecycle_efficiency.scoring_dispatch_enabled
        assert restored.lifecycle_efficiency.extended_cache_enabled == cfg.lifecycle_efficiency.extended_cache_enabled

    def test_opt_in_preserves_old_behavior(self):
        cfg = SchedulerConfig.default()
        assert cfg.lifecycle_efficiency.scoring_dispatch_enabled is False
        assert cfg.lifecycle_efficiency.extended_cache_enabled is False
        assert cfg.lifecycle_efficiency.recommendations_enabled is False

    def test_custom_config_from_dict(self):
        d = {
            "max_slots": 30,
            "lifecycle_efficiency": {
                "scoring_dispatch_enabled": True,
                "extended_cache_enabled": True,
                "lifecycle_telemetry_enabled": True,
                "recommendations_enabled": True,
                "pilot_rework_rate_threshold": 0.25,
                "pilot_audit_reopen_threshold": 0.15,
                "auto_disable_on_quality_regression": True,
            },
        }
        cfg = SchedulerConfig.from_dict(d)
        assert cfg.lifecycle_efficiency.scoring_dispatch_enabled is True
        assert cfg.lifecycle_efficiency.extended_cache_enabled is True
        assert cfg.lifecycle_efficiency.pilot_rework_rate_threshold == 0.25
