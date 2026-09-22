"""Extensive tests for codebot.worker_scaler — P0 dynamic worker pool sizing.

Covers all public symbols in worker_scaler.py:
CLAIM_TTL_SECONDS, MIN_ROTATING_SLOTS,
rotating_slots, worker_reserved_slots,
_get_available_memory_mb, _model_tier_for_complexity,
is_manifest_error_disabled, is_manifest_restart_budget_exceeded,
_read_state_file, load_bot_registry, build_bots.

Uses tmp_path only — never touches live .codebot/state.
One When per test, Given/When/Then docstrings, isolated fixtures.
Real file I/O where possible; mock only clock/external.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import codebot.worker_scaler as ws
from codebot.process_manager import BotConfig


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
def test_claim_ttl_constant_is_300_Given_module_When_inspected_Then_300() -> None:
    """Given worker_scaler module
    When inspecting CLAIM_TTL_SECONDS
    Then it equals 300."""
    assert ws.CLAIM_TTL_SECONDS == 300


def test_min_rotating_slots_constant_is_4_Given_module_When_inspected_Then_4() -> None:
    """Given worker_scaler module
    When inspecting MIN_ROTATING_SLOTS
    Then it equals 4."""
    assert ws.MIN_ROTATING_SLOTS == 4


# ---------------------------------------------------------------------------
# rotating_slots — scaling logic
# ---------------------------------------------------------------------------
def test_rotating_slots_returns_available_Given_running_10_When_rotating_slots_Then_16() -> None:
    """Given 10 running processes and default cap 26
    When rotating_slots() is called
    Then it returns 16."""
    with patch("codebot.process_manager._count_api_runner_processes", return_value=10):
        result = ws.rotating_slots()
    assert result == 16


def test_rotating_slots_clamps_at_zero_Given_running_exceeds_cap_When_rotating_slots_Then_zero() -> None:
    """Given running count exceeds max_concurrent
    When rotating_slots() is called
    Then it clamps to 0."""
    with patch("codebot.process_manager._count_api_runner_processes", return_value=30):
        result = ws.rotating_slots(max_concurrent=26)
    assert result == 0


def test_rotating_slots_exactly_at_cap_Given_running_equals_cap_When_rotating_slots_Then_zero() -> None:
    """Given running equals max_concurrent
    When rotating_slots() is called
    Then it returns 0."""
    with patch("codebot.process_manager._count_api_runner_processes", return_value=26):
        result = ws.rotating_slots(max_concurrent=26)
    assert result == 0


def test_rotating_slots_returns_zero_on_exception_Given_count_raises_When_rotating_slots_Then_zero() -> None:
    """Given _count_api_runner_processes raises
    When rotating_slots() is called
    Then it returns 0."""
    with patch("codebot.process_manager._count_api_runner_processes", side_effect=RuntimeError("boom")):
        result = ws.rotating_slots()
    assert result == 0


def test_rotating_slots_custom_max_concurrent_Given_custom_cap_When_rotating_slots_Then_uses_custom() -> None:
    """Given custom max_concurrent 10 and 3 running
    When rotating_slots(max_concurrent=10) is called
    Then it returns 7."""
    with patch("codebot.process_manager._count_api_runner_processes", return_value=3):
        result = ws.rotating_slots(max_concurrent=10)
    assert result == 7


def test_rotating_slots_zero_running_Given_no_running_When_rotating_slots_Then_full_cap() -> None:
    """Given 0 running processes
    When rotating_slots() is called
    Then it returns full cap."""
    with patch("codebot.process_manager._count_api_runner_processes", return_value=0):
        result = ws.rotating_slots(max_concurrent=26)
    assert result == 26


def test_rotating_slots_handles_import_exception_Given_import_fails_When_rotating_slots_Then_zero() -> None:
    """Given process_manager import fails inside rotating_slots
    When rotating_slots() is called
    Then it returns 0."""
    with patch.dict("sys.modules", {"codebot.process_manager": None}):
        result = ws.rotating_slots()
    assert result == 0


# ---------------------------------------------------------------------------
# worker_reserved_slots
# ---------------------------------------------------------------------------
def test_worker_reserved_slots_returns_two_Given_any_When_worker_reserved_slots_Then_two() -> None:
    """Given any state
    When worker_reserved_slots() is called
    Then it returns 2."""
    assert ws.worker_reserved_slots() == 2


def test_worker_reserved_slots_stable_Given_repeated_calls_When_worker_reserved_slots_Then_always_two() -> None:
    """Given repeated calls
    When worker_reserved_slots() is called multiple times
    Then it always returns 2."""
    assert ws.worker_reserved_slots() == 2
    assert ws.worker_reserved_slots() == 2
    assert ws.worker_reserved_slots() == 2


# ---------------------------------------------------------------------------
# _get_available_memory_mb — resource checks
# ---------------------------------------------------------------------------
def test_get_available_memory_mb_parses_meminfo_Given_mocked_meminfo_When_get_available_Then_correct() -> None:
    """Given mocked /proc/meminfo with MemAvailable 8192000 kB
    When _get_available_memory_mb() is called
    Then it returns 8000.0 MB."""
    mock_instance = MagicMock()
    mock_instance.read_text.return_value = "MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\nSwapTotal:             0 kB\n"
    mock_cls = MagicMock(return_value=mock_instance)
    with patch("codebot.worker_scaler.Path", mock_cls):
        result = ws._get_available_memory_mb()
    assert result == 8192000 / 1024
    mock_cls.assert_called_once_with("/proc/meminfo")


def test_get_available_memory_mb_returns_zero_on_os_error_Given_read_raises_When_get_available_Then_zero() -> None:
    """Given Path read raises OSError
    When _get_available_memory_mb() is called
    Then it returns 0.0."""
    mock_instance = MagicMock()
    mock_instance.read_text.side_effect = OSError("missing")
    mock_cls = MagicMock(return_value=mock_instance)
    with patch("codebot.worker_scaler.Path", mock_cls):
        result = ws._get_available_memory_mb()
    assert result == 0.0


def test_get_available_memory_mb_returns_zero_when_no_memavailable_Given_no_line_When_get_available_Then_zero() -> None:
    """Given meminfo without MemAvailable line
    When _get_available_memory_mb() is called
    Then it returns 0.0."""
    mock_instance = MagicMock()
    mock_instance.read_text.return_value = "MemTotal: 16384 kB\nMemFree: 1000 kB\nBuffers: 200 kB\n"
    mock_cls = MagicMock(return_value=mock_instance)
    with patch("codebot.worker_scaler.Path", mock_cls):
        result = ws._get_available_memory_mb()
    assert result == 0.0


def test_get_available_memory_mb_handles_empty_file_Given_empty_meminfo_When_get_available_Then_zero() -> None:
    """Given empty meminfo content
    When _get_available_memory_mb() is called
    Then it returns 0.0."""
    mock_instance = MagicMock()
    mock_instance.read_text.return_value = ""
    mock_cls = MagicMock(return_value=mock_instance)
    with patch("codebot.worker_scaler.Path", mock_cls):
        result = ws._get_available_memory_mb()
    assert result == 0.0


# ---------------------------------------------------------------------------
# _model_tier_for_complexity — scaling logic
# ---------------------------------------------------------------------------
def test_model_tier_trivial_returns_true_Given_trivial_When_model_tier_Then_true() -> None:
    """Given complexity trivial
    When _model_tier_for_complexity is called
    Then it returns True regardless of model."""
    assert ws._model_tier_for_complexity("xiaomi-mimo-2.5", "trivial") is True
    assert ws._model_tier_for_complexity("qwen-3.8-max", "trivial") is True
    assert ws._model_tier_for_complexity("default", "trivial") is True


def test_model_tier_small_returns_true_Given_small_When_model_tier_Then_true() -> None:
    """Given complexity small
    When _model_tier_for_complexity is called
    Then it returns True."""
    assert ws._model_tier_for_complexity("xiaomi-mimo-2.5", "small") is True
    assert ws._model_tier_for_complexity("other", "small") is True


def test_model_tier_medium_returns_true_Given_medium_When_model_tier_Then_true() -> None:
    """Given complexity medium
    When _model_tier_for_complexity is called
    Then it returns True."""
    assert ws._model_tier_for_complexity("xiaomi-mimo-2.5", "medium") is True
    assert ws._model_tier_for_complexity("default", "medium") is True


def test_model_tier_high_with_cheap_returns_false_Given_high_cheap_When_model_tier_Then_false() -> None:
    """Given complexity high and cheap model xiaomi-mimo-2.5
    When _model_tier_for_complexity is called
    Then it returns False."""
    assert ws._model_tier_for_complexity("xiaomi-mimo-2.5", "high") is False


def test_model_tier_high_with_expensive_returns_true_Given_high_expensive_When_model_tier_Then_true() -> None:
    """Given complexity high and expensive model
    When _model_tier_for_complexity is called
    Then it returns True."""
    assert ws._model_tier_for_complexity("qwen-3.8-max", "high") is True
    assert ws._model_tier_for_complexity("qwen-3.8-max-thinking", "high") is True
    assert ws._model_tier_for_complexity("qwen-3.7-max", "high") is True
    assert ws._model_tier_for_complexity("qwen-3.7-max-thinking", "high") is True


def test_model_tier_high_with_default_returns_true_Given_high_default_When_model_tier_Then_true() -> None:
    """Given complexity high and non-cheap non-expensive model
    When _model_tier_for_complexity is called
    Then it returns True."""
    assert ws._model_tier_for_complexity("default", "high") is True
    assert ws._model_tier_for_complexity("other-model", "high") is True


def test_model_tier_critical_with_expensive_returns_true_Given_critical_expensive_When_model_tier_Then_true() -> None:
    """Given complexity critical and expensive model
    When _model_tier_for_complexity is called
    Then it returns True."""
    assert ws._model_tier_for_complexity("qwen-3.8-max", "critical") is True
    assert ws._model_tier_for_complexity("qwen-3.7-max-thinking", "critical") is True


def test_model_tier_critical_with_cheap_returns_false_Given_critical_cheap_When_model_tier_Then_false() -> None:
    """Given complexity critical and cheap model
    When _model_tier_for_complexity is called
    Then it returns False."""
    assert ws._model_tier_for_complexity("xiaomi-mimo-2.5", "critical") is False


def test_model_tier_critical_with_default_returns_false_Given_critical_default_When_model_tier_Then_false() -> None:
    """Given complexity critical and non-expensive model
    When _model_tier_for_complexity is called
    Then it returns False."""
    assert ws._model_tier_for_complexity("default", "critical") is False
    assert ws._model_tier_for_complexity("other", "critical") is False


def test_model_tier_unknown_complexity_returns_true_Given_unknown_When_model_tier_Then_true() -> None:
    """Given unknown complexity
    When _model_tier_for_complexity is called
    Then it returns True."""
    assert ws._model_tier_for_complexity("xiaomi-mimo-2.5", "unknown") is True
    assert ws._model_tier_for_complexity("default", "low") is True
    assert ws._model_tier_for_complexity("default", "") is True


def test_model_tier_queue_flag_does_not_affect_result_Given_queue_flag_When_model_tier_Then_same() -> None:
    """Given queue_has_tier_work flag set
    When _model_tier_for_complexity is called
    Then result is same as without flag (flag currently unused)."""
    assert ws._model_tier_for_complexity("xiaomi-mimo-2.5", "high", queue_has_tier_work=True) is False
    assert ws._model_tier_for_complexity("xiaomi-mimo-2.5", "high", queue_has_tier_work=False) is False
    assert ws._model_tier_for_complexity("qwen-3.8-max", "critical", queue_has_tier_work=True) is True


# ---------------------------------------------------------------------------
# is_manifest_error_disabled — bot registry / state file
# ---------------------------------------------------------------------------
def test_is_manifest_error_disabled_true_Given_state_disabled_When_check_Then_true(tmp_path: Path) -> None:
    """Given state file with status disabled
    When is_manifest_error_disabled() is called
    Then it returns True."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "mybot.state.json").write_text(json.dumps({"status": "disabled"}))
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.is_manifest_error_disabled("mybot")
    assert result is True


