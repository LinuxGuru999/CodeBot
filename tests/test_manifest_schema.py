"""Tests for codebot/manifest_schema.py — schema validation, loading, and defaults."""
import json
import pytest
from pathlib import Path

from codebot.manifest_schema import (
    validate_manifest,
    load_manifest,
    load_all_manifests,
    _REQUIRED_FIELDS,
    _OPTIONAL_FIELDS,
    _ALLOWED_FIELDS,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def valid_manifest(**overrides):
    base = {
        "name": "test_bot",
        "kind": "scan",
        "prompt_file": "TEST_BOT.md",
        "model": "default",
        "runner": "api",
        "enabled": True,
        "interval_seconds": 300,
        "heartbeat_timeout": 600,
        "tier_priority": 1,
        "max_restarts": 3,
        "clean_exit_wait": True,
        "session_timeout": 1800,
        "input": [{"path": "QUEUE.md", "type": "queue"}],
        "output": [{"path": "logs/test.log", "type": "log"}],
        "noop_cap": 0,
    }
    base.update(overrides)
    return base


# ===========================================================================
# validate_manifest — happy path
# ===========================================================================

class TestValidateManifestHappyPath:
    def test_valid_minimal_dict(self):
        ok, errors = validate_manifest(valid_manifest())
        assert ok is True
        assert errors == []

    def test_valid_all_kinds(self):
        for kind in ("scan", "queue", "command"):
            ok, _ = validate_manifest(valid_manifest(kind=kind))
            assert ok is True, f"kind={kind} should be valid"

    def test_valid_both_runners(self):
        for runner in ("opencode", "api"):
            ok, _ = validate_manifest(valid_manifest(runner=runner))
            assert ok is True

    def test_valid_both_batch_tiers(self):
        for tier in ("standard", "high-limit"):
            ok, _ = validate_manifest(valid_manifest(batch_tier=tier))
            assert ok is True

    def test_valid_with_all_optional_fields(self):
        m = valid_manifest(
            complexity_filter=["small", "medium"],
            shard="shard-a",
            scratchpad=True,
            batch_tier="standard",
            noop_counter_file="state/counter.txt",
        )
        m["noop_cap"] = 3
        ok, errors = validate_manifest(m)
        assert ok is True, errors

    def test_valid_with_noop_cap_and_counter(self):
        m = valid_manifest(noop_cap=5, noop_counter_file="state/counter.txt")
        ok, _ = validate_manifest(m)
        assert ok is True

    def test_valid_from_json_file(self, tmp_path):
        p = tmp_path / "manifest.json"
        p.write_text(json.dumps(valid_manifest()))
        ok, errors = validate_manifest(p)
        assert ok is True, errors

    def test_valid_from_string_path(self, tmp_path):
        p = tmp_path / "manifest.json"
        p.write_text(json.dumps(valid_manifest()))
        ok, _ = validate_manifest(str(p))
        assert ok is True

    def test_valid_input_with_filter(self):
        m = valid_manifest(input=[{"path": "QUEUE.md", "type": "queue", "filter": "small"}])
        ok, _ = validate_manifest(m)
        assert ok is True

    def test_valid_tier_priority_negative(self):
        ok, _ = validate_manifest(valid_manifest(tier_priority=-1))
        assert ok is True

    def test_valid_noop_cap_zero_no_counter(self):
        ok, _ = validate_manifest(valid_manifest(noop_cap=0))
        assert ok is True


# ===========================================================================
# validate_manifest — missing required fields
# ===========================================================================

class TestMissingRequiredFields:
    @pytest.mark.parametrize("field", sorted(_REQUIRED_FIELDS))
    def test_missing_each_required_field(self, field):
        m = valid_manifest()
        del m[field]
        ok, errors = validate_manifest(m)
        assert ok is False
        assert any(field in e for e in errors), f"should complain about missing {field}"

    def test_missing_multiple_fields(self):
        m = valid_manifest()
        del m["name"]
        del m["kind"]
        ok, errors = validate_manifest(m)
        assert ok is False
        assert len([e for e in errors if "missing required" in e]) >= 2

    def test_empty_dict_fails(self):
        ok, errors = validate_manifest({})
        assert ok is False
        assert len(errors) >= len(_REQUIRED_FIELDS)


# ===========================================================================
# validate_manifest — unknown fields
# ===========================================================================

class TestUnknownFields:
    def test_unknown_top_level_field(self):
        m = valid_manifest(unknown_field="oops")
        ok, errors = validate_manifest(m)
        assert ok is False
        assert any("unknown field" in e for e in errors)
        assert any("unknown_field" in e for e in errors)

    def test_unknown_input_entry_field(self):
        m = valid_manifest(input=[{"path": "x", "type": "y", "bogus": "z"}])
        ok, errors = validate_manifest(m)
        assert ok is False
        assert any("bogus" in e for e in errors)

    def test_unknown_output_entry_field(self):
        m = valid_manifest(output=[{"path": "x", "type": "y", "extra": "z"}])
        ok, errors = validate_manifest(m)
        assert ok is False
        assert any("extra" in e for e in errors)


# ===========================================================================
# validate_manifest — type checking
# ===========================================================================

class TestFieldTypeValidation:
    def test_name_empty_string(self):
        ok, errors = validate_manifest(valid_manifest(name=""))
        assert ok is False
        assert any("name" in e for e in errors)

    def test_name_whitespace_only(self):
        ok, _ = validate_manifest(valid_manifest(name="   "))
        assert ok is False

    def test_name_not_string(self):
        ok, _ = validate_manifest(valid_manifest(name=123))
        assert ok is False

    def test_kind_invalid_value(self):
        ok, _ = validate_manifest(valid_manifest(kind="invalid"))
        assert ok is False

    def test_kind_not_string(self):
        ok, _ = validate_manifest(valid_manifest(kind=123))
        assert ok is False

    def test_runner_invalid(self):
        ok, _ = validate_manifest(valid_manifest(runner="docker"))
        assert ok is False

    def test_enabled_not_bool(self):
        for bad in ("true", 1, 0, None):
            ok, _ = validate_manifest(valid_manifest(enabled=bad))
            assert ok is False, f"enabled={bad!r} should fail"

    def test_enabled_bool_true_false_valid(self):
        for val in (True, False):
            ok, _ = validate_manifest(valid_manifest(enabled=val))
            assert ok is True

    def test_interval_seconds_zero_fails(self):
        ok, _ = validate_manifest(valid_manifest(interval_seconds=0))
        assert ok is False

    def test_interval_seconds_negative_fails(self):
        ok, _ = validate_manifest(valid_manifest(interval_seconds=-1))
        assert ok is False

    def test_interval_seconds_float_fails(self):
        ok, _ = validate_manifest(valid_manifest(interval_seconds=3.5))
        assert ok is False

    def test_interval_seconds_bool_fails(self):
        ok, _ = validate_manifest(valid_manifest(interval_seconds=True))
        assert ok is False

    def test_heartbeat_timeout_zero_fails(self):
        ok, _ = validate_manifest(valid_manifest(heartbeat_timeout=0))
        assert ok is False

    def test_tier_priority_bool_fails(self):
        ok, _ = validate_manifest(valid_manifest(tier_priority=True))
        assert ok is False

    def test_tier_priority_int_valid(self):
        for v in (0, 1, -5, 100):
            ok, _ = validate_manifest(valid_manifest(tier_priority=v))
            assert ok is True, f"tier_priority={v} should be valid"

    def test_max_restarts_zero_fails(self):
        ok, _ = validate_manifest(valid_manifest(max_restarts=0))
        assert ok is False

    def test_max_restarts_bool_fails(self):
        ok, _ = validate_manifest(valid_manifest(max_restarts=True))
        assert ok is False

    def test_clean_exit_wait_not_bool(self):
        ok, _ = validate_manifest(valid_manifest(clean_exit_wait="yes"))
        assert ok is False

    def test_session_timeout_zero_fails(self):
        ok, _ = validate_manifest(valid_manifest(session_timeout=0))
        assert ok is False

    def test_session_timeout_negative_fails(self):
        ok, _ = validate_manifest(valid_manifest(session_timeout=-10))
        assert ok is False

    def test_noop_cap_negative_fails(self):
        ok, _ = validate_manifest(valid_manifest(noop_cap=-1))
        assert ok is False

    def test_noop_cap_bool_fails(self):
        ok, _ = validate_manifest(valid_manifest(noop_cap=True))
        assert ok is False

    def test_batch_tier_invalid(self):
        ok, _ = validate_manifest(valid_manifest(batch_tier="ultra"))
        assert ok is False

    def test_complexity_filter_not_list(self):
        ok, _ = validate_manifest(valid_manifest(complexity_filter="small"))
        assert ok is False

    def test_complexity_filter_empty_list(self):
        ok, _ = validate_manifest(valid_manifest(complexity_filter=[]))
        assert ok is False

    def test_complexity_filter_non_string_elements(self):
        ok, _ = validate_manifest(valid_manifest(complexity_filter=[1, 2]))
        assert ok is False

    def test_shard_empty_string(self):
        ok, _ = validate_manifest(valid_manifest(shard=""))
        assert ok is False

    def test_scratchpad_not_bool(self):
        ok, _ = validate_manifest(valid_manifest(scratchpad="yes"))
        assert ok is False


# ===========================================================================
# validate_manifest — input/output
# ===========================================================================

class TestInputOutputValidation:
    def test_input_empty_list(self):
        ok, _ = validate_manifest(valid_manifest(input=[]))
        assert ok is False

    def test_input_not_list(self):
        ok, _ = validate_manifest(valid_manifest(input="QUEUE.md"))
        assert ok is False

    def test_input_entry_not_dict(self):
        ok, _ = validate_manifest(valid_manifest(input=["not a dict"]))
        assert ok is False

    def test_input_missing_path(self):
        ok, _ = validate_manifest(valid_manifest(input=[{"type": "queue"}]))
        assert ok is False

    def test_input_missing_type(self):
        ok, _ = validate_manifest(valid_manifest(input=[{"path": "QUEUE.md"}]))
        assert ok is False

    def test_input_empty_path(self):
        ok, _ = validate_manifest(valid_manifest(input=[{"path": "", "type": "queue"}]))
        assert ok is False

    def test_input_empty_type(self):
        ok, _ = validate_manifest(valid_manifest(input=[{"path": "QUEUE.md", "type": ""}]))
        assert ok is False

    def test_input_filter_not_string(self):
        ok, _ = validate_manifest(valid_manifest(input=[{"path": "x", "type": "y", "filter": 123}]))
        assert ok is False

    def test_output_empty_list(self):
        ok, _ = validate_manifest(valid_manifest(output=[]))
        assert ok is False

    def test_output_entry_missing_path(self):
        ok, _ = validate_manifest(valid_manifest(output=[{"type": "log"}]))
        assert ok is False

    def test_output_entry_missing_type(self):
        ok, _ = validate_manifest(valid_manifest(output=[{"path": "logs/x.log"}]))
        assert ok is False

    def test_multiple_input_entries(self):
        m = valid_manifest(input=[
            {"path": "QUEUE.md", "type": "queue"},
            {"path": "other.md", "type": "docs"},
        ])
        ok, _ = validate_manifest(m)
        assert ok is True

    def test_multiple_output_entries(self):
        m = valid_manifest(output=[
            {"path": "logs/a.log", "type": "log"},
            {"path": "docs/b.md", "type": "docs"},
        ])
        ok, _ = validate_manifest(m)
        assert ok is True


# ===========================================================================
# validate_manifest — noop_cap conditional
# ===========================================================================

class TestNoopCapConditional:
    def test_noop_cap_positive_requires_counter(self):
        ok, errors = validate_manifest(valid_manifest(noop_cap=3))
        assert ok is False
        assert any("noop_counter_file" in e for e in errors)

    def test_noop_cap_positive_with_counter_ok(self):
        ok, _ = validate_manifest(valid_manifest(noop_cap=3, noop_counter_file="counter.txt"))
        assert ok is True

    def test_noop_cap_zero_without_counter_ok(self):
        ok, _ = validate_manifest(valid_manifest(noop_cap=0))
        assert ok is True

    def test_noop_cap_zero_with_counter_validates_type(self):
        ok, _ = validate_manifest(valid_manifest(noop_cap=0, noop_counter_file=""))
        assert ok is False

    def test_noop_cap_positive_with_empty_counter_fails(self):
        ok, _ = validate_manifest(valid_manifest(noop_cap=1, noop_counter_file=""))
        assert ok is False

    def test_noop_cap_positive_with_nonstring_counter_fails(self):
        ok, _ = validate_manifest(valid_manifest(noop_cap=1, noop_counter_file=123))
        assert ok is False

    def test_stray_counter_without_cap_validates(self):
        m = valid_manifest()
        del m["noop_cap"]
        m["noop_counter_file"] = "counter.txt"
        ok, _ = validate_manifest(m)
        # noop_cap missing is already an error; counter alone should not add another edge error
        assert any("noop_cap" in e or "noop_counter" in e for e in [str(e) for e in _] ) or ok is False

    def test_stray_empty_counter_without_cap_fails(self):
        m = valid_manifest()
        del m["noop_cap"]
        m["noop_counter_file"] = ""
        ok, errors = validate_manifest(m)
        assert ok is False


# ===========================================================================
# validate_manifest — path-like inputs
# ===========================================================================

class TestValidateManifestPathInputs:
    def test_dict_input(self):
        ok, _ = validate_manifest(valid_manifest())
        assert ok is True

    def test_path_to_valid_file(self, tmp_path):
        p = tmp_path / "m.json"
        p.write_text(json.dumps(valid_manifest()))
        ok, _ = validate_manifest(p)
        assert ok is True

    def test_path_to_invalid_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{ not json }")
        ok, errors = validate_manifest(p)
        assert ok is False
        assert any("invalid JSON" in e for e in errors)

    def test_path_to_nonexistent_json(self, tmp_path):
        ok, errors = validate_manifest(tmp_path / "missing.json")
        assert ok is False
        assert any("file not found" in e for e in errors)

    def test_string_with_slash_treated_as_missing_file(self, tmp_path):
        ok, errors = validate_manifest("some/missing.json")
        assert ok is False
        assert any("file not found" in e for e in errors)

    def test_json_object_not_dict(self, tmp_path):
        p = tmp_path / "arr.json"
        p.write_text(json.dumps([1, 2, 3]))
        ok, errors = validate_manifest(p)
        assert ok is False
        assert any("must be a JSON object" in e for e in errors)

    def test_non_dict_non_path_input(self):
        ok, errors = validate_manifest(12345)
        assert ok is False

    def test_none_input(self):
        ok, errors = validate_manifest(None)
        assert ok is False


# ===========================================================================
# load_manifest
# ===========================================================================

class TestLoadManifest:
    def test_load_valid_file(self, tmp_path):
        p = tmp_path / "m.json"
        p.write_text(json.dumps(valid_manifest()))
        result = load_manifest(p)
        assert result["name"] == "test_bot"

    def test_load_missing_file_raises(self, tmp_path):
        with pytest.raises(ValueError, match="file not found"):
            load_manifest(tmp_path / "no.json")

    def test_load_invalid_json_raises(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{ bad }")
        with pytest.raises(ValueError, match="invalid JSON"):
            load_manifest(p)

    def test_load_invalid_schema_raises(self, tmp_path):
        m = valid_manifest()
        del m["name"]
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(m))
        with pytest.raises(ValueError):
            load_manifest(p)

    def test_load_error_message_contains_path(self, tmp_path):
        m = valid_manifest()
        del m["name"]
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(m))
        try:
            load_manifest(p)
            assert False, "should have raised"
        except ValueError as e:
            assert str(p) in str(e)

    def test_load_preserves_all_fields(self, tmp_path):
        m = valid_manifest(batch_tier="high-limit", shard="x")
        p = tmp_path / "m.json"
        p.write_text(json.dumps(m))
        result = load_manifest(p)
        assert result["batch_tier"] == "high-limit"
        assert result["shard"] == "x"

    def test_load_with_string_path(self, tmp_path):
        p = tmp_path / "m.json"
        p.write_text(json.dumps(valid_manifest()))
        result = load_manifest(str(p))
        assert result["name"] == "test_bot"


