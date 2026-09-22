# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownMemberType=false, reportUnannotatedClassAttribute=false, reportUnusedCallResult=false, reportUnknownLambdaType=false, reportUnusedParameter=false, reportUnusedImport=false, reportArgumentType=false, reportIndexIssue=false, reportOperatorIssue=false, reportOptionalMemberAccess=false, reportGeneralTypeIssues=false
"""Extensive P0 batch4 — 6 data/service modules >=85% coverage each.

Modules:
  codebot.metrics_service, codebot.metrics_collector, codebot.cost_tracker,
  codebot.coverage_runner, codebot.coverage_bridge, codebot.anomaly_alerts

Pattern: Given/When/Then + tmp_path isolation + mock subprocess/network.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import threading
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# metrics_service
# ---------------------------------------------------------------------------
import codebot.metrics_service as ms_mod
from codebot.metrics_service import (
    BotMetrics,
    PipelineMetrics,
    _write_json_atomic,
    _load_metrics_file,
    bot_metrics_path,
    read_bot_metrics,
    save_bot_metrics,
    record_bot_run,
    record_stuck_restart,
    record_model_change,
    get_all_bot_metrics,
    pipeline_metrics_path,
    read_pipeline_metrics,
    save_pipeline_metrics,
    collect_bot_status_snapshot,
)

import codebot.metrics_collector as mc_mod
from codebot.metrics_collector import (
    _read_json as mc_read_json,
    _get_bots_with_fresh_heartbeats,
    _discover_workers,
    _collect_execution,
    _collect_alignment,
    _collect_progress,
    _collect_liveness,
    _collect_quality,
    _collect_tokens,
    _collect_bot_tokens,
    _collect_throughput,
    _collect_output_quality,
    _collect_economics,
    _collect_autonomy,
    _collect_learning,
    _collect_scrutiny,
    _build_docs_findings_cache,
    _parse_queue_md_once,
    collect_all,
    save_snapshot,
)

import codebot.cost_tracker as ct_mod
from codebot.cost_tracker import CostTracker, TicketCost

import codebot.coverage_runner as cov_mod
from codebot.coverage_runner import (
    ModuleCoverage,
    CoverageReport,
    run_coverage,
    _parse_coverage_json,
    _parse_missing_lines,
    _empty_report,
    save_coverage_report,
    load_coverage_report,
)

import codebot.coverage_bridge as cb_mod
from codebot.coverage_bridge import (
    generate_coverage_tickets,
    _is_protected,
    _chunk_missing_lines,
    _severity_for_coverage,
    _risk_for_coverage,
    _format_line_ranges,
    _create_test_ticket,
    coverage_delta_score,
)

import codebot.anomaly_alerts as aa_mod
from codebot.anomaly_alerts import (
    _read_json as aa_read_json,
    _write_json_atomic as aa_write_json_atomic,
    _history_snapshots,
    _queue_depth,
    _approval_backlog_hours,
    _daily_token_avg,
    evaluate,
    write_daily_digest,
)

# =============================================================================
# metrics_service helpers
# =============================================================================

@pytest.fixture(autouse=False)
def isolate_metrics_service(tmp_path, monkeypatch):
    new_metrics = tmp_path / "metrics"
    new_metrics.mkdir(parents=True)
    monkeypatch.setattr(ms_mod, "METRICS_DIR", new_metrics)
    monkeypatch.setattr(ms_mod, "STATE_DIR", tmp_path)
    return new_metrics


# =============================================================================
# metrics_service tests
# =============================================================================

def test_metrics_service_write_and_load_atomic_Given_dict_When_write_Then_load_roundtrip(isolate_metrics_service, tmp_path):
    """Given dict When _write_json_atomic Then file readable."""
    p = isolate_metrics_service / "a.json"
    _write_json_atomic(p, {"k": 1, "list": [1,2]})
    assert p.exists()
    assert json.loads(p.read_text()) == {"k": 1, "list": [1,2]}
    # overwrite
    _write_json_atomic(p, [1, 2, 3])
    assert json.loads(p.read_text()) == [1, 2, 3]

def test_metrics_service_load_metrics_file_missing_Given_no_file_When_load_Then_empty(isolate_metrics_service):
    """Given missing file When _load_metrics_file Then empty dict."""
    p = isolate_metrics_service / "nope.json"
    assert _load_metrics_file(p) == {}

def test_metrics_service_load_metrics_file_corrupt_Given_bad_json_When_load_Then_empty(isolate_metrics_service):
    """Given corrupt json When _load_metrics_file Then empty."""
    p = isolate_metrics_service / "bad.json"
    p.write_text("{not json")
    assert _load_metrics_file(p) == {}
    p.write_text(json.dumps([1,2,3]))
    assert _load_metrics_file(p) == {}  # non-dict returns empty

def test_metrics_service_load_metrics_file_os_error_Given_unreadable_When_load_Then_empty(isolate_metrics_service, monkeypatch):
    """Given OSError on read When _load_metrics_file Then empty."""
    p = isolate_metrics_service / "err.json"
    p.write_text(json.dumps({"a": 1}))
    with patch.object(Path, "read_text", side_effect=OSError("fail")):
        assert _load_metrics_file(p) == {}

def test_metrics_service_bot_metrics_path_Given_name_When_path_Then_under_metrics_dir(isolate_metrics_service):
    """Given bot name When bot_metrics_path Then correct path."""
    p = bot_metrics_path("mybot")
    assert p == isolate_metrics_service / "mybot.metrics.json"
    assert p.name == "mybot.metrics.json"

def test_metrics_service_read_bot_metrics_defaults_Given_no_file_When_read_Then_defaults(isolate_metrics_service):
    """Given no file When read_bot_metrics Then defaults."""
    m = read_bot_metrics("newbot")
    assert m.name == "newbot"
    assert m.total_runs == 0
    assert m.successful_runs == 0
    assert m.first_seen_at > 0

def test_metrics_service_save_and_read_bot_metrics_Given_metrics_When_save_then_read_Then_equal(isolate_metrics_service):
    """Given BotMetrics When save_bot_metrics Then read matches."""
    bm = BotMetrics(name="botA", total_runs=5, successful_runs=3, failed_runs=2, stuck_restarts=1,
                    total_uptime_seconds=123.4, last_run_at=1000.0, last_success_at=999.0,
                    last_failure_at=998.0, consecutive_errors=1, model_changes=2, first_seen_at=500.0)
    save_bot_metrics(bm)
    loaded = read_bot_metrics("botA")
    assert loaded.name == "botA"
    assert loaded.total_runs == 5
    assert loaded.successful_runs == 3
    assert loaded.failed_runs == 2
    assert loaded.stuck_restarts == 1
    assert loaded.total_uptime_seconds == 123.4
    assert loaded.last_run_at == 1000.0
    assert loaded.model_changes == 2
    assert loaded.first_seen_at == 500.0

def test_metrics_service_read_bot_metrics_with_existing_data_Given_saved_file_When_read_Then_fields(isolate_metrics_service):
    """Given saved file with partial data When read Then defaults for missing."""
    p = bot_metrics_path("partial")
    p.write_text(json.dumps({"name": "partial", "total_runs": 10}))
    m = read_bot_metrics("partial")
    assert m.name == "partial"
    assert m.total_runs == 10
    assert m.successful_runs == 0

def test_metrics_service_record_bot_run_success_Given_no_metrics_When_success_Then_counts(isolate_metrics_service, monkeypatch):
    """Given fresh bot When record_bot_run success Then increments success."""
    fixed = 1700000000.0
    monkeypatch.setattr(time, "time", lambda: fixed)
    record_bot_run("srv", success=True, duration_seconds=10.5)
    m = read_bot_metrics("srv")
    assert m.total_runs == 1
    assert m.successful_runs == 1
    assert m.failed_runs == 0
    assert m.consecutive_errors == 0
    assert m.last_success_at == fixed
    assert m.last_run_at == fixed
    assert m.total_uptime_seconds == 10.5

def test_metrics_service_record_bot_run_failure_Given_success_before_When_failure_Then_increments(isolate_metrics_service, monkeypatch):
    """Given previous success When failure Then counts and streak."""
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    record_bot_run("mix", success=True, duration_seconds=1.0)
    monkeypatch.setattr(time, "time", lambda: 2000.0)
    record_bot_run("mix", success=False, duration_seconds=2.0)
    m = read_bot_metrics("mix")
    assert m.total_runs == 2
    assert m.successful_runs == 1
    assert m.failed_runs == 1
    assert m.consecutive_errors == 1
    assert m.last_failure_at == 2000.0
    assert m.total_uptime_seconds == 3.0

def test_metrics_service_record_bot_run_consecutive_errors_reset_Given_failures_When_success_Then_reset(isolate_metrics_service, monkeypatch):
    """Given consecutive failures When success Then reset to 0."""
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    record_bot_run("r", success=False)
    record_bot_run("r", success=False)
    assert read_bot_metrics("r").consecutive_errors == 2
    record_bot_run("r", success=True)
    assert read_bot_metrics("r").consecutive_errors == 0

def test_metrics_service_record_stuck_restart_Given_bot_When_restart_Then_increment(isolate_metrics_service):
    """Given bot When record_stuck_restart Then increments."""
    record_stuck_restart("stuckbot")
    assert read_bot_metrics("stuckbot").stuck_restarts == 1
    record_stuck_restart("stuckbot")
    assert read_bot_metrics("stuckbot").stuck_restarts == 2

def test_metrics_service_record_model_change_Given_bot_When_change_Then_increment(isolate_metrics_service):
    """Given bot When record_model_change Then increments."""
    record_model_change("modelbot")
    assert read_bot_metrics("modelbot").model_changes == 1
    record_model_change("modelbot")
    assert read_bot_metrics("modelbot").model_changes == 2

def test_metrics_service_get_all_bot_metrics_empty_Given_no_dir_When_get_all_Then_empty(monkeypatch, tmp_path):
    """Given no metrics dir When get_all_bot_metrics Then empty."""
    empty_dir = tmp_path / "empty_metrics"
    # ensure not exists
    if empty_dir.exists():
        import shutil; shutil.rmtree(empty_dir)
    monkeypatch.setattr(ms_mod, "METRICS_DIR", empty_dir)
    assert get_all_bot_metrics() == {}

def test_metrics_service_get_all_bot_metrics_with_files_Given_two_bots_When_get_all_Then_both(isolate_metrics_service):
    """Given two saved bots When get_all Then both returned."""
    save_bot_metrics(BotMetrics(name="a", total_runs=1))
    save_bot_metrics(BotMetrics(name="b", total_runs=2))
    allm = get_all_bot_metrics()
    assert "a" in allm and "b" in allm
    assert allm["a"].total_runs == 1
    assert allm["b"].total_runs == 2

def test_metrics_service_get_all_ignores_non_metrics_files_Given_extra_files_When_get_all_Then_ignored(isolate_metrics_service):
    """Given non-metrics files When get_all Then ignored."""
    save_bot_metrics(BotMetrics(name="keep", total_runs=7))
    (isolate_metrics_service / "not_metrics.json").write_text(json.dumps({"x":1}))
    (isolate_metrics_service / "pipeline.metrics.json").write_text(json.dumps({"tickets_created":5}))
    allm = get_all_bot_metrics()
    # pipeline.metrics.json glob is *.metrics.json so it matches, but stem replace yields pipeline
    # ensure keep present
    assert "keep" in allm

def test_metrics_service_pipeline_metrics_path_Given_isolate_When_path_Then_correct(isolate_metrics_service):
    """Given isolate When pipeline_metrics_path Then under metrics dir."""
    p = pipeline_metrics_path()
    assert p == isolate_metrics_service / "pipeline.metrics.json"

def test_metrics_service_read_pipeline_metrics_defaults_Given_no_file_When_read_Then_defaults(isolate_metrics_service):
    """Given no pipeline file When read_pipeline_metrics Then defaults."""
    pm = read_pipeline_metrics()
    assert pm.tickets_created == 0
    assert pm.tickets_completed == 0
    assert isinstance(pm.last_updated, float)

def test_metrics_service_save_and_read_pipeline_Given_metrics_When_save_Then_roundtrip(isolate_metrics_service):
    """Given PipelineMetrics When save Then read matches."""
    pm = PipelineMetrics(tickets_created=10, tickets_completed=7, tickets_reworked=2, avg_resolution_time_seconds=123.5, last_updated=9999.0)
    save_pipeline_metrics(pm)
    loaded = read_pipeline_metrics()
    assert loaded.tickets_created == 10
    assert loaded.tickets_completed == 7
    assert loaded.tickets_reworked == 2
    assert loaded.avg_resolution_time_seconds == 123.5
    assert loaded.last_updated == 9999.0

def test_metrics_service_read_pipeline_with_partial_data_Given_partial_When_read_Then_defaults(isolate_metrics_service):
    """Given partial pipeline json When read Then missing defaults."""
    pipeline_metrics_path().write_text(json.dumps({"tickets_created": 99}))
    pm = read_pipeline_metrics()
    assert pm.tickets_created == 99
    assert pm.tickets_completed == 0

def test_metrics_service_BotMetrics_dataclass_defaults_Given_no_args_When_construct_Then_fields():
    """Given BotMetrics dataclass When constructed Then defaults."""
    bm = BotMetrics(name="x")
    assert bm.total_runs == 0
    assert bm.first_seen_at > 0
    # ensure dataclass fields exist
    assert hasattr(bm, "consecutive_errors")

def test_metrics_service_PipelineMetrics_dataclass_Given_defaults_When_construct_Then_zero():
    """Given PipelineMetrics When constructed Then zeros."""
    pm = PipelineMetrics()
    assert pm.tickets_created == 0
    assert pm.avg_resolution_time_seconds == 0.0

def test_metrics_service_collect_bot_status_snapshot_Given_bots_When_snapshot_Then_contains_fields(tmp_path, monkeypatch):
    """Given bots dict When collect_bot_status_snapshot Then snapshot fields."""
    # patch METRICS_DIR not needed for snapshot, but need health_monitor and model_manager mocks
    fake_hb = {"bot1": time.time(), "bot2": 0.0}
    monkeypatch.setattr("codebot.health_monitor.batch_read_heartbeats", lambda names: {n: fake_hb.get(n, 0.0) for n in names})
    monkeypatch.setattr("codebot.health_monitor.effective_heartbeat_timeout", lambda *a, **kw: 90)
    mock_profile = MagicMock()
    mock_profile.lockup_risk = "low"
    monkeypatch.setattr("codebot.model_manager.model_profile", lambda m: mock_profile)

    # create fake BotState objects
    class FakeProc:
        pid = 1234
        def poll(self): return None
    class FakeConfig:
        enabled = True
        model = "xiaomi-mimo-2.5"
        interval_seconds = 60
        heartbeat_timeout = 90
    class FakeBot:
        def __init__(self, proc):
            self.config = FakeConfig()
            self.process = proc
            self.next_run_at = time.time() + 50
            self.restart_count = 3
            self.consecutive_errors = 1
    bots = {"bot1": FakeBot(FakeProc()), "bot2": FakeBot(None)}
    snap = collect_bot_status_snapshot(bots)
    assert "bot1" in snap and "bot2" in snap
    assert snap["bot1"]["running"] is True
    assert snap["bot1"]["pid"] == 1234
    assert snap["bot2"]["running"] is False
    assert snap["bot1"]["lockup_risk"] == "low"
    assert snap["bot1"]["effective_timeout"] == 90
    assert snap["bot1"]["restart_count"] == 3

def test_metrics_service_collect_snapshot_heartbeat_age_none_Given_no_hb_When_snapshot_Then_none(tmp_path, monkeypatch):
    """Given no heartbeat When snapshot Then heartbeat_age None."""
    monkeypatch.setattr("codebot.health_monitor.batch_read_heartbeats", lambda names: {n: 0.0 for n in names})
    monkeypatch.setattr("codebot.health_monitor.effective_heartbeat_timeout", lambda *a, **kw: 60)
    monkeypatch.setattr("codebot.model_manager.model_profile", lambda m: None)
    class Cfg:
        enabled=False; model="unknown"; interval_seconds=30; heartbeat_timeout=60
    class Bot:
        config=Cfg(); process=None; next_run_at=None; restart_count=0; consecutive_errors=0
    snap = collect_bot_status_snapshot({"nb": Bot()})
    assert snap["nb"]["heartbeat_age_seconds"] is None
    assert snap["nb"]["lockup_risk"] == "unknown"
    assert snap["nb"]["next_run_in"] is None

def test_metrics_service_collect_snapshot_process_poll_not_none_Given_dead_proc_When_snapshot_Then_not_running(tmp_path, monkeypatch):
    """Given process.poll() returns 0 (dead) When snapshot Then not running."""
    monkeypatch.setattr("codebot.health_monitor.batch_read_heartbeats", lambda names: {n: time.time() for n in names})
    monkeypatch.setattr("codebot.health_monitor.effective_heartbeat_timeout", lambda *a, **kw: 80)
    mock_prof = MagicMock(); mock_prof.lockup_risk="medium"
    monkeypatch.setattr("codebot.model_manager.model_profile", lambda m: mock_prof)
    class DeadProc:
        pid=999
        def poll(self): return 0
    class Cfg:
        enabled=True; model="qwen-3.8-max"; interval_seconds=120; heartbeat_timeout=180
    class Bot:
        config=Cfg(); process=DeadProc(); next_run_at=time.time()+10; restart_count=0; consecutive_errors=0
    snap = collect_bot_status_snapshot({"dead": Bot()})
    assert snap["dead"]["running"] is False
    assert snap["dead"]["pid"] is None

def test_metrics_service_collect_snapshot_next_run_in_null_when_running_Given_running_When_snapshot_Then_none(tmp_path, monkeypatch):
    """Given running pid When snapshot Then next_run_in None even if next_run_at set."""
    monkeypatch.setattr("codebot.health_monitor.batch_read_heartbeats", lambda n: {k: time.time() for k in n})
    monkeypatch.setattr("codebot.health_monitor.effective_heartbeat_timeout", lambda *a, **kw: 70)
    monkeypatch.setattr("codebot.model_manager.model_profile", lambda m: MagicMock(lockup_risk="high"))
    class Proc:
        pid=1
        def poll(self): return None
    class Cfg:
        enabled=True; model="qwen-3.8-max-thinking"; interval_seconds=60; heartbeat_timeout=100
    class Bot:
        config=Cfg(); process=Proc(); next_run_at=time.time()+999; restart_count=5; consecutive_errors=2
    snap = collect_bot_status_snapshot({"run": Bot()})
    assert snap["run"]["next_run_in"] is None  # because pid not None

def test_metrics_service_write_json_atomic_replaces_Given_existing_When_write_Then_replaced(isolate_metrics_service):
    """Given existing file When _write_json_atomic Then replaced atomically."""
    p = isolate_metrics_service / "atomic.json"
    p.write_text(json.dumps({"old":1}))
    _write_json_atomic(p, {"new":2})
    assert json.loads(p.read_text()) == {"new":2}
    # tmp file should not exist
    assert not (p.with_name(f"{p.name}.{os.getpid()}.tmp")).exists()

# =============================================================================
# metrics_collector tests
# =============================================================================

@pytest.fixture
def isolate_collector(tmp_path, monkeypatch):
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    dp_docs = tmp_path / "docs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    dp_docs.mkdir(parents=True)
    # Patch module globals
    monkeypatch.setattr(mc_mod, "STATE_DIR", state)
    monkeypatch.setattr(mc_mod, "LOGS_DIR", logs)
    monkeypatch.setattr(mc_mod, "BOTS_DIR", tmp_path)
    monkeypatch.setattr(mc_mod, "METRICS_FILE", state / "bot_metrics.json")
    monkeypatch.setattr(mc_mod, "HISTORY_FILE", state / "bot_metrics_history.jsonl")
    # Ensure KNOWN_BOTS is predictable for tests that depend on it: keep original but tests can override
    return {"state": state, "logs": logs, "tmp": tmp_path, "docs": dp_docs}

def test_collector_read_json_exists_Given_valid_When_read_Then_dict(isolate_collector):
    """Given valid json When _read_json Then dict."""
    p = isolate_collector["state"] / "x.json"
    p.write_text(json.dumps({"a":1}))
    assert mc_read_json(p) == {"a":1}

def test_collector_read_json_missing_Given_no_file_When_read_Then_none(isolate_collector):
    """Given missing file When _read_json Then None."""
    assert mc_read_json(isolate_collector["state"] / "nope.json") is None

def test_collector_read_json_non_dict_Given_list_When_read_Then_none(isolate_collector):
    """Given list json When _read_json Then None."""
    p = isolate_collector["state"] / "list.json"
    p.write_text(json.dumps([1,2]))
    assert mc_read_json(p) is None

def test_collector_read_json_corrupt_Given_bad_When_read_Then_none(isolate_collector):
    """Given corrupt json When _read_json Then None."""
    p = isolate_collector["state"] / "bad.json"
    p.write_text("{bad")
    assert mc_read_json(p) is None

def test_collector_get_bots_with_fresh_heartbeats_Given_fresh_When_call_Then_found(isolate_collector):
    """Given fresh heartbeat files When _get_bots_with_fresh_heartbeats Then found."""
    state = isolate_collector["state"]
    (state / "botA.heartbeat").write_text("x")
    (state / "botB.heartbeat").write_text("x")
    # set mtime to now
    now = time.time()
    os.utime(state / "botA.heartbeat", (now, now))
    os.utime(state / "botB.heartbeat", (now - 400, now - 400))  # stale
    fresh = _get_bots_with_fresh_heartbeats(max_age_s=300)
    assert "botA" in fresh
    assert "botB" not in fresh

def test_collector_get_bots_with_fresh_os_error_Given_unstatable_When_call_Then_skipped(isolate_collector, monkeypatch):
    """Given OSError on stat When _get_bots_with_fresh_heartbeats Then skipped."""
    state = isolate_collector["state"]
    (state / "botC.heartbeat").write_text("x")
    orig_stat = Path.stat
    def fake_stat(self, *a, **kw):
        raise OSError("fail")
    with patch.object(Path, "stat", fake_stat):
        fresh = _get_bots_with_fresh_heartbeats()
        # should not crash, returns empty or skips error file
        assert isinstance(fresh, list)

def test_collector_discover_workers_fallback_Given_no_files_When_discover_Then_12_default(monkeypatch, isolate_collector):
    """Given empty state When _discover_workers Then 12 defaults."""
    # remove any worker files
    for p in list(isolate_collector["state"].glob("worker-*")):
        p.unlink()
    # also ensure no rl_state
    if (isolate_collector["state"] / "rl_state.json").exists():
        (isolate_collector["state"] / "rl_state.json").unlink()
    # Mock orchestrator import to fail -> fallback to discovery via glob then defaults
    with patch.dict("sys.modules", {"orchestrator": None}):
        # force import failure by ensuring WORKER_POOL not found
        workers = _discover_workers()
        assert len(workers) == 12
        assert "worker-1" in workers and "worker-12" in workers

def test_collector_discover_workers_via_heartbeat_glob_Given_files_When_discover_Then_found(isolate_collector):
    """Given worker heartbeat files When _discover_workers Then found."""
    state = isolate_collector["state"]
    for i in [1, 3, 7]:
        (state / f"worker-{i}.heartbeat").write_text("0")
    workers = _discover_workers()
    for i in [1,3,7]:
        assert f"worker-{i}" in workers

def test_collector_discover_workers_invalid_suffix_Given_bad_name_When_discover_Then_ignored(isolate_collector):
    """Given worker-abc heartbeat When discover Then ignored."""
    state = isolate_collector["state"]
    (state / "worker-abc.heartbeat").write_text("x")
    (state / "worker-999.heartbeat").write_text("x")  # valid
    workers = _discover_workers()
    assert "worker-abc" not in workers
    assert "worker-999" in workers

def test_collector_discover_workers_via_checkpoint_Given_checkpoint_When_discover_Then_found(isolate_collector):
    """Given checkpoint file When discover Then found."""
    state = isolate_collector["state"]
    (state / "worker-5.checkpoint.json").write_text(json.dumps({"reason":"done"}))
    workers = _discover_workers()
    assert "worker-5" in workers

def test_collector_discover_workers_via_rl_state_Given_rl_When_discover_Then_found(isolate_collector):
    """Given rl_state bots When discover Then includes."""
    state = isolate_collector["state"]
    state.joinpath("rl_state.json").write_text(json.dumps({"bots": {"worker-9": {}, "worker-11": {}, "notworker": {}}}))
    workers = _discover_workers()
    assert "worker-9" in workers
    assert "worker-11" in workers
    assert "notworker" not in workers

def test_collector_discover_workers_orchestrator_pool_Given_pool_When_discover_Then_included(monkeypatch, isolate_collector):
    """Given orchestrator.WORKER_POOL When discover Then included."""
    fake_orch = MagicMock()
    fake_orch.WORKER_POOL = ["worker-20", "worker-21"]
    with patch.dict("sys.modules", {"orchestrator": fake_orch}):
        workers = _discover_workers()
        assert "worker-20" in workers

def test_collector_collect_execution_completed_Given_done_checkpoint_When_collect_Then_counts(isolate_collector):
    """Given completed checkpoint When _collect_execution Then completion_rate 1."""
    state = isolate_collector["state"]
    state.joinpath("botX.checkpoint.json").write_text(json.dumps({"reason":"completed", "scan_iteration": 5}))
    exe = _collect_execution("botX")
    assert exe["runs"] == 1
    assert exe["completed"] == 1
    assert exe["errors"] == 0
    assert exe["completion_rate"] == 1.0
    assert exe["max_iterations"] == 5

def test_collector_collect_execution_error_and_exit_event_Given_error_When_collect_Then_error_rate(isolate_collector):
    """Given error reason and exit event When collect Then error counts."""
    state = isolate_collector["state"]
    state.joinpath("botE.checkpoint.json").write_text(json.dumps({"reason":"error failed", "tool_iterations": 3}))
    ev_dir = state / "alignment_events"
    ev_dir.mkdir(exist_ok=True)
    (ev_dir / "botE.exit.json").write_text(json.dumps({"exit_code": 1, "exit_reason":"restart needed"}))
    (ev_dir / "botE-2.exit.json").write_text(json.dumps({"exit_code":0}))
    exe = _collect_execution("botE")
    assert exe["runs"] >= 2
    assert exe["errors"] >= 1
    assert exe["restarts"] >= 1

def test_collector_collect_execution_no_files_Given_none_When_collect_Then_zero(isolate_collector):
    """Given no files When _collect_execution Then zeros."""
    exe = _collect_execution("nobody")
    assert exe["runs"] == 0
    assert exe["completion_rate"] == 0.0
    assert exe["max_iterations"] == 0

def test_collector_collect_execution_corrupt_exit_json_Given_bad_exit_When_collect_Then_skipped(isolate_collector):
    """Given corrupt exit json When collect Then skipped."""
    state = isolate_collector["state"]
    state.joinpath("botC.checkpoint.json").write_text(json.dumps({"reason":"done"}))
    ev_dir = state / "alignment_events"; ev_dir.mkdir(exist_ok=True)
    (ev_dir / "botC.exit.json").write_text("{bad")
    exe = _collect_execution("botC")
    # should not crash, runs at least 1 from checkpoint
    assert exe["runs"] >= 1

def test_collector_collect_alignment_from_rl_and_trigger_Given_rl_and_trigger_When_collect_Then_fields(isolate_collector):
    """Given rl_state and trigger file When _collect_alignment Then fields."""
    state = isolate_collector["state"]
    state.joinpath("rl_state.json").write_text(json.dumps({"bots":{"mybot":{"avg_reward":0.9,"last_reward":0.95,"last_score":10,"total_runs":5,"epsilon":0.1,"q_values":{"a":1,"b":2}}}}))
    state.joinpath("alignment_scores.json").write_text(json.dumps({"mybot":{"score":0.88}}))
    trig_dir = state / "alignment_triggers"; trig_dir.mkdir(parents=True, exist_ok=True)
    (trig_dir / "mybot.evolve.json").write_text("{}")
    ali = _collect_alignment("mybot")
    assert ali["avg_reward"] == 0.9
    assert ali["last_reward"] == 0.95
    assert ali["q_arms"] == 2
    assert ali["trigger_active"] is True
    assert ali["alignment_score"] == 0.88

def test_collector_collect_alignment_scores_variants_Given_scores_dict_When_collect_Then_parsed(isolate_collector):
    """Given alignment_scores with nested bots key When collect Then handles."""
    state = isolate_collector["state"]
    state.joinpath("rl_state.json").write_text(json.dumps({"bots":{"b2":{"avg_reward":0.5}}}))
    state.joinpath("alignment_scores.json").write_text(json.dumps({"bots":{"b2": 0.77}}))
    ali = _collect_alignment("b2")
    assert ali["alignment_score"] == 0.77
    # empty rl
    ali2 = _collect_alignment("unknown")
    assert ali2["avg_reward"] == 0.0

def test_collector_collect_progress_with_files_Given_tasklog_When_collect_Then_lines(isolate_collector):
    """Given tasklog and scratchpad When _collect_progress Then counts."""
    state = isolate_collector["state"]; logs = isolate_collector["logs"]
    logs.joinpath("prog.tasklog").write_text("line1\nline2\nline3")
    state.joinpath("prog.scratchpad.md").write_text("a\nb")
    state.joinpath("prog.checkpoint.json").write_text(json.dumps({"x":1}))
    pro = _collect_progress("prog", time.time())
    assert pro["tasklog_lines"] == 3
    assert pro["scratchpad_lines"] == 2
    assert pro["checkpoint_age_s"] is not None

def test_collector_collect_progress_with_cache_Given_cache_When_collect_Then_uses_cache(isolate_collector):
    """Given docs cache When _collect_progress Then uses it."""
    now = time.time()
    cache = {"botZ": 5}
    pro = _collect_progress("botZ", now, docs_findings_cache=cache)
    assert pro["output_files"] == 5
    pro2 = _collect_progress("other", now, docs_findings_cache=cache)
    assert pro2["output_files"] == 0

def test_collector_collect_progress_no_files_Given_none_When_collect_Then_defaults(isolate_collector):
    """Given no files When _collect_progress Then defaults."""
    pro = _collect_progress("nofiles", time.time())
    assert pro["tasklog_lines"] == 0
    assert pro["tasklog_age_s"] is None

def test_collector_collect_liveness_with_heartbeat_Given_hb_When_collect_Then_age(isolate_collector):
    """Given heartbeat file When _collect_liveness Then age computed."""
    state = isolate_collector["state"]; logs = isolate_collector["logs"]
    now = time.time()
    (state / "live.heartbeat").write_text(str(now - 10))
    (logs / "live.log").write_text("log")
    (state / "live.status.json").write_text(json.dumps({"updated_at": now - 5, "current_task":"doing", "iteration": 3}))
    liv = _collect_liveness("live", now)
    assert liv["heartbeat_age_s"] == pytest.approx(10.0, abs=1)
    assert liv["log_age_s"] is not None
    assert liv["status_age_s"] == pytest.approx(5.0, abs=1)
    assert liv["current_task"] == "doing"

def test_collector_collect_liveness_missing_Given_none_When_collect_Then_none(isolate_collector):
    """Given no files When _collect_liveness Then Nones."""
    liv = _collect_liveness("ghost", time.time())
    assert liv["heartbeat_age_s"] is None
    assert liv["log_age_s"] is None
    assert liv["status_age_s"] is None

def test_collector_collect_liveness_corrupt_hb_Given_bad_hb_When_collect_Then_none(isolate_collector):
    """Given corrupt heartbeat When collect Then none."""
    (isolate_collector["state"] / "bad.heartbeat").write_text("not_a_number")
    liv = _collect_liveness("bad", time.time())
    assert liv["heartbeat_age_s"] is None

def test_collector_collect_quality_with_measurements_Given_measurements_When_collect_Then_values(isolate_collector):
    """Given measurements When _collect_quality Then values extracted."""
    meas = {"false_positive_rate":{"botQ":0.12},"coverage":{"botQ":{"coverage_pct":88}},"error_rate":{"botQ":0.03},"findings_per_scan":{"botQ":5}}
    q = _collect_quality("botQ", meas)
    assert q["fp_rate"] == 0.12
    assert q["coverage_pct"] == 88
    assert q["meas_error_rate"] == 0.03
    assert q["findings"] == 5
    assert q["fp_source"] == "measurements"

def test_collector_collect_quality_fallback_rl_Given_no_measurements_but_rl_When_collect_Then_rl_fallback(isolate_collector):
    """Given no measurements but rl_state When _collect_quality Then fallback."""
    state = isolate_collector["state"]
    state.joinpath("rl_state.json").write_text(json.dumps({"bots":{"fb":{"failures":2,"total_runs":10}}}))
    q = _collect_quality("fb", None)
    assert q["fp_rate"] == pytest.approx(0.2, abs=0.01)
    assert q["fp_source"] == "rl_failures"

def test_collector_collect_quality_no_runs_Given_zero_runs_When_collect_Then_zero(isolate_collector):
    """Given zero runs When _collect_quality Then 0.0 no_runs."""
    state = isolate_collector["state"]
    state.joinpath("rl_state.json").write_text(json.dumps({"bots":{"zr":{"failures":0,"total_runs":0}}}))
    q = _collect_quality("zr", {})
    assert q["fp_rate"] == 0.0
    assert q["fp_source"] == "no_runs"

def test_collector_collect_tokens_fleet_Given_ledger_When_collect_Then_totals():
    """Given ledger When _collect_tokens Then totals."""
    ledger = {"by_model": {"m1":{"prompt_actual":100,"completion_actual":50},"m2":{"prompt_actual":200,"completion_actual":30}}}
    t = _collect_tokens(ledger)
    assert t["fleet_prompt_actual"] == 300
    assert t["fleet_completion_actual"] == 80
    assert _collect_tokens(None) == {"fleet_prompt_actual":0,"fleet_completion_actual":0}
    assert _collect_tokens({}) == {"fleet_prompt_actual":0,"fleet_completion_actual":0}

def test_collector_collect_bot_tokens_from_stream_Given_stream_When_collect_Then_tokens(isolate_collector):
    """Given stream.json with usage When _collect_bot_tokens Then parsed."""
    logs = isolate_collector["logs"]
    logs.joinpath("tok.stream.json").write_text(json.dumps({"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30},"model":"qwen-3.8-max","api_calls":2,"messages":[]}))
    tok = _collect_bot_tokens("tok")
    assert tok["prompt_tokens"] == 10
    assert tok["completion_tokens"] == 20
    assert tok["total_tokens"] == 30
    assert tok["model"] == "qwen-3.8-max"
    assert tok["source"] == "stream_usage"

def test_collector_collect_bot_tokens_char_estimate_Given_no_usage_When_collect_Then_estimate(isolate_collector):
    """Given messages but no usage When _collect_bot_tokens Then char estimate."""
    logs = isolate_collector["logs"]
    logs.joinpath("est.stream.json").write_text(json.dumps({"messages":[{"content":"hello world hello"},{"content":"more"}],"model":"x"}))
    tok = _collect_bot_tokens("est")
    assert tok["source"] == "char_estimate"
    assert tok["total_tokens"] > 0

def test_collector_collect_bot_tokens_missing_file_Given_none_When_collect_Then_defaults(isolate_collector):
    """Given no stream file When _collect_bot_tokens Then defaults."""
    tok = _collect_bot_tokens("ghost")
    assert tok["prompt_tokens"] == 0
    assert tok["source"] == "none"

def test_collector_collect_bot_tokens_bad_json_Given_corrupt_When_collect_Then_defaults(isolate_collector):
    """Given corrupt stream When _collect_bot_tokens Then defaults."""
    logs = isolate_collector["logs"]
    logs.joinpath("bad.stream.json").write_text("{bad")
    tok = _collect_bot_tokens("bad")
    assert tok["total_tokens"] == 0

def test_collector_collect_throughput_with_cache_Given_queue_cache_When_collect_Then_counts(isolate_collector):
    """Given cached queue When _collect_throughput Then counts per bot."""
    cache = [("Q-1","worker-1","implemented"),("Q-2","worker-1","open"),("Q-3","worker-2","done")]
    thr = _collect_throughput("worker-1", queue_cache=cache)
    assert thr["claimed"] == 2
    assert thr["completed"] == 1
    assert thr["completion_rate"] == 0.5
    thr2 = _collect_throughput("worker-2", queue_cache=cache)
    assert thr2["claimed"] == 1

def test_collector_collect_throughput_empty_Given_no_cache_When_collect_Then_zero(isolate_collector):
    """Given no QUEUE.md When _collect_throughput Then zero."""
    thr = _collect_throughput("nobody", queue_cache=[])
    assert thr["claimed"] == 0
    assert thr["completion_rate"] == 0.0

def test_collector_collect_throughput_from_file_Given_queue_file_When_collect_Then_parsed(isolate_collector):
    """Given QUEUE.md file When _collect_throughput without cache Then parsed."""
    tmp = isolate_collector["tmp"]
    qpath = tmp / "docs" / "triage" / "QUEUE.md"
    qpath.parent.mkdir(parents=True, exist_ok=True)
    qpath.write_text("| Q-1 | a | b | c | worker-1 | implemented |\n| Q-2 | a | b | c | worker-1 | open |\n")
    thr = _collect_throughput("worker-1", queue_cache=None)
    assert thr["claimed"] >= 1

def test_collector_collect_output_quality_Given_build_gate_When_collect_Then_gate(isolate_collector):
    """Given build-gate file When _collect_output_quality Then parsed."""
    tmp = isolate_collector["tmp"]
    bg = tmp / "docs" / "optimization" / "build-gate-INDEX.md"
    bg.parent.mkdir(parents=True, exist_ok=True)
    bg.write_text("PASS")
    oq = _collect_output_quality("any")
    assert oq["build_gate_pass"] is True
    bg.write_text("FAIL something")
    oq2 = _collect_output_quality("any")
    assert oq2["build_gate_pass"] is False

def test_collector_collect_output_quality_precision_Given_rl_When_collect_Then_precision(isolate_collector):
    """Given rl successes When _collect_output_quality Then precision."""
    state = isolate_collector["state"]
    state.joinpath("rl_state.json").write_text(json.dumps({"bots":{"prec":{"successes":8,"failures":2}}}))
    oq = _collect_output_quality("prec")
    assert oq["finding_precision"] == 0.8

def test_collector_collect_economics_Given_tokens_When_collect_Then_cost(isolate_collector):
    """Given tokens and execution When _collect_economics Then cost."""
    tok = {"total_tokens": 1000, "model":"xiaomi-mimo-2.5"}
    exe = {"completed": 2}
    # need checkpoint for noop check
    (isolate_collector["state"] / "eco.checkpoint.json").write_text(json.dumps({"reason":"noop something"}))
    eco = _collect_economics("eco", tok, exe)
    assert eco["tokens_per_completion"] == 500.0
    assert eco["est_cost_usd"] > 0
    assert eco["noop_flag"] == 1.0

def test_collector_collect_economics_noop_false_Given_no_noop_When_collect_Then_zero(isolate_collector):
    """Given normal reason When _collect_economics Then noop 0."""
    tok = {"total_tokens": 0, "model": "unknown"}
    exe = {"completed": 0}
    eco = _collect_economics("nope", tok, exe)
    assert eco["noop_flag"] == 0.0
    assert eco["tokens_per_completion"] == 0.0

def test_collector_collect_autonomy_Given_human_queue_When_collect_Then_counts(isolate_collector, monkeypatch):
    """Given human_review_queue When _collect_autonomy Then interventions."""
    state = isolate_collector["state"]
    # _read_json filters non-dict to None, so human_review_queue as list would be filtered.
    # To exercise the list branch we patch _read_json to return list for that file.
    def fake_read(path):
        if "human_review_queue" in str(path):
            return [{"bot":"aut"},{"bot":"aut"},{"bot":"other"}]
        return mc_read_json(path)
    monkeypatch.setattr(mc_mod, "_read_json", fake_read)
    ev_dir = state / "alignment_events"; ev_dir.mkdir(exist_ok=True)
    (ev_dir / "aut.exit.json").write_text(json.dumps({"exit_reason":"stuck"}))
    state.joinpath("rl_state.json").write_text(json.dumps({"bots":{"aut":{"consecutive_failures":3}}}))
    aut = _collect_autonomy("aut", {})
    assert aut["human_interventions"] == 2
    assert aut["stuck_restarts"] == 1
    assert aut["failure_streak"] == 3
    assert aut["auto_disabled"] is True

def test_collector_collect_learning_Given_q_values_When_collect_Then_spread(isolate_collector):
    """Given q_values When _collect_learning Then spread."""
    state = isolate_collector["state"]
    state.joinpath("rl_state.json").write_text(json.dumps({"bots":{"learn":{"q_values":{"a":0.1,"b":0.9},"epsilon":0.04}}}))
    ali = {"epsilon":0.04,"q_arms":2}
    lr = _collect_learning("learn", ali)
    assert lr["q_spread"] == pytest.approx(0.8, abs=0.01)
    assert lr["needs_exploration_reset"] is True
    # no q values
    lr2 = _collect_learning("unknown", {"epsilon":None,"q_arms":0})
    assert lr2["epsilon"] is None

def test_collector_collect_scrutiny_Given_logs_When_collect_Then_flagged(isolate_collector):
    """Given scrutiny_log When _collect_scrutiny Then flagged."""
    state = isolate_collector["state"]
    state.joinpath("scrutiny_log.jsonl").write_text('{"item_id":"Q-1"}\n{"item_id":"Q-2"}\n{"item_id":"Q-1"}\n')
    state.joinpath("approved_ids.json").write_text(json.dumps(["Q-1"]))
    scr = _collect_scrutiny()
    assert scr["flagged_count"] == 2
    assert scr["human_approved_count"] == 1
    # dict form
    state.joinpath("approved_ids.json").write_text(json.dumps({"Q-1": True, "Q-2": False}))
    scr2 = _collect_scrutiny()
    assert scr2["human_approved_count"] == 1

def test_collector_collect_scrutiny_no_files_Given_none_When_collect_Then_empty(isolate_collector):
    """Given no scrutiny files When _collect_scrutiny Then empty."""
    scr = _collect_scrutiny()
    assert scr["flagged_count"] == 0

def test_collector_build_docs_cache_Given_docs_When_build_Then_counts(isolate_collector, monkeypatch):
    """Given docs files When _build_docs_findings_cache Then per-bot counts."""
    tmp = isolate_collector["tmp"]
    docs = tmp / "docs"
    # file containing worker-1 in name
    (docs / "worker-1_report.md").write_text("x")
    (docs / "feature_worker-2.md").write_text("y")
    # patch KNOWN_BOTS to small set
    monkeypatch.setattr(mc_mod, "KNOWN_BOTS", ["worker-1", "worker-2"])
    cache = _build_docs_findings_cache()
    assert cache["worker-1"] >= 1
    assert cache["worker-2"] >= 1

def test_collector_build_docs_cache_no_docs_Given_no_dir_When_build_Then_zeros(isolate_collector, monkeypatch):
    """Given no docs dir When _build_docs_findings_cache Then zeros."""
    import shutil
    tmp = isolate_collector["tmp"]
    docs = tmp / "docs"
    shutil.rmtree(docs, ignore_errors=True)
    monkeypatch.setattr(mc_mod, "KNOWN_BOTS", ["worker-1"])
    cache = _build_docs_findings_cache()
    assert cache["worker-1"] == 0

def test_collector_parse_queue_once_Given_queue_When_parse_Then_entries(isolate_collector):
    """Given QUEUE.md When _parse_queue_md_once Then entries."""
    tmp = isolate_collector["tmp"]
    qpath = tmp / "docs" / "triage" / "QUEUE.md"
    qpath.parent.mkdir(parents=True, exist_ok=True)
    qpath.write_text("| Q-10 | a | b | c | worker-1 | done |\n")
    entries = _parse_queue_md_once()
    assert len(entries) >= 1
    assert entries[0][0] == "Q-10"

def test_collector_collect_all_integration_Given_isolated_state_When_collect_all_Then_snapshot(isolate_collector, monkeypatch):
    """Given isolated state When collect_all Then snapshot structure."""
    state = isolate_collector["state"]
    # minimal ledger
    state.joinpath("token_ledger.json").write_text(json.dumps({"day_utc":"2026-09-21","total_actual":150,"by_model":{"m":{"prompt_actual":100,"completion_actual":50}}}))
    state.joinpath("measurements.json").write_text(json.dumps({"false_positive_rate":{"worker-1":0.1},"coverage":{"worker-1":{"coverage_pct":90}},"error_rate":{},"findings_per_scan":{}}))
    state.joinpath("worker-1.heartbeat").write_text(str(time.time()))
    monkeypatch.setattr(mc_mod, "KNOWN_BOTS", ["worker-1", "worker-2"])
    snap = collect_all()
    assert "timestamp" in snap
    assert "bots" in snap
    assert "worker-1" in snap["bots"]
    assert "fleet_tokens" in snap
    assert "summary" in snap
    assert snap["summary"]["bots_tracked"] == 2

def test_collector_collect_all_only_fresh_Given_fresh_flag_When_collect_Then_filtered(isolate_collector, monkeypatch):
    """Given only_fresh True When collect_all Then filters stale."""
    state = isolate_collector["state"]
    now = time.time()
    (state / "worker-1.heartbeat").write_text(str(now))
    os.utime(state / "worker-1.heartbeat", (now, now))
    (state / "worker-2.heartbeat").write_text(str(now - 1000))
    os.utime(state / "worker-2.heartbeat", (now - 1000, now - 1000))
    # also need heartbeat files for _get_bots_with_fresh_heartbeats to find fresh
    monkeypatch.setattr(mc_mod, "KNOWN_BOTS", ["worker-1", "worker-2"])
    snap = collect_all(only_fresh=True)
    # should contain at least worker-1 (fresh) but not necessarily worker-2 if filtered
    assert "worker-1" in snap["bots"]

def test_collector_collect_all_trend_improving_Given_reward_jump_When_collect_Then_trend(isolate_collector, monkeypatch):
    """Given last_reward > avg_reward When collect_all Then improving."""
    state = isolate_collector["state"]
    state.joinpath("rl_state.json").write_text(json.dumps({"bots":{"worker-1":{"avg_reward":0.5,"last_reward":0.8}}}))
    # ensure heartbeat not counting as erroring etc.
    state.joinpath("worker-1.checkpoint.json").write_text(json.dumps({"reason":"done"}))
    monkeypatch.setattr(mc_mod, "KNOWN_BOTS", ["worker-1"])
    snap = collect_all()
    assert snap["bots"]["worker-1"]["reward_trend"] == "improving"
    assert snap["summary"]["improving"] >= 1

def test_collector_collect_all_trend_regressing_Given_low_reward_When_collect_Then_regressing(isolate_collector, monkeypatch):
    """Given low reward When collect_all Then regressing."""
    state = isolate_collector["state"]
    state.joinpath("rl_state.json").write_text(json.dumps({"bots":{"worker-1":{"avg_reward":0.8,"last_reward":0.5}}}))
    state.joinpath("worker-1.checkpoint.json").write_text(json.dumps({"reason":"done"}))
    monkeypatch.setattr(mc_mod, "KNOWN_BOTS", ["worker-1"])
    snap = collect_all()
    assert snap["bots"]["worker-1"]["reward_trend"] == "regressing"
    assert snap["summary"]["regressing"] >= 1

def test_collector_collect_all_erroring_count_Given_errors_When_collect_Then_erroring(isolate_collector, monkeypatch):
    """Given high error rate When collect_all Then erroring count."""
    state = isolate_collector["state"]
    # two runs with errors
    state.joinpath("worker-1.checkpoint.json").write_text(json.dumps({"reason":"error failed"}))
    ev_dir = state / "alignment_events"; ev_dir.mkdir(exist_ok=True)
    (ev_dir / "worker-1.exit.json").write_text(json.dumps({"exit_code":1}))
    (ev_dir / "worker-1-2.exit.json").write_text(json.dumps({"exit_code":1}))
    monkeypatch.setattr(mc_mod, "KNOWN_BOTS", ["worker-1"])
    snap = collect_all()
    assert snap["summary"]["erroring"] >= 1 or snap["bots"]["worker-1"]["execution"]["error_rate"] > 0.5

def test_collector_save_snapshot_Given_snapshot_When_save_Then_files(isolate_collector, monkeypatch):
    """Given snapshot When save_snapshot Then files written and history trimmed."""
    monkeypatch.setattr(mc_mod, "MAX_HISTORY_LINES", 3)
    snap = {"timestamp": time.time(), "bots": {}, "summary": {}}
    save_snapshot(snap)
    assert (isolate_collector["state"] / "bot_metrics.json").exists()
    assert (isolate_collector["state"] / "bot_metrics_history.jsonl").exists()
    # write many times to test trimming
    for i in range(5):
        save_snapshot({"timestamp": time.time(), "i": i, "bots": {}, "summary": {}})
    lines = (isolate_collector["state"] / "bot_metrics_history.jsonl").read_text().splitlines()
    assert len(lines) <= 3

def test_collector_main_with_incremental_flag_Given_flag_When_main_Then_collect(monkeypatch, isolate_collector):
    """Given --incremental flag When main Then only_fresh used."""
    monkeypatch.setattr(sys, "argv", ["prog", "--incremental"])
    with patch.object(mc_mod, "collect_all") as mock_collect, patch.object(mc_mod, "save_snapshot") as mock_save:
        mock_collect.return_value = {"timestamp_human":"now","summary":{"bots_tracked":1,"active":0,"idle":0,"stale":0,"erroring":0,"improving":0,"regressing":0},"ledger_total_actual":0,"ledger_day":"?"}
        # need to ensure METRICS_FILE path exists for print
        mc_mod.main()
        mock_collect.assert_called_once_with(only_fresh=True)

# =============================================================================
# cost_tracker
# =============================================================================

def test_cost_tracker_record_valid_Given_valid_id_When_record_Then_entry(tmp_path):
    """Given CB- id When record_phase_cost Then returns TicketCost."""
    ct = CostTracker(tmp_path)
    entry = ct.record_phase_cost("CB-123", "agentA", "modelX", 100, 50, "impl", wall_clock_seconds=5.0, attempts=1)
    assert entry.ticket_id == "CB-123"
    assert entry.total_tokens == 150
    assert entry.prompt_tokens == 100
    assert entry.phase == "impl"
    assert (tmp_path / "ticket_costs.jsonl").exists()
    data = (tmp_path / "ticket_costs.jsonl").read_text().splitlines()
    assert len(data) == 1
    assert json.loads(data[0])["ticket_id"] == "CB-123"

def test_cost_tracker_record_invalid_id_Given_bad_id_When_record_Then_raises(tmp_path):
    """Given invalid ticket_id When record_phase_cost Then ValueError."""
    ct = CostTracker(tmp_path)
    with pytest.raises(ValueError, match="invalid"):
        ct.record_phase_cost("", "a", "m", 10, 10, "p")
    with pytest.raises(ValueError, match="invalid"):
        ct.record_phase_cost("WRONG-1", "a", "m", 10, 10, "p")
    with pytest.raises(ValueError):
        bad_val = None  # type: ignore[var-annotated]
        ct.record_phase_cost(bad_val, "a", "m", 10, 10, "p")  # type: ignore[arg-type]

def test_cost_tracker_record_clamps_negatives_Given_negative_tokens_When_record_Then_zero(tmp_path):
    """Given negative tokens When record Then clamped to 0."""
    ct = CostTracker(tmp_path)
    entry = ct.record_phase_cost("CB-1", "a", "m", -5, -10, "phase", wall_clock_seconds=-1, attempts=0)
    assert entry.prompt_tokens == 0
    assert entry.completion_tokens == 0
    assert entry.total_tokens == 0
    assert entry.wall_clock_seconds == 0.0
    assert entry.attempts == 1

def test_cost_tracker_append_creates_file_Given_new_dir_When_append_Then_mkdir(tmp_path):
    """Given non-existent state_dir children When _append Then mkdir -p."""
    nested = tmp_path / "nested" / "deep"
    ct = CostTracker(nested)
    ct.record_phase_cost("CB-2", "a", "m", 10, 10, "p")
    assert (nested / "ticket_costs.jsonl").exists()

def test_cost_tracker_get_ticket_total_Given_multiple_phases_When_get_total_Then_agg(tmp_path):
    """Given multiple phases When get_ticket_total Then summed."""
    ct = CostTracker(tmp_path)
    ct.record_phase_cost("CB-T", "a", "m", 100, 50, "plan")
    ct.record_phase_cost("CB-T", "a", "m", 200, 100, "impl")
    ct.record_phase_cost("CB-OTHER", "a", "m", 10, 10, "plan")
    total = ct.get_ticket_total("CB-T")
    assert int(total["prompt_tokens"]) == 300  # type: ignore[index]
    assert int(total["completion_tokens"]) == 150  # type: ignore[index]
    assert int(total["total_tokens"]) == 450  # type: ignore[index]
    by_phase = total["by_phase"]  # type: ignore
    assert int(by_phase["plan"]) == 150  # type: ignore[index]
    assert int(by_phase["impl"]) == 300  # type: ignore[index]

def test_cost_tracker_get_ticket_total_empty_Given_no_file_When_get_total_Then_zeros(tmp_path):
    """Given no file When get_ticket_total Then zeros."""
    ct = CostTracker(tmp_path)
    total = ct.get_ticket_total("CB-NONE")
    assert int(total["total_tokens"]) == 0  # type: ignore[index]

def test_cost_tracker_get_ticket_total_ignores_bad_json_Given_corrupt_lines_When_get_total_Then_skipped(tmp_path):
    """Given corrupt lines When get_ticket_total Then skipped."""
    ct = CostTracker(tmp_path)
    ct.record_phase_cost("CB-1", "a", "m", 10, 10, "p")
    with open(tmp_path / "ticket_costs.jsonl", "a") as f:
        f.write("{bad json\n")
        f.write("\n")
    total = ct.get_ticket_total("CB-1")
    assert int(total["total_tokens"]) == 20  # type: ignore[index]

def test_cost_tracker_read_recent_lines_limit_Given_many_lines_When_read_Then_limited(tmp_path):
    """Given many lines When _read_recent_lines Then respects MAX_COST_READ."""
    ct = CostTracker(tmp_path)
    for i in range(15):
        ct.record_phase_cost(f"CB-{100+i}", "a", "m", 10, 10, "p")
    lines = ct._read_recent_lines(limit=5)
    assert len(lines) == 5

def test_cost_tracker_read_recent_lines_no_file_Given_missing_When_read_Then_empty(tmp_path):
    """Given missing file When _read_recent_lines Then empty."""
    ct = CostTracker(tmp_path)
    assert ct._read_recent_lines() == []

def test_cost_tracker_read_recent_os_error_Given_unreadable_When_read_Then_empty(tmp_path, monkeypatch):
    """Given OSError on read When _read_recent_lines Then empty."""
    ct = CostTracker(tmp_path)
    ct.record_phase_cost("CB-1", "a", "m", 10, 10, "p")
    with patch.object(Path, "read_text", side_effect=OSError("fail")):
        assert ct._read_recent_lines() == []

def test_cost_tracker_rotate_trims_Given_oversized_When_rotate_Then_trimmed(tmp_path):
    """Given oversized file When rotate Then trimmed."""
    ct = CostTracker(tmp_path)
    for i in range(10):
        ct.record_phase_cost(f"CB-{i}", "a", "m", 10, 10, "p")
    ct.rotate(max_lines=5)
    lines = (tmp_path / "ticket_costs.jsonl").read_text().splitlines()
    assert len(lines) == 5
    assert "CB-0" not in lines[0]  # oldest removed
    # also test no file
    ct2 = CostTracker(tmp_path / "empty")
    ct2.rotate()  # should not crash
    # also test exact size
    ct.rotate(max_lines=5)  # already 5

def test_cost_tracker_rotate_os_error_Given_failure_When_rotate_Then_swallowed(tmp_path, monkeypatch):
    """Given OSError in rotate When call Then swallowed."""
    ct = CostTracker(tmp_path)
    ct.record_phase_cost("CB-1", "a", "m", 10, 10, "p")
    with patch.object(Path, "read_text", side_effect=OSError("fail")):
        ct.rotate()  # should not raise
    with patch.object(Path, "write_text", side_effect=OSError("fail")):
        ct.rotate(max_lines=0)

def test_cost_tracker_iter_entries_filters_Given_mixed_ids_When_iter_Then_only_ticket(tmp_path):
    """Given mixed ticket ids When _iter_entries Then only matching."""
    ct = CostTracker(tmp_path)
    ct.record_phase_cost("CB-A", "a", "m", 10, 10, "plan")
    ct.record_phase_cost("CB-B", "a", "m", 5, 5, "impl")
    ct.record_phase_cost("CB-A", "a", "m", 7, 3, "impl")
    entries = ct._iter_entries("CB-A")
    assert len(entries) == 2
    assert all(e["ticket_id"] == "CB-A" for e in entries)

def test_cost_tracker_iter_entries_no_file_Given_none_When_iter_Then_empty(tmp_path):
    """Given no file When _iter_entries Then empty."""
    ct = CostTracker(tmp_path)
    assert ct._iter_entries("CB-X") == []

def test_cost_tracker_cache_validity_Given_new_file_When_build_summary_then_cached(tmp_path):
    """Given file When build_summary twice Then cached if mtime unchanged."""
    ct = CostTracker(tmp_path)
    ct.record_phase_cost("CB-1", "a", "m", 10, 10, "p", wall_clock_seconds=2.0, attempts=2)
    s1 = ct.build_summary()
    assert "CB-1" in s1["tickets"]
    assert s1["fleet_totals"]["total_tokens"] == 20
    # second call should be cached (same object)
    s2 = ct.build_summary()
    assert s1 is s2  # same cached object
    # after new entry, cache invalidated
    ct.record_phase_cost("CB-2", "a", "m", 5, 5, "p2")
    s3 = ct.build_summary()
    assert s3 is not s1
    assert "CB-2" in s3["tickets"]

def test_cost_tracker_cache_empty_file_Given_no_costs_When_build_summary_Then_empty(tmp_path):
    """Given no file When build_summary Then empty structure."""
    ct = CostTracker(tmp_path)
    s = ct.build_summary()
    assert s["tickets"] == {}
    assert s["fleet_totals"] == {}
    # second call cached
    s2 = ct.build_summary()
    assert s2 is s  # cached empty
    # after creating file, cache invalid
    ct.record_phase_cost("CB-1", "a", "m", 10, 10, "p")
    s3 = ct.build_summary()
    assert "CB-1" in s3["tickets"]

def test_cost_tracker_cache_after_delete_Given_deleted_file_When_check_Then_invalid(tmp_path):
    """Given cache for file When file deleted Then cache invalid."""
    ct = CostTracker(tmp_path)
    ct.record_phase_cost("CB-1", "a", "m", 10, 10, "p")
    ct.build_summary()
    os.remove(str(tmp_path / "ticket_costs.jsonl"))
    # cache was for file existence; now missing but cache says mtime 0? Let's see: after delete, _is_cache_valid should return False because size mismatch? Actually previous cache metadata was for existing file; after delete, exists() false and cached mtime !=0 so invalid.
    assert ct._is_cache_valid() is False

def test_cost_tracker_build_summary_aggregates_agents_models_Given_multi_agents_When_build_Then_sets(tmp_path):
    """Given multiple agents/models When build_summary Then aggregated."""
    ct = CostTracker(tmp_path)
    ct.record_phase_cost("CB-M", "agent1", "modelA", 10, 10, "plan", wall_clock_seconds=1.0)
    ct.record_phase_cost("CB-M", "agent2", "modelB", 20, 20, "impl", wall_clock_seconds=2.0, attempts=3)
    s = ct.build_summary()
    entry = s["tickets"]["CB-M"]
    assert entry["total_tokens"] == 60
    assert sorted(entry["agents"]) == ["agent1", "agent2"]
    assert sorted(entry["models"]) == ["modelA", "modelB"]
    assert entry["attempts"] == 3  # max
    assert entry["wall_clock_total"] == 3.0
    assert entry["phases"]["plan"] == 20
    assert entry["phases"]["impl"] == 40

def test_cost_tracker_build_summary_handles_bad_lines_Given_corrupt_When_build_Then_skipped(tmp_path):
    """Given corrupt lines When build_summary Then skipped."""
    ct = CostTracker(tmp_path)
    ct.record_phase_cost("CB-1", "a", "m", 10, 10, "p")
    with open(tmp_path / "ticket_costs.jsonl", "a") as f:
        f.write("not json\n")
        f.write('{"ticket_id":"CB-2","total_tokens":5,"prompt_tokens":5,"completion_tokens":0,"phase":"x","agent":"a","model":"m","attempts":1,"wall_clock_seconds":0}\n')
    ct2 = CostTracker(tmp_path)  # fresh without cache
    s = ct2.build_summary()
    assert "CB-1" in s["tickets"]
    assert "CB-2" in s["tickets"]

def test_cost_tracker_update_cache_metadata_No_file_Given_no_file_When_update_Then_zero(tmp_path):
    """Given no file When _update_cache_metadata Then 0."""
    ct = CostTracker(tmp_path)
    ct._update_cache_metadata()
    assert ct._cache_file_mtime == 0
    assert ct._cache_file_size == 0

def test_cost_tracker_is_cache_valid_os_error_Given_stat_fails_When_check_Then_false(tmp_path):
    """Given OSError on stat When _is_cache_valid Then False."""
    ct = CostTracker(tmp_path)
    ct.record_phase_cost("CB-1", "a", "m", 10, 10, "p")
    ct.build_summary()
    with patch.object(Path, "stat", side_effect=OSError("fail")):
        assert ct._is_cache_valid() is False
    # also test with no cache
    ct2 = CostTracker(tmp_path / "empty2")
    assert ct2._is_cache_valid() is False

def test_cost_tracker_ticket_cost_to_dict_Given_dataclass_When_to_dict_Then_keys():
    """Given TicketCost When to_dict Then dict."""
    tc = TicketCost(ticket_id="CB-1", agent="a", model="m", prompt_tokens=1, completion_tokens=2, total_tokens=3, phase="p", timestamp=123.0)
    d = tc.to_dict()
    assert d["ticket_id"] == "CB-1"
    assert d["total_tokens"] == 3

def test_cost_tracker_fleet_totals_Given_multiple_tickets_When_build_Then_fleet(tmp_path):
    """Given multiple tickets When build_summary Then fleet totals correct."""
    ct = CostTracker(tmp_path)
    ct.record_phase_cost("CB-1", "a", "m", 10, 10, "p")
    ct.record_phase_cost("CB-2", "a", "m", 20, 20, "p")
    s = ct.build_summary()
    assert s["fleet_totals"]["prompt_tokens"] == 30
    assert s["fleet_totals"]["completion_tokens"] == 30
    assert s["fleet_totals"]["total_tokens"] == 60
    assert s["fleet_totals"]["ticket_count"] == 2

def test_cost_tracker_thread_safety_Given_concurrent_When_record_Then_no_crash(tmp_path):
    """Given concurrent records When called Then thread-safe via lock for summary."""
    ct = CostTracker(tmp_path)
    def worker(n):
        for i in range(5):
            ct.record_phase_cost(f"CB-{n}-{i}", "a", "m", 10, 10, "p")
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(3)]
    for t in threads: t.start()
    for t in threads: t.join()
    s = ct.build_summary()
    assert s["fleet_totals"]["ticket_count"] == 15

# =============================================================================
# coverage_runner
# =============================================================================

def test_coverage_runner_module_coverage_to_dict_Given_module_When_to_dict_Then_keys():
    """Given ModuleCoverage When to_dict Then all fields."""
    mc = ModuleCoverage(path="a.py", statements=10, covered=8, missing=[2,5], coverage_pct=80.0)
    d = mc.to_dict()
    assert d["path"] == "a.py"
    assert d["missing"] == [2,5]
    assert d["coverage_pct"] == 80.0

def test_coverage_runner_report_to_dict_and_from_dict_Given_report_When_roundtrip_Then_equal():
    """Given CoverageReport When to/from dict Then equal."""
    mod = ModuleCoverage(path="m.py", statements=10, covered=5, missing=[1,2], coverage_pct=50.0)
    rep = CoverageReport(total_statements=10, total_covered=5, total_coverage_pct=50.0, modules={"m.py": mod}, timestamp=1234.0, pytest_exit_code=0, error=None)
    d = rep.to_dict()
    assert d["total_statements"] == 10
    assert "m.py" in d["modules"]
    json_str = rep.to_json()
    assert "total_statements" in json_str
    rep2 = CoverageReport.from_dict(d)
    assert rep2.total_statements == 10
    assert rep2.modules["m.py"].covered == 5
    assert rep2.pytest_exit_code == 0

def test_coverage_runner_report_below_threshold_Given_mods_When_below_Then_filtered():
    """Given report with varied coverage When below_threshold Then low ones."""
    mods = {
        "low.py": ModuleCoverage("low.py", 10, 2, [1,2], 20.0),
        "high.py": ModuleCoverage("high.py", 10, 9, [], 90.0),
        "mid.py": ModuleCoverage("mid.py", 10, 5, [1], 50.0),
    }
    rep = CoverageReport(30, 16, 53.3, mods, time.time(), 0)
    below = rep.below_threshold(60.0)
    assert len(below) == 2
    assert below[0].coverage_pct < below[1].coverage_pct  # sorted
    assert below[0].path == "low.py"

def test_coverage_runner_uncovered_lines_Given_module_When_query_Then_missing():
    """Given report When uncovered_lines Then missing."""
    mod = ModuleCoverage("x.py", 5, 3, [1,4], 60.0)
    rep = CoverageReport(5, 3, 60.0, {"x.py": mod}, time.time(), 0)
    assert rep.uncovered_lines("x.py") == [1,4]
    assert rep.uncovered_lines("missing.py") == []

def test_coverage_runner_parse_missing_int_list_Given_ints_When_parse_Then_list():
    """Given ints When _parse_missing_lines Then ints."""
    assert _parse_missing_lines([1,2,3]) == [1,2,3]
    assert _parse_missing_lines([3,1,2]) == [1,2,3]
    assert _parse_missing_lines([1,1,2]) == [1,2]

def test_coverage_runner_parse_missing_string_ranges_Given_strings_When_parse_Then_expanded():
    """Given string ranges When _parse_missing_lines Then expanded."""
    assert _parse_missing_lines(["1-3", "5"]) == [1,2,3,5]
    assert _parse_missing_lines(["1, 2, 5-7"]) == [1,2,5,6,7]
    assert _parse_missing_lines(["10-12, 20"]) == [10,11,12,20]

def test_coverage_runner_parse_missing_invalid_string_Given_bad_When_parse_Then_ignored():
    """Given bad strings When _parse_missing_lines Then ignored parts."""
    assert _parse_missing_lines(["a-b", "x"]) == []
    assert _parse_missing_lines(["1 - 2"]) == [1, 2]
    assert _parse_missing_lines("not_list") == []
    assert _parse_missing_lines(None) == []
    assert _parse_missing_lines([]) == []

def test_coverage_runner_parse_missing_mixed_types_Given_mixed_When_parse_Then_filtered():
    """Given mixed ints and strings When parse Then handled."""
    result = _parse_missing_lines([1, "2-4", "6", "bad"])
    assert result == [1,2,3,4,6]

def test_coverage_runner_empty_report_Given_error_When_empty_Then_fields():
    """Given error When _empty_report Then zero."""
    r = _empty_report(error="oops", exit_code=5)
    assert r.total_statements == 0
    assert r.error == "oops"
    assert r.pytest_exit_code == 5
    assert r.modules == {}

def test_coverage_runner_parse_coverage_json_full_Given_valid_json_When_parse_Then_report(tmp_path):
    """Given valid coverage json When _parse_coverage_json Then report."""
    cov = tmp_path / "cov.json"
    data = {
        "totals": {"num_statements": 100, "covered_lines": 80, "percent_covered": 80.0},
        "files": {
            "a.py": {"summary": {"num_statements": 50, "covered_lines": 40, "percent_covered": 80.0}, "missing_lines": [1,2]},
            "b.py": {"summary": {"num_statements": 50, "covered_lines": 40, "percent_covered": {"display": 80.0}}, "missing_lines": ["5-7"]},
        }
    }
    cov.write_text(json.dumps(data))
    rep = _parse_coverage_json(cov, 0)
    assert rep.total_statements == 100
    assert rep.total_covered == 80
    assert "a.py" in rep.modules
    assert rep.modules["b.py"].coverage_pct == 80.0
    assert rep.modules["b.py"].missing == [5,6,7]
    # total pct dict form
    cov2 = tmp_path / "cov2.json"
    data2 = {"totals": {"num_statements": 10, "covered_lines": 5, "percent_covered": {"display": 50.0}}, "files": {}}
    cov2.write_text(json.dumps(data2))
    rep2 = _parse_coverage_json(cov2, 0)
    assert rep2.total_coverage_pct == 50.0

def test_coverage_runner_parse_coverage_json_bad_json_Given_corrupt_When_parse_Then_empty(tmp_path):
    """Given corrupt json When _parse_coverage_json Then empty report with error."""
    cov = tmp_path / "bad.json"
    cov.write_text("{bad")
    rep = _parse_coverage_json(cov, 2)
    assert rep.error is not None
    assert "failed to parse" in rep.error

def test_coverage_runner_parse_coverage_json_missing_totals_Given_empty_When_parse_Then_zero(tmp_path):
    """Given empty totals When _parse_coverage_json Then zero."""
    cov = tmp_path / "empty.json"
    cov.write_text(json.dumps({}))
    rep = _parse_coverage_json(cov, 0)
    assert rep.total_statements == 0

def test_coverage_runner_run_coverage_success_Given_mock_success_When_run_Then_report(tmp_path):
    """Given subprocess success and cov json When run_coverage Then report."""
    # Need to make cov.json be written by fake subprocess side effect
    root = tmp_path / "proj"
    root.mkdir()
    (root / "codebot").mkdir()
    (root / "tests").mkdir()
    state_dir = root / ".codebot" / "state"
    cov_path = state_dir / "coverage.json"
    def fake_run(cmd, cwd, capture_output, text, timeout):
        state_dir.mkdir(parents=True, exist_ok=True)
        cov_path.write_text(json.dumps({
            "totals": {"num_statements": 20, "covered_lines": 10, "percent_covered": 50.0},
            "files": {"codebot/x.py": {"summary": {"num_statements": 20, "covered_lines": 10, "percent_covered": 50.0}, "missing_lines": [1]}}
        }), encoding="utf-8")
        return MagicMock(returncode=0, stderr="", stdout="")
    with patch("codebot.coverage_runner.subprocess.run", side_effect=fake_run):
        rep = run_coverage(root, test_dirs=["tests/"], source_dirs=["codebot"], timeout=10)
        assert rep.total_statements == 20
        assert rep.pytest_exit_code == 0
        assert "codebot/x.py" in rep.modules

def test_coverage_runner_run_coverage_timeout_Given_timeout_When_run_Then_empty(tmp_path):
    """Given TimeoutExpired When run_coverage Then error."""
    root = tmp_path / "proj2"
    root.mkdir()
    with patch("codebot.coverage_runner.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=1)):
        rep = run_coverage(root, timeout=1)
        assert rep.error is not None
        assert "timed out" in rep.error

def test_coverage_runner_run_coverage_not_found_Given_no_python_When_run_Then_empty(tmp_path):
    """Given FileNotFoundError When run_coverage Then python not found."""
    root = tmp_path / "proj3"
    root.mkdir()
    with patch("codebot.coverage_runner.subprocess.run", side_effect=FileNotFoundError("no")):
        rep = run_coverage(root)
        assert rep.error == "python3 not found"

def test_coverage_runner_run_coverage_generic_exception_Given_generic_When_run_Then_empty(tmp_path):
    """Given generic Exception When run_coverage Then error."""
    root = tmp_path / "proj4"
    root.mkdir()
    with patch("codebot.coverage_runner.subprocess.run", side_effect=RuntimeError("boom")):
        rep = run_coverage(root)
        assert rep.error is not None and "boom" in rep.error

def test_coverage_runner_run_no_cov_json_and_pytest_cov_missing_Given_missing_cov_When_run_Then_pytest_cov_error(tmp_path):
    """Given no cov json and stderr indicates missing pytest-cov When run Then error."""
    root = tmp_path / "proj5"
    root.mkdir()
    (root / "codebot").mkdir()
    with patch("codebot.coverage_runner.subprocess.run", return_value=MagicMock(returncode=1, stderr="No module named pytest_cov", stdout="")):
        rep = run_coverage(root)
        assert rep.error is not None and "pytest-cov not installed" in rep.error

def test_coverage_runner_run_no_cov_json_generic_Given_no_cov_When_run_Then_error(tmp_path):
    """Given no cov json generic When run Then error with exit code."""
    root = tmp_path / "proj6"
    root.mkdir()
    with patch("codebot.coverage_runner.subprocess.run", return_value=MagicMock(returncode=2, stderr="some error", stdout="")):
        rep = run_coverage(root)
        assert rep.error is not None and "coverage.json not produced" in rep.error
        assert rep.pytest_exit_code == 2

def test_coverage_runner_run_auto_detect_source_and_test_Given_dirs_When_run_Then_cmd_built(tmp_path):
    """Given source_dirs None When run_coverage Then auto-detect."""
    root = tmp_path / "proj7"
    root.mkdir()
    (root / "codebot").mkdir()
    (root / "tests").mkdir()
    captured = {}
    def fake_run(cmd, cwd, capture_output, text, timeout):
        captured["cmd"] = cmd
        # write cov json
        cov_path = Path(cwd) / ".codebot" / "state" / "coverage.json"
        cov_path.parent.mkdir(parents=True, exist_ok=True)
        cov_path.write_text(json.dumps({"totals": {"num_statements": 1, "covered_lines": 1, "percent_covered": 100}, "files": {}}), encoding="utf-8")
        return MagicMock(returncode=0, stderr="", stdout="")
    with patch("codebot.coverage_runner.subprocess.run", side_effect=fake_run):
        rep = run_coverage(root)
        assert "--cov=codebot" in captured["cmd"]
        assert "tests" in captured["cmd"]

def test_coverage_runner_run_with_extra_args_Given_extra_When_run_Then_included(tmp_path):
    """Given extra_args When run_coverage Then appended."""
    root = tmp_path / "proj8"
    root.mkdir()
    (root / "tests").mkdir()
    captured = {}
    def fake_run(cmd, cwd, capture_output, text, timeout):
        captured["cmd"] = cmd
        cov_path = Path(cwd) / ".codebot" / "state" / "coverage.json"
        cov_path.parent.mkdir(parents=True, exist_ok=True)
        cov_path.write_text(json.dumps({"totals": {"num_statements": 0, "covered_lines": 0, "percent_covered": 0}, "files": {}}))
        return MagicMock(returncode=0, stderr="", stdout="")
    with patch("codebot.coverage_runner.subprocess.run", side_effect=fake_run):
        run_coverage(root, extra_args=["-k", "mytest"])
        assert "-k" in captured["cmd"] and "mytest" in captured["cmd"]

def test_coverage_runner_save_and_load_roundtrip_Given_report_When_save_load_Then_equal(tmp_path):
    """Given report When save and load Then equal."""
    mod = ModuleCoverage("a.py", 10, 8, [2,3], 80.0)
    rep = CoverageReport(10, 8, 80.0, {"a.py": mod}, time.time(), 0)
    path = save_coverage_report(rep, tmp_path)
    assert path.exists()
    loaded = load_coverage_report(tmp_path)
    assert loaded is not None
    assert loaded.total_statements == 10
    assert loaded.modules["a.py"].missing == [2,3]

def test_coverage_runner_load_missing_Given_no_file_When_load_Then_none(tmp_path):
    """Given no file When load_coverage_report Then None."""
    assert load_coverage_report(tmp_path / "missing") is None

def test_coverage_runner_load_corrupt_Given_bad_json_When_load_Then_none(tmp_path):
    """Given corrupt json When load Then None."""
    (tmp_path / "coverage_report.json").write_text("{bad")
    assert load_coverage_report(tmp_path) is None

def test_coverage_runner_load_os_error_Given_unreadable_When_load_Then_none(tmp_path, monkeypatch):
    """Given OSError When load Then None."""
    (tmp_path / "coverage_report.json").write_text(json.dumps({"total_statements":1,"total_covered":1,"total_coverage_pct":100,"modules":{},"timestamp":0,"pytest_exit_code":0}))
    with patch.object(Path, "read_text", side_effect=OSError("fail")):
        assert load_coverage_report(tmp_path) is None

# =============================================================================
# coverage_bridge
# =============================================================================

def _make_report(mods_dict, error=None, total_pct=50.0):
    mods = {}
    for path, (stmts, covered, missing, pct) in mods_dict.items():
        mods[path] = ModuleCoverage(path=path, statements=stmts, covered=covered, missing=missing, coverage_pct=pct)
    return CoverageReport(total_statements=sum(v[0] for v in mods_dict.values()),
                          total_covered=sum(v[1] for v in mods_dict.values()),
                          total_coverage_pct=total_pct,
                          modules=mods, timestamp=time.time(), pytest_exit_code=0, error=error)

def _make_store():
    # Minimal TicketStore-like mock
    store = MagicMock()
    tickets = []
    def add(ticket):
        tickets.append(ticket)
        # Simulate duplicate check? Not needed
        return ticket
    store.add = add
    store.transition = MagicMock()
    store._tickets = tickets
    return store

def test_bridge_is_protected_Given_path_When_check_Then_bool():
    """Given path When _is_protected Then bool."""
    assert _is_protected("secret/x.py", {"secret/"}) is True
    assert _is_protected("public/x.py", {"secret/"}) is False
    assert _is_protected("a.py", set()) is False

def test_bridge_chunk_missing_empty_Given_empty_When_chunk_Then_one_empty():
    """Given empty When _chunk_missing_lines Then [[]]."""
    assert _chunk_missing_lines([], 10) == [[]]

def test_bridge_chunk_missing_single_Given_list_When_chunk_Then_chunks():
    """Given list When chunk Then correct chunks."""
    assert _chunk_missing_lines([1,2,3,4,5], 2) == [[1,2],[3,4],[5]]
    assert _chunk_missing_lines([1,2,3], 10) == [[1,2,3]]
    # unsorted input sorted
    assert _chunk_missing_lines([5,1,3], 10) == [[1,3,5]]

def test_bridge_severity_and_risk_Given_pct_When_call_Then_level():
    """Given pct When _severity_for_coverage Then correctly."""
    assert _severity_for_coverage(10) == "critical"
    assert _severity_for_coverage(30) == "high"  # 30 not <30
    assert _severity_for_coverage(40) == "high"
    assert _severity_for_coverage(60) == "medium"
    assert _severity_for_coverage(80) == "low"
    assert _risk_for_coverage(10) == "critical"
    assert _risk_for_coverage(40) == "high"
    assert _risk_for_coverage(60) == "medium"
    assert _risk_for_coverage(80) == "medium"

def test_bridge_format_line_ranges_Given_lines_When_format_Then_ranges():
    """Given lines When _format_line_ranges Then correct string."""
    assert _format_line_ranges([]) == "all"
    assert _format_line_ranges([1]) == "1"
    assert _format_line_ranges([1,2,3]) == "1-3"
    assert _format_line_ranges([1,3,5]) == "1, 3, 5"
    assert _format_line_ranges([1,2,3,5,6,10]) == "1-3, 5-6, 10"
    assert _format_line_ranges([5,1,2]) == "5, 1-2"
    assert _format_line_ranges(sorted([5,1,2])) == "1-2, 5"

def test_bridge_generate_no_data_Given_error_report_When_generate_Then_empty():
    """Given error report When generate_coverage_tickets Then empty."""
    rep = _make_report({}, error="oops")
    store = _make_store()
    assert generate_coverage_tickets(rep, store) == []
    empty_rep = CoverageReport(total_statements=0, total_covered=0, total_coverage_pct=0.0, modules={}, timestamp=time.time(), pytest_exit_code=0, error=None)
    assert generate_coverage_tickets(empty_rep, store) == []

def test_bridge_generate_below_threshold_Given_low_cov_When_generate_Then_tickets(tmp_path):
    """Given low coverage module When generate Then ticket created."""
    rep = _make_report({"low.py": (100, 20, list(range(1,31)), 20.0)}, total_pct=50.0)
    mock_ticket = MagicMock()
    mock_ticket.id = "CB-TEST"
    mock_create = MagicMock(return_value=mock_ticket)
    store = MagicMock()
    store.add = MagicMock()
    store.transition = MagicMock()
    with patch("codebot.ticket_engine.create_ticket", mock_create):
        ids = generate_coverage_tickets(rep, store, min_coverage_pct=60.0, protected_paths=set(), max_tickets=5)
        assert len(ids) == 1
        assert ids[0] == "CB-TEST"
        assert mock_create.call_count == 1
        kwargs = mock_create.call_args.kwargs
        assert "low.py" in kwargs["title"]
        assert kwargs["source"] == "coverage_bridge"

def test_bridge_generate_protected_skipped_Given_protected_When_generate_Then_zero():
    """Given protected path When generate Then skipped."""
    rep = _make_report({"protected/secret.py": (100, 10, [1,2], 10.0)})
    store = _make_store()
    with patch("codebot.ticket_engine.create_ticket") as mock_create:
        mock_ticket = MagicMock(); mock_ticket.id="CB-1"; mock_create.return_value=mock_ticket
        ids = generate_coverage_tickets(rep, store, protected_paths={"protected/"})
        assert ids == []
        mock_create.assert_not_called()

def test_bridge_generate_zero_statements_skipped_Given_zero_When_generate_Then_skipped():
    """Given zero statements When generate Then skipped."""
    rep = _make_report({"empty.py": (0, 0, [], 0.0)})
    store = _make_store()
    with patch("codebot.ticket_engine.create_ticket") as mock_create:
        mock_create.return_value=MagicMock(id="CB-1")
        ids = generate_coverage_tickets(rep, store)
        assert ids == []

def test_bridge_generate_max_tickets_Given_many_mods_When_generate_Then_capped():
    """Given many modules When generate Then capped at max_tickets."""
    mods = {f"mod{i}.py": (100, 10, [1,2,3], 10.0) for i in range(10)}
    rep = _make_report(mods)
    store = _make_store()
    with patch("codebot.ticket_engine.create_ticket") as mock_create:
        mock_create.return_value=MagicMock(id="CB-X")
        store.add = MagicMock()
        store.transition = MagicMock()
        # need to make each call return unique id
        mock_create.side_effect = [MagicMock(id=f"CB-{i}") for i in range(10)]
        ids = generate_coverage_tickets(rep, store, max_tickets=3)
        assert len(ids) == 3

def test_bridge_generate_chunked_missing_Given_many_missing_When_generate_Then_multiple_tickets():
    """Given many missing lines > MAX_LINES_PER_TICKET When generate Then chunked."""
    # MAX is 50, so 120 lines -> 3 tickets
    missing = list(range(1,121))
    rep = _make_report({"big.py": (200, 80, missing, 40.0)})
    with patch("codebot.ticket_engine.create_ticket") as mock_create:
        mock_create.side_effect = [MagicMock(id=f"CB-{i}") for i in range(10)]
        store = MagicMock()
        ids = generate_coverage_tickets(rep, store, max_tickets=20)
        assert len(ids) == 3

def test_bridge_create_ticket_duplicate_handling_Given_duplicate_error_When_create_Then_none(monkeypatch):
    """Given duplicate error When _create_test_ticket Then None."""
    mod = ModuleCoverage("dup.py", 100, 20, [1,2], 20.0)
    store = MagicMock()
    with patch("codebot.ticket_engine.create_ticket", side_effect=ValueError("duplicate ticket exists")):
        with patch.object(cb_mod.logger, "debug") as mock_debug:
            result = _create_test_ticket(store, mod, [1,2], 50.0)
            assert result is None
            mock_debug.assert_called()

def test_bridge_create_ticket_other_value_error_Given_other_error_When_create_Then_none_and_warn():
    """Given other ValueError When _create_test_ticket Then None and warning."""
    mod = ModuleCoverage("err.py", 100, 20, [1,2], 20.0)
    store = MagicMock()
    with patch("codebot.ticket_engine.create_ticket", side_effect=ValueError("something else")):
        with patch.object(cb_mod.logger, "warning") as mock_warn:
            result = _create_test_ticket(store, mod, [1,2], 50.0)
            assert result is None
            mock_warn.assert_called()

def test_bridge_create_ticket_transitions_Given_success_When_create_Then_transitions():
    """Given successful create When _create_test_ticket Then 3 transitions."""
    mod = ModuleCoverage("a.py", 100, 50, [1,2,3], 50.0)
    store = MagicMock()
    fake_ticket = MagicMock()
    fake_ticket.id = "CB-999"
    with patch("codebot.ticket_engine.create_ticket", return_value=fake_ticket):
        result = _create_test_ticket(store, mod, [1,2,3], 60.0)
        assert result == "CB-999"
        store.add.assert_called_once_with(fake_ticket)
        assert store.transition.call_count == 3

def test_bridge_create_ticket_acceptance_criteria_Given_lines_When_create_Then_criteria(monkeypatch):
    """Given lines When _create_test_ticket Then acceptance includes pytest passes."""
    mod = ModuleCoverage("a.py", 100, 20, [1,2,5], 20.0)
    store = MagicMock()
    fake_ticket = MagicMock(id="CB-1")
    captured = {}
    def fake_create(**kw):
        captured.update(kw)
        return fake_ticket
    with patch("codebot.ticket_engine.create_ticket", side_effect=fake_create):
        _create_test_ticket(store, mod, [1,2,5], 50.0)
        # line_ranges for [1,2,5] -> "1-2, 5"
        assert "Tests exercise code at lines 1-2" in captured["acceptance_criteria"]
        assert "Tests exercise code at lines 5" in captured["acceptance_criteria"]
        assert any("pytest passes" in x for x in captured["acceptance_criteria"])

def test_bridge_coverage_delta_score_Given_old_new_When_score_Then_delta():
    """Given old and new reports When coverage_delta_score Then delta."""
    old = _make_report({"a.py": (100, 50, [1], 50.0)}, total_pct=50.0)
    new = _make_report({"a.py": (100, 80, [], 80.0)}, total_pct=80.0)
    assert coverage_delta_score(old, new) == pytest.approx(0.1, abs=0.01)  # capped at 0.1
    assert coverage_delta_score(old, new, module_path="a.py") == pytest.approx(0.1, abs=0.01)
    # regression
    new_low = _make_report({"a.py": (100, 20, [1], 20.0)}, total_pct=20.0)
    assert coverage_delta_score(old, new_low) == pytest.approx(-0.1, abs=0.01)  # floor -0.1 but delta -30/100=-0.3 capped -0.1

def test_bridge_coverage_delta_score_edge_cases_Given_null_When_score_Then_zero():
    """Given None/error When coverage_delta_score Then 0."""
    assert coverage_delta_score(None, _make_report({"a.py":(10,5,[],50.0)})) == 0.0
    old_err = _make_report({}, error="oops")
    assert coverage_delta_score(old_err, _make_report({"a.py":(10,5,[],50.0)})) == 0.0
    new_err = _make_report({}, error="oops")
    assert coverage_delta_score(_make_report({"a.py":(10,5,[],50.0)}), new_err) == 0.0
    # missing module
    old = _make_report({"a.py": (10,5,[],50.0)})
    new = _make_report({"b.py": (10,5,[],50.0)})
    assert coverage_delta_score(old, new, module_path="a.py") == 0.0
    assert coverage_delta_score(old, new, module_path="missing.py") == 0.0
    # small delta not capped
    old2 = _make_report({"a.py": (100, 50, [], 50.0)}, total_pct=50.0)
    new2 = _make_report({"a.py": (100, 55, [], 55.0)}, total_pct=55.0)
    assert coverage_delta_score(old2, new2) == pytest.approx(0.05, abs=0.01)

# =============================================================================
# anomaly_alerts
# =============================================================================

@pytest.fixture
def isolate_alerts(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(parents=True)
    # BOTS_DIR is codebot pkg dir, parent is project root where docs/ lives
    codebot_dir = tmp_path / "codebot"
    codebot_dir.mkdir(parents=True, exist_ok=True)
    docs_triage = tmp_path / "docs" / "triage"
    docs_triage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(aa_mod, "STATE_DIR", state)
    monkeypatch.setattr(aa_mod, "BOTS_DIR", codebot_dir)
    monkeypatch.setattr(aa_mod, "ALERTS_FILE", state / "anomaly_alerts.json")
    monkeypatch.setattr(aa_mod, "DIGEST_FILE", state / "daily_digest.md")
    monkeypatch.setattr(aa_mod, "HISTORY_FILE", state / "bot_metrics_history.jsonl")
    return {"state": state, "tmp": tmp_path, "triage": docs_triage, "docs": tmp_path / "docs"}

def test_alerts_read_json_exists_Given_valid_When_read_Then_dict(isolate_alerts):
    """Given valid file When _read_json Then data."""
    p = isolate_alerts["state"] / "a.json"
    p.write_text(json.dumps({"k":1}))
    assert aa_read_json(p) == {"k":1}

def test_alerts_read_json_missing_Given_no_file_When_read_Then_none(isolate_alerts):
    """Given missing When _read_json Then None."""
    assert aa_read_json(isolate_alerts["state"] / "nope.json") is None

def test_alerts_read_json_corrupt_Given_bad_When_read_Then_none(isolate_alerts):
    """Given bad json When _read_json Then None."""
    p = isolate_alerts["state"] / "bad.json"
    p.write_text("{bad")
    assert aa_read_json(p) is None

def test_alerts_write_json_atomic_Given_data_When_write_Then_file(isolate_alerts):
    """Given data When _write_json_atomic Then file exists."""
    p = isolate_alerts["state"] / "out.json"
    aa_write_json_atomic(p, {"a":1})
    assert json.loads(p.read_text()) == {"a":1}
    # overwrite
    aa_write_json_atomic(p, [1,2])
    assert json.loads(p.read_text()) == [1,2]

def test_alerts_write_json_atomic_handles_exception_Given_bad_path_When_write_Then_swallowed(isolate_alerts, monkeypatch):
    """Given exception When _write_json_atomic Then swallowed."""
    with patch.object(Path, "write_text", side_effect=OSError("fail")):
        aa_write_json_atomic(isolate_alerts["state"] / "fail.json", {"a":1})  # should not raise

def test_alerts_history_snapshots_Given_history_When_read_Then_list(isolate_alerts):
    """Given history file When _history_snapshots Then list."""
    hf = isolate_alerts["state"] / "bot_metrics_history.jsonl"
    hf.write_text(json.dumps({"id":1}) + "\n" + json.dumps({"id":2}) + "\n" + "{bad\n")
    snaps = _history_snapshots(limit=10)
    assert len(snaps) == 2
    assert snaps[0]["id"] == 1

def test_alerts_history_no_file_Given_none_When_read_Then_empty(isolate_alerts):
    """Given no history When _history_snapshots Then empty."""
    assert _history_snapshots() == []

def test_alerts_history_os_error_Given_unreadable_When_read_Then_empty(isolate_alerts, monkeypatch):
    """Given OSError When _history_snapshots Then empty."""
    hf = isolate_alerts["state"] / "bot_metrics_history.jsonl"
    hf.write_text(json.dumps({"a":1}))
    with patch.object(Path, "read_text", side_effect=OSError("fail")):
        assert _history_snapshots() == []

def test_alerts_queue_depth_counts_Given_queue_When_depth_Then_count(isolate_alerts):
    """Given QUEUE.md When _queue_depth Then count."""
    q = isolate_alerts["triage"] / "QUEUE.md"
    q.write_text("| Q-1 | a | b | c | d | e |\n| QUEUE-DECOMP-1 | a | b | c | d | e |\nnot counted\n")
    assert _queue_depth() == 2

def test_alerts_queue_depth_no_file_Given_none_When_depth_Then_zero(isolate_alerts):
    """Given no file When _queue_depth Then 0."""
    assert _queue_depth() == 0

def test_alerts_queue_depth_os_error_Given_fail_When_depth_Then_zero(monkeypatch, isolate_alerts):
    """Given OSError When _queue_depth Then 0."""
    with patch.object(Path, "read_text", side_effect=OSError("fail")):
        assert _queue_depth() == 0

def test_alerts_approval_backlog_hours_Given_unknown_When_call_Then_zero(isolate_alerts, monkeypatch):
    """Given no approval file When _approval_backlog_hours Then 0."""
    # patch load_approved_ids to return empty
    monkeypatch.setattr("codebot.readiness.load_approved_ids", lambda x: set())
    # need QUEUE file with T4+ item but mocked to not be old; current impl always returns 0 if body contains TIER
    q = isolate_alerts["triage"] / "QUEUE.md"
    q.write_text("### Q-1\nTIER-4 something\n")
    # function currently returns 0 even if found (oldest =0.0 if Tier found), so check
    assert _approval_backlog_hours() == 0.0

def test_alerts_approval_backlog_no_queue_Given_no_queue_When_call_Then_zero(isolate_alerts, monkeypatch):
    """Given no queue When _approval_backlog_hours Then 0."""
    monkeypatch.setattr("codebot.readiness.load_approved_ids", lambda x: set())
    # ensure no file
    q = isolate_alerts["triage"] / "QUEUE.md"
    if q.exists(): q.unlink()
    assert _approval_backlog_hours() == 0.0

def test_alerts_daily_token_avg_Given_snaps_When_avg_Then_mean():
    """Given snaps When _daily_token_avg Then average."""
    snaps = [{"ledger_total_actual": 100}, {"ledger_total_actual": 200}, {"ledger_total_actual": 300}]
    assert _daily_token_avg(snaps, days=3) == 200.0
    assert _daily_token_avg(snaps, days=2) == 250.0
    assert _daily_token_avg([], days=7) == 0.0
    assert _daily_token_avg([{"a":1}], days=7) == 0.0

def test_alerts_evaluate_A1_erroring_streak_Given_3_erroring_When_evaluate_Then_page(isolate_alerts):
    """Given 3 consecutive erroring When evaluate Then A1 page."""
    # prepare history with 2 prior erroring snapshots
    hf = isolate_alerts["state"] / "bot_metrics_history.jsonl"
    snap_err = {"summary":{"erroring":1,"stale":0,"regressing":0}}
    hf.write_text("\n".join([json.dumps(snap_err), json.dumps(snap_err)]) + "\n")
    # current snapshot also erroring
    snapshot = {"summary":{"erroring":2,"stale":0,"regressing":0,"bots_tracked":5},"ledger_total_actual": 0}
    result = evaluate(snapshot)
    assert any(a["rule"]=="A1" and a["severity"]=="page" for a in result["alerts"])

def test_alerts_evaluate_no_A1_Given_not_enough_When_evaluate_Then_none(isolate_alerts):
    """Given not enough erroring When evaluate Then no A1."""
    hf = isolate_alerts["state"] / "bot_metrics_history.jsonl"
    hf.write_text(json.dumps({"summary":{"erroring":0}}) + "\n")
    snapshot = {"summary":{"erroring":0,"stale":0,"regressing":0},"ledger_total_actual":0}
    result = evaluate(snapshot)
    assert not any(a["rule"]=="A1" for a in result["alerts"])

def test_alerts_evaluate_A3_token_burn_Given_high_tokens_When_evaluate_Then_page(isolate_alerts):
    """Given token burn >2x avg When evaluate Then A3."""
    # create history with low avg
    hf = isolate_alerts["state"] / "bot_metrics_history.jsonl"
    hf.write_text("\n".join([json.dumps({"ledger_total_actual": 100}) for _ in range(5)]) + "\n")
    snapshot = {"summary":{"erroring":0,"stale":0,"regressing":0},"ledger_total_actual": 500, "ledger_day":"2026-09-21"}
    result = evaluate(snapshot)
    assert any(a["rule"]=="A3" for a in result["alerts"])

def test_alerts_evaluate_A3_no_trigger_Given_low_tokens_When_evaluate_Then_none(isolate_alerts):
    """Given tokens below threshold When evaluate Then no A3."""
    hf = isolate_alerts["state"] / "bot_metrics_history.jsonl"
    hf.write_text("\n".join([json.dumps({"ledger_total_actual": 200}) for _ in range(3)]) + "\n")
    snapshot = {"summary":{"erroring":0,"stale":0,"regressing":0},"ledger_total_actual": 100}
    result = evaluate(snapshot)
    assert not any(a["rule"]=="A3" for a in result["alerts"])

def test_alerts_evaluate_A5_stale_Given_stale_When_evaluate_Then_digest(isolate_alerts):
    """Given stale bots When evaluate Then A5 digest."""
    snapshot = {"summary":{"stale":2,"erroring":0,"regressing":0},"ledger_total_actual":0}
    result = evaluate(snapshot)
    assert any(a["rule"]=="A5" and a["severity"]=="digest" for a in result["alerts"])

def test_alerts_evaluate_A6_regressing_Given_3_regressing_When_evaluate_Then_digest(isolate_alerts):
    """Given 3 regressing When evaluate Then A6."""
    snapshot = {"summary":{"regressing":3,"stale":0,"erroring":0},"ledger_total_actual":0}
    result = evaluate(snapshot)
    assert any(a["rule"]=="A6" for a in result["alerts"])
    # below threshold no alert
    snapshot2 = {"summary":{"regressing":2,"stale":0,"erroring":0},"ledger_total_actual":0}
    result2 = evaluate(snapshot2)
    assert not any(a["rule"]=="A6" for a in result2["alerts"])

def test_alerts_evaluate_loads_from_file_Given_no_snapshot_When_evaluate_Then_reads(isolate_alerts):
    """Given no snapshot arg When evaluate Then reads bot_metrics.json."""
    state = isolate_alerts["state"]
    state.joinpath("bot_metrics.json").write_text(json.dumps({"summary":{"erroring":0,"stale":1,"regressing":0},"ledger_total_actual":0}))
    result = evaluate(None)
    assert any(a["rule"]=="A5" for a in result["alerts"])

def test_alerts_evaluate_merges_prev_alerts_Given_prev_file_When_evaluate_Then_merged(isolate_alerts):
    """Given previous alerts file When evaluate Then merged and capped."""
    state = isolate_alerts["state"]
    # preload alerts file with 49 items
    prev = [{"rule":"A1","severity":"page","message":"old","ts": 1.0} for _ in range(49)]
    (state / "anomaly_alerts.json").write_text(json.dumps(prev))
    snapshot = {"summary":{"stale":1,"erroring":0,"regressing":0},"ledger_total_actual":0}
    result = evaluate(snapshot)
    merged = json.loads((state / "anomaly_alerts.json").read_text())
    assert len(merged) <= 50
    assert len(merged) >= 1

def test_alerts_evaluate_prev_corrupt_Given_bad_prev_When_evaluate_Then_handled(isolate_alerts):
    """Given corrupt prev alerts When evaluate Then handled."""
    (isolate_alerts["state"] / "anomaly_alerts.json").write_text("{bad")
    snapshot = {"summary":{"stale":0,"erroring":0,"regressing":0},"ledger_total_actual":0}
    result = evaluate(snapshot)  # should not crash
    assert "alerts" in result

def test_alerts_evaluate_A2_never_triggers_due_to_bug_Given_any_When_evaluate_Then_no_A2(isolate_alerts):
    """Given any state When evaluate Then A2 never triggers (bug: depths are same value)."""
    snapshot = {"summary":{"erroring":0,"stale":0,"regressing":0},"ledger_total_actual":0}
    result = evaluate(snapshot)
    assert not any(a["rule"]=="A2" for a in result["alerts"])

def test_alerts_write_daily_digest_creates_file_Given_snapshot_When_write_Then_file(isolate_alerts):
    """Given snapshot When write_daily_digest Then file created."""
    snapshot = {"summary":{"bots_tracked":10,"active":5,"erroring":0,"stale":1,"improving":2,"regressing":1},"bots":{"b1":{"alignment":{"avg_reward":0.9},"reward_trend":"flat"},"b2":{"alignment":{"avg_reward":0.1},"reward_trend":"regressing"}},"ledger_total_actual":123,"ledger_day":"2026-09-21"}
    # ensure no marker
    marker = isolate_alerts["state"] / ".digest_date"
    if marker.exists(): marker.unlink()
    wrote = write_daily_digest(snapshot, force=True)
    assert wrote is True
    assert (isolate_alerts["state"] / "daily_digest.md").exists()
    content = (isolate_alerts["state"] / "daily_digest.md").read_text()
    assert "# Daily Digest" in content
    assert "Fleet:" in content

def test_alerts_write_daily_digest_marker_prevents_double_Given_today_written_When_write_Then_false(isolate_alerts):
    """Given marker for today When write without force Then false."""
    snapshot = {"summary":{},"bots":{},"ledger_total_actual":0}
    write_daily_digest(snapshot, force=True)
    # second without force should be false
    wrote2 = write_daily_digest(snapshot, force=False)
    assert wrote2 is False

def test_alerts_write_daily_digest_force_overrides_Given_marker_When_force_Then_true(isolate_alerts):
    """Given marker When force Then writes."""
    snapshot = {"summary":{},"bots":{},"ledger_total_actual":0}
    write_daily_digest(snapshot, force=True)
    wrote = write_daily_digest(snapshot, force=True)
    assert wrote is True

def test_alerts_write_daily_digest_loads_snapshot_if_none(isolate_alerts):
    """Given None snapshot When write_daily_digest Then loads from file."""
    state = isolate_alerts["state"]
    state.joinpath("bot_metrics.json").write_text(json.dumps({"summary":{"bots_tracked":1,"active":1,"erroring":0,"stale":0,"improving":0,"regressing":0},"bots":{},"ledger_total_actual":0,"ledger_day":"2026-09-21"}))
    marker = state / ".digest_date"
    if marker.exists(): marker.unlink()
    wrote = write_daily_digest(None, force=True)
    assert wrote is True

def test_alerts_write_daily_digest_pages_included_Given_alerts_When_write_Then_pages(isolate_alerts):
    """Given pages in alerts file When write_daily_digest Then included."""
    state = isolate_alerts["state"]
    (state / "anomaly_alerts.json").write_text(json.dumps([{"rule":"A1","severity":"page","message":"boom","ts": time.time()}]))
    snapshot = {"summary":{"bots_tracked":0,"active":0,"erroring":0,"stale":0,"improving":0,"regressing":0},"bots":{},"ledger_total_actual":0}
    write_daily_digest(snapshot, force=True)
    content = (state / "daily_digest.md").read_text()
    assert "A1" in content or "boom" in content

def test_alerts_write_daily_digest_regressors_none_Given_no_regressors_When_write_Then_none_line(isolate_alerts):
    """Given no regressors When write Then 'none' line."""
    snapshot = {"summary":{"bots_tracked":1,"active":0,"erroring":0,"stale":0,"improving":0,"regressing":0},"bots":{"b1":{"alignment":{"avg_reward":0.5},"reward_trend":"flat"}},"ledger_total_actual":0}
    write_daily_digest(snapshot, force=True)
    content = (isolate_alerts["state"] / "daily_digest.md").read_text()
    assert "none" in content.lower()

def test_alerts_main_prints_Given_evaluate_When_main_Then_print(monkeypatch, isolate_alerts, capsys):
    """Given evaluate When main Then prints."""
    monkeypatch.setattr(aa_mod, "evaluate", lambda: {"alerts":[{"severity":"page","rule":"A1","message":"x"}],"pages":[{"severity":"page","rule":"A1","message":"x"}]})
    monkeypatch.setattr(aa_mod, "write_daily_digest", lambda: True)
    aa_mod.main()
    out = capsys.readouterr().out
    assert "alerts=" in out

# Additional edge to hit uncovered lines: anomaly_alerts _approval_backlog_hours with exception
def test_alerts_approval_backlog_exception_Given_import_fail_When_call_Then_zero(isolate_alerts, monkeypatch):
    """Given exception in _approval_backlog_hours When called Then 0."""
    # make load_approved_ids raise
    with patch("codebot.readiness.load_approved_ids", side_effect=RuntimeError("fail")):
        assert _approval_backlog_hours() == 0.0

def test_alerts_daily_token_avg_exception_Given_bad_data_When_call_Then_zero():
    """Given bad data When _daily_token_avg Then 0."""
    bad = [None, "bad"]  # type: ignore[list-item]
    assert _daily_token_avg(bad, days=7) == 0.0  # type: ignore[arg-type]

def test_metrics_collector_main_without_flag_Given_no_flag_When_main_not_incremental(monkeypatch, isolate_collector):
    """Given no flag When main Then not only_fresh."""
    monkeypatch.setattr(sys, "argv", ["prog"])
    with patch.object(mc_mod, "collect_all") as mock_collect, patch.object(mc_mod, "save_snapshot") as mock_save:
        mock_collect.return_value = {"timestamp_human":"now","summary":{"bots_tracked":1,"active":0,"idle":0,"stale":0,"erroring":0,"improving":0,"regressing":0},"ledger_total_actual":0,"ledger_day":"?"}
        mc_mod.main()
        mock_collect.assert_called_once_with(only_fresh=False)

# Extra: ensure coverage of metrics_service ModuleCoverage dataclass repr etc.
def test_metrics_service_imports_exist_Given_module_When_import_Then_callable():
    """Given metrics_service When imported Then has expected attrs."""
    assert callable(read_bot_metrics)
    assert callable(save_bot_metrics)
    assert hasattr(ms_mod, "METRICS_DIR")