def test_is_manifest_error_disabled_false_when_enabled_Given_status_running_When_check_Then_false(tmp_path: Path) -> None:
    """Given state file with status running
    When is_manifest_error_disabled() is called
    Then it returns False."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "mybot.state.json").write_text(json.dumps({"status": "running"}))
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.is_manifest_error_disabled("mybot")
    assert result is False


def test_is_manifest_error_disabled_false_when_missing_Given_no_file_When_check_Then_false(tmp_path: Path) -> None:
    """Given no state file
    When is_manifest_error_disabled() is called
    Then it returns False."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.is_manifest_error_disabled("ghost")
    assert result is False


def test_is_manifest_error_disabled_false_on_corrupt_Given_corrupt_json_When_check_Then_false(tmp_path: Path) -> None:
    """Given corrupt JSON state file
    When is_manifest_error_disabled() is called
    Then it returns False."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "bad.state.json").write_text("not json {{{")
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.is_manifest_error_disabled("bad")
    assert result is False


def test_is_manifest_error_disabled_false_on_empty_dict_Given_empty_json_When_check_Then_false(tmp_path: Path) -> None:
    """Given state file with empty dict
    When is_manifest_error_disabled() is called
    Then it returns False."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "empty.state.json").write_text(json.dumps({}))
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.is_manifest_error_disabled("empty")
    assert result is False