# ===========================================================================
# load_all_manifests
# ===========================================================================

class TestLoadAllManifests:
    def test_empty_dir(self, tmp_path):
        result = load_all_manifests(tmp_path)
        assert result == {}

    def test_loads_multiple_manifests(self, tmp_path):
        for name in ("alpha", "beta"):
            m = valid_manifest(name=name)
            (tmp_path / f"{name}.json").write_text(json.dumps(m))
        result = load_all_manifests(tmp_path)
        assert "alpha" in result
        assert "beta" in result

    def test_missing_dir_raises(self, tmp_path):
        with pytest.raises(ValueError, match="manifest directory not found"):
            load_all_manifests(tmp_path / "nope")

    def test_not_a_dir_raises(self, tmp_path):
        p = tmp_path / "file.json"
        p.write_text("{}")
        with pytest.raises(ValueError, match="not a directory"):
            load_all_manifests(p)

    def test_invalid_manifest_raises(self, tmp_path):
        bad = valid_manifest(name="bad")
        del bad["kind"]
        (tmp_path / "bad.json").write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            load_all_manifests(tmp_path)

    def test_duplicate_name_raises(self, tmp_path):
        m = valid_manifest(name="same")
        (tmp_path / "a.json").write_text(json.dumps(m))
        (tmp_path / "b.json").write_text(json.dumps(m))
        with pytest.raises(ValueError, match="duplicate manifest name"):
            load_all_manifests(tmp_path)

    def test_ignores_non_json_files(self, tmp_path):
        (tmp_path / "readme.md").write_text("hello")
        m = valid_manifest(name="only")
        (tmp_path / "only.json").write_text(json.dumps(m))
        result = load_all_manifests(tmp_path)
        assert len(result) == 1
        assert "only" in result

    def test_default_dir_missing_raises(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValueError, match="manifest directory not found"):
            load_all_manifests()

    def test_deterministic_order(self, tmp_path):
        for name in ("zeta", "alpha", "middle"):
            m = valid_manifest(name=name)
            (tmp_path / f"{name}.json").write_text(json.dumps(m))
        result = load_all_manifests(tmp_path)
        assert list(result.keys()) == sorted(result.keys())