# ---------------------------------------------------------------------------
# is_manifest_restart_budget_exceeded — scaling limits / claim TTL context
# ---------------------------------------------------------------------------
def test_is_manifest_restart_budget_exceeded_true_Given_restart_count_5_When_check_Then_true(tmp_path: Path) -> None:
    """Given restart_count 5
    When is_manifest_restart_budget_exceeded() is called
    Then it returns True."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "bot1.state.json").write_text(json.dumps({"restart_count": 5}))
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.is_manifest_restart_budget_exceeded("bot1")
    assert result is True


def test_is_manifest_restart_budget_exceeded_true_above_limit_Given_restart_10_When_check_Then_true(tmp_path: Path) -> None:
    """Given restart_count 10
    When is_manifest_restart_budget_exceeded() is called
    Then it returns True."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "bot1.state.json").write_text(json.dumps({"restart_count": 10}))
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.is_manifest_restart_budget_exceeded("bot1")
    assert result is True


def test_is_manifest_restart_budget_exceeded_false_when_below_Given_restart_4_When_check_Then_false(tmp_path: Path) -> None:
    """Given restart_count 4
    When is_manifest_restart_budget_exceeded() is called
    Then it returns False."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "bot1.state.json").write_text(json.dumps({"restart_count": 4}))
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.is_manifest_restart_budget_exceeded("bot1")
    assert result is False


def test_is_manifest_restart_budget_exceeded_false_when_zero_Given_restart_0_When_check_Then_false(tmp_path: Path) -> None:
    """Given restart_count 0
    When is_manifest_restart_budget_exceeded() is called
    Then it returns False."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "bot1.state.json").write_text(json.dumps({"restart_count": 0}))
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.is_manifest_restart_budget_exceeded("bot1")
    assert result is False


def test_is_manifest_restart_budget_exceeded_false_when_missing_Given_no_file_When_check_Then_false(tmp_path: Path) -> None:
    """Given no state file
    When is_manifest_restart_budget_exceeded() is called
    Then it returns False."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.is_manifest_restart_budget_exceeded("ghost")
    assert result is False


def test_is_manifest_restart_budget_exceeded_false_on_corrupt_Given_corrupt_When_check_Then_false(tmp_path: Path) -> None:
    """Given corrupt JSON
    When is_manifest_restart_budget_exceeded() is called
    Then it returns False."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "bad.state.json").write_text("{{{")
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.is_manifest_restart_budget_exceeded("bad")
    assert result is False


def test_is_manifest_restart_budget_exceeded_false_when_no_count_field_Given_no_restart_count_When_check_Then_false(tmp_path: Path) -> None:
    """Given state file without restart_count
    When is_manifest_restart_budget_exceeded() is called
    Then it returns False (defaults to 0)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "bot1.state.json").write_text(json.dumps({"status": "running"}))
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.is_manifest_restart_budget_exceeded("bot1")
    assert result is False


# ---------------------------------------------------------------------------
# _read_state_file — bot registry loading helper
# ---------------------------------------------------------------------------
def test_read_state_file_returns_data_Given_valid_file_When_read_Then_dict(tmp_path: Path) -> None:
    """Given valid state file
    When _read_state_file() is called
    Then it returns parsed dict."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    data = {"status": "running", "restart_count": 2}
    (state_dir / "alpha.state.json").write_text(json.dumps(data))
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws._read_state_file("alpha")
    assert result == data


def test_read_state_file_returns_empty_when_missing_Given_no_file_When_read_Then_empty(tmp_path: Path) -> None:
    """Given no state file
    When _read_state_file() is called
    Then it returns {}."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws._read_state_file("ghost")
    assert result == {}


def test_read_state_file_returns_empty_on_corrupt_Given_corrupt_When_read_Then_empty(tmp_path: Path) -> None:
    """Given corrupt JSON
    When _read_state_file() is called
    Then it returns {}."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "bad.state.json").write_text("not json")
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws._read_state_file("bad")
    assert result == {}


def test_read_state_file_returns_empty_on_os_error_Given_unreadable_When_read_Then_empty(tmp_path: Path) -> None:
    """Given file that raises on read
    When _read_state_file() is called
    Then it returns {}."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "err.state.json").write_text(json.dumps({"x": 1}))
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        with patch.object(Path, "read_text", side_effect=OSError("fail")):
            result = ws._read_state_file("err")
    assert result == {}


# ---------------------------------------------------------------------------
# load_bot_registry — bot registry loading
# ---------------------------------------------------------------------------
def test_load_bot_registry_without_adapter_returns_defaults_Given_none_When_load_Then_defaults() -> None:
    """Given no adapter
    When load_bot_registry(None) is called
    Then it returns 3 default BotConfigs."""
    result = ws.load_bot_registry(None)
    assert len(result) == 3
    names = [c.name for c in result]
    assert names == ["discovery", "implementer", "reviewer"]


def test_load_bot_registry_without_adapter_prompt_files_Given_none_When_load_Then_prompt_files() -> None:
    """Given no adapter
    When load_bot_registry is called
    Then default prompt files are set."""
    result = ws.load_bot_registry()
    prompt_map = {c.name: c.prompt_file for c in result}
    assert prompt_map["discovery"] == "bug_hunter.md"
    assert prompt_map["implementer"] == "general_implementer.md"
    assert prompt_map["reviewer"] == "correctness_reviewer.md"


def test_load_bot_registry_without_adapter_intervals_Given_none_When_load_Then_intervals() -> None:
    """Given no adapter
    When load_bot_registry is called
    Then default intervals are set."""
    result = ws.load_bot_registry(None)
    interval_map = {c.name: c.interval_seconds for c in result}
    assert interval_map["discovery"] == 1800
    assert interval_map["implementer"] == 300
    assert interval_map["reviewer"] == 600


def test_load_bot_registry_with_adapter_success_Given_adapter_with_entries_When_load_Then_configs() -> None:
    """Given adapter returning two entries
    When load_bot_registry(adapter) is called
    Then it returns configs from adapter."""
    adapter = MagicMock()
    adapter.bot_registry.return_value = [
        {"name": "custom-a", "prompt": "a.md", "interval": 100, "model": "xiaomi-mimo-2.5"},
        {"name": "custom-b", "prompt": "b.md", "interval": 200, "model": "default"},
    ]
    result = ws.load_bot_registry(adapter)
    assert len(result) == 2
    assert result[0].name == "custom-a"
    assert result[0].prompt_file == "a.md"
    assert result[0].interval_seconds == 100
    assert result[1].name == "custom-b"


def test_load_bot_registry_adapter_fields_mapped_Given_full_entry_When_load_Then_all_fields() -> None:
    """Given adapter entry with all fields
    When load_bot_registry is called
    Then all BotConfig fields are mapped."""
    adapter = MagicMock()
    adapter.bot_registry.return_value = [
        {
            "name": "full",
            "prompt": "full.md",
            "interval": 999,
            "model": "qwen-3.8-max",
            "fallback_model": "fallback",
            "enabled": False,
            "max_restarts": 10,
            "clean_exit_wait": False,
            "tier": 1,
            "runner_mode": "local",
            "max_tokens_per_run": 5000,
        }
    ]
    result = ws.load_bot_registry(adapter)
    cfg = result[0]
    assert cfg.name == "full"
    assert cfg.prompt_file == "full.md"
    assert cfg.interval_seconds == 999
    assert cfg.heartbeat_timeout == 1998
    assert cfg.model == "qwen-3.8-max"
    assert cfg.fallback_model == "fallback"
    assert cfg.enabled is False
    assert cfg.max_restarts == 10
    assert cfg.clean_exit_wait is False
    assert cfg.tier == 1
    assert cfg.runner_mode == "local"
    assert cfg.max_tokens_per_run == 5000


def test_load_bot_registry_adapter_defaults_for_missing_fields_Given_minimal_entry_When_load_Then_defaults() -> None:
    """Given adapter entry with only name
    When load_bot_registry is called
    Then missing fields use defaults."""
    adapter = MagicMock()
    adapter.bot_registry.return_value = [{"name": "minimal"}]
    result = ws.load_bot_registry(adapter)
    cfg = result[0]
    assert cfg.prompt_file == "minimal.md"
    assert cfg.interval_seconds == 600
    assert cfg.heartbeat_timeout == 1200
    assert cfg.model == "default"
    assert cfg.fallback_model == ""
    assert cfg.enabled is True
    assert cfg.max_restarts == 5
    assert cfg.clean_exit_wait is True
    assert cfg.tier == 2
    assert cfg.runner_mode == "api"
    assert cfg.max_tokens_per_run == 0


def test_load_bot_registry_with_adapter_empty_falls_back_Given_empty_entries_When_load_Then_defaults() -> None:
    """Given adapter returning empty list
    When load_bot_registry is called
    Then it falls back to defaults."""
    adapter = MagicMock()
    adapter.bot_registry.return_value = []
    result = ws.load_bot_registry(adapter)
    assert len(result) == 3
    assert [c.name for c in result] == ["discovery", "implementer", "reviewer"]


def test_load_bot_registry_with_adapter_exception_falls_back_Given_adapter_raises_When_load_Then_defaults() -> None:
    """Given adapter whose bot_registry raises
    When load_bot_registry is called
    Then it returns defaults."""
    adapter = MagicMock()
    adapter.bot_registry.side_effect = RuntimeError("adapter fail")
    result = ws.load_bot_registry(adapter)
    assert len(result) == 3
    assert [c.name for c in result] == ["discovery", "implementer", "reviewer"]


def test_load_bot_registry_adapter_partial_failure_preserves_defaults_Given_adapter_returns_none_then_raises_When_load_Then_defaults() -> None:
    """Given adapter that returns non-list or raises later
    When load_bot_registry handles it
    Then it does not crash."""
    adapter = MagicMock()
    adapter.bot_registry.side_effect = ValueError("bad data")
    result = ws.load_bot_registry(adapter)
    assert len(result) == 3


# ---------------------------------------------------------------------------
# build_bots — scaling / worker pool construction from registry
# ---------------------------------------------------------------------------
def test_build_bots_creates_bot_state_Given_registry_When_build_bots_Then_dict(tmp_path: Path) -> None:
    """Given a registry with two BotConfigs
    When build_bots() is called
    Then it returns dict with BotState per config."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    registry = [
        BotConfig("alpha", "a.md", 300, 600, "default"),
        BotConfig("beta", "b.md", 600, 1200, "default"),
    ]
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.build_bots(registry)
    assert set(result.keys()) == {"alpha", "beta"}
    assert result["alpha"].config.name == "alpha"
    assert result["beta"].config.name == "beta"
    assert result["alpha"].consecutive_errors == 0
    assert result["alpha"].next_run_at == 0.0
    assert result["alpha"].restart_count == 0


def test_build_bots_loads_state_file_Given_state_file_When_build_bots_Then_populated(tmp_path: Path) -> None:
    """Given state file with errors and next_run_at
    When build_bots() is called
    Then BotState is populated from file."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "alpha.state.json").write_text(json.dumps({
        "consecutive_errors": 3,
        "next_run_at": 12345.0,
        "restart_count": 2,
    }))
    registry = [BotConfig("alpha", "a.md", 300, 600, "default")]
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.build_bots(registry)
    assert result["alpha"].consecutive_errors == 3
    assert result["alpha"].next_run_at == 12345.0
    assert result["alpha"].restart_count == 2


def test_build_bots_ignores_missing_state_file_Given_no_file_When_build_bots_Then_defaults(tmp_path: Path) -> None:
    """Given no state file
    When build_bots() is called
    Then defaults remain."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    registry = [BotConfig("ghost", "g.md", 300, 600, "default")]
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.build_bots(registry)
    assert result["ghost"].consecutive_errors == 0
    assert result["ghost"].next_run_at == 0.0
    assert result["ghost"].restart_count == 0


def test_build_bots_ignores_corrupt_state_file_Given_corrupt_When_build_bots_Then_defaults(tmp_path: Path) -> None:
    """Given corrupt JSON state file
    When build_bots() is called
    Then it falls back to defaults."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "bad.state.json").write_text("{{{ not json")
    registry = [BotConfig("bad", "b.md", 300, 600, "default")]
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.build_bots(registry)
    assert result["bad"].consecutive_errors == 0
    assert result["bad"].next_run_at == 0.0
    assert result["bad"].restart_count == 0


def test_build_bots_ignores_non_dict_json_Given_list_json_When_build_bots_Then_defaults(tmp_path: Path) -> None:
    """Given state file with JSON list (not dict)
    When build_bots() is called
    Then it ignores and keeps defaults."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "weird.state.json").write_text(json.dumps([1, 2, 3]))
    registry = [BotConfig("weird", "w.md", 300, 600, "default")]
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.build_bots(registry)
    assert result["weird"].consecutive_errors == 0
    assert result["weird"].next_run_at == 0.0
    assert result["weird"].restart_count == 0


def test_build_bots_handles_partial_state_Given_partial_json_When_build_bots_Then_partial_populated(tmp_path: Path) -> None:
    """Given state file with only restart_count
    When build_bots() is called
    Then only restart_count is set, others default."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "partial.state.json").write_text(json.dumps({"restart_count": 7}))
    registry = [BotConfig("partial", "p.md", 300, 600, "default")]
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.build_bots(registry)
    assert result["partial"].restart_count == 7
    assert result["partial"].consecutive_errors == 0
    assert result["partial"].next_run_at == 0.0


def test_build_bots_empty_registry_Given_empty_When_build_bots_Then_empty(tmp_path: Path) -> None:
    """Given empty registry
    When build_bots() is called
    Then it returns empty dict."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.build_bots([])
    assert result == {}


def test_build_bots_three_defaults_roundtrip_Given_defaults_registry_When_build_bots_Then_three(tmp_path: Path) -> None:
    """Given default registry from load_bot_registry
    When build_bots() is called
    Then it creates 3 BotStates."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    registry = ws.load_bot_registry(None)
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.build_bots(registry)
    assert len(result) == 3
    assert "discovery" in result
    assert "implementer" in result
    assert "reviewer" in result


def test_build_bots_state_file_with_string_values_Given_string_numbers_When_build_bots_Then_preserved(tmp_path: Path) -> None:
    """Given state file with float next_run_at
    When build_bots() is called
    Then float is preserved."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "floatbot.state.json").write_text(json.dumps({
        "consecutive_errors": 1,
        "next_run_at": 9999999.5,
        "restart_count": 1,
    }))
    registry = [BotConfig("floatbot", "f.md", 300, 600, "default")]
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.build_bots(registry)
    assert result["floatbot"].next_run_at == 9999999.5


def test_build_bots_real_io_multiple_bots_Given_multiple_state_files_When_build_bots_Then_each_correct(tmp_path: Path) -> None:
    """Given multiple bots with distinct state files
    When build_bots() is called
    Then each BotState reflects its own file."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "a.state.json").write_text(json.dumps({"consecutive_errors": 5, "next_run_at": 111.0, "restart_count": 1}))
    (state_dir / "b.state.json").write_text(json.dumps({"consecutive_errors": 9, "next_run_at": 222.0, "restart_count": 4}))
    registry = [
        BotConfig("a", "a.md", 300, 600, "default"),
        BotConfig("b", "b.md", 300, 600, "default"),
        BotConfig("c", "c.md", 300, 600, "default"),
    ]
    with patch("codebot.process_manager.STATE_DIR", state_dir):
        result = ws.build_bots(registry)
    assert result["a"].consecutive_errors == 5
    assert result["a"].next_run_at == 111.0
    assert result["a"].restart_count == 1
    assert result["b"].consecutive_errors == 9
    assert result["b"].next_run_at == 222.0
    assert result["b"].restart_count == 4
    assert result["c"].consecutive_errors == 0
    assert result["c"].next_run_at == 0.0
    assert result["c"].restart_count == 0
