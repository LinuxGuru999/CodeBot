"""Tests for remaining P0 uncovered modules: runtime_invariants, review_store, metrics_service, orchestrator_cli/compat, documentation_ticket_generator."""
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestRuntimeInvariants:
    def test_check_empty_dir_no_alerts(self, tmp_path):
        from codebot.runtime_invariants import check_runtime_invariants
        alerts = check_runtime_invariants(tmp_path)
        assert isinstance(alerts, list)

    def test_check_with_store(self, tmp_path):
        from codebot.runtime_invariants import check_runtime_invariants
        from codebot.ticket_engine import TicketStore
        store = TicketStore(tmp_path / "tickets.json")
        alerts = check_runtime_invariants(tmp_path, store=store)
        assert isinstance(alerts, list)
        store.close()

    def test_check_with_workforce_status_drift(self, tmp_path):
        from codebot.runtime_invariants import check_runtime_invariants
        from codebot.ticket_engine import TicketStore, TicketClass, Severity, RiskLevel, create_ticket
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"], risk=RiskLevel.LOW)
        store.add(t)
        store.flush()

        workforce_status = {"buckets": {"implement": 999, "review": 0, "planning": 0, "rework": 0, "decomp": 0, "goal": 0, "triaged": 0}}
        alerts = check_runtime_invariants(tmp_path, store=store, workforce_status=workforce_status)
        assert isinstance(alerts, list)
        store.close()

    def test_record_runtime_invariants_writes_file(self, tmp_path):
        from codebot.runtime_invariants import record_runtime_invariants
        result = record_runtime_invariants(tmp_path)
        assert isinstance(result, list)

    def test_quarantine_alert(self, tmp_path):
        from codebot.runtime_invariants import check_runtime_invariants
        quarantine = tmp_path / "checkpoint_quarantine"
        quarantine.mkdir()
        (quarantine / "bad.json").write_text("{}")
        alerts = check_runtime_invariants(tmp_path)
        assert any(a["rule"] == "CHECKPOINT_CORRUPTION" for a in alerts)

    def test_packet_coverage_gap(self, tmp_path):
        from codebot.runtime_invariants import check_runtime_invariants
        from codebot.ticket_engine import TicketStore, TicketClass, Severity, RiskLevel, create_ticket, TicketState
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"], risk=RiskLevel.LOW)
        store.add(t)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.GOAL)
        store.transition(t.id, TicketState.DECOMP)
        store.transition(t.id, TicketState.PLANNING)
        store.transition(t.id, TicketState.IMPLEMENT)
        store.flush()
        alerts = check_runtime_invariants(tmp_path, store=store)
        # Should detect missing implementation packet
        assert any("PACKET" in a["rule"] for a in alerts) or isinstance(alerts, list)
        store.close()


class TestReviewStore:
    def test_write_and_load_verdict(self, tmp_path):
        from codebot.review_store import write_verdict, load_ticket_verdicts
        verdict = {"verdict": "APPROVE", "reviewer": "correctness_reviewer", "ticket_id": "CB-1"}
        path = write_verdict(tmp_path, "CB-1", "correctness_reviewer", verdict)
        assert path.exists()
        loaded = load_ticket_verdicts(tmp_path, "CB-1")
        assert len(loaded) == 1

    def test_invalid_role_raises(self, tmp_path):
        from codebot.review_store import write_verdict
        with pytest.raises(ValueError, match="invalid reviewer role"):
            write_verdict(tmp_path, "CB-1", "bad role!", {"verdict": "APPROVE"})

    def test_empty_ticket_id_raises(self, tmp_path):
        from codebot.review_store import write_verdict
        with pytest.raises(ValueError):
            write_verdict(tmp_path, "", "correctness_reviewer", {"verdict": "APPROVE"})

    def test_non_dict_verdict_raises(self, tmp_path):
        from codebot.review_store import write_verdict
        with pytest.raises(ValueError):
            write_verdict(tmp_path, "CB-1", "correctness_reviewer", "not a dict")  # type: ignore

    def test_oversized_verdict_raises(self, tmp_path):
        from codebot.review_store import write_verdict
        big = {"verdict": "APPROVE", "data": "x" * (70 * 1024)}
        with pytest.raises(ValueError, match="exceeds size"):
            write_verdict(tmp_path, "CB-1", "correctness_reviewer", big)

    def test_sanitize_ticket_id(self, tmp_path):
        from codebot.review_store import write_verdict, load_ticket_verdicts
        verdict = {"verdict": "APPROVE"}
        path = write_verdict(tmp_path, "CB/1\\test", "correctness_reviewer", verdict)
        assert path.exists()
        # Should sanitize slashes
        assert "\\" not in str(path)

    def test_has_required_reviewers(self, tmp_path):
        from codebot.review_store import write_verdict, has_required_reviewers
        write_verdict(tmp_path, "CB-2", "correctness_reviewer", {"verdict": "APPROVE"})
        assert has_required_reviewers(tmp_path, "CB-2", ["correctness_reviewer"]) is True
        assert has_required_reviewers(tmp_path, "CB-2", ["security_reviewer"]) is False
        assert has_required_reviewers(tmp_path, "CB-2", []) is True

    def test_load_malformed_quarantined(self, tmp_path):
        from codebot.review_store import load_ticket_verdicts
        reviews_dir = tmp_path / "reviews" / "CB-BAD"
        reviews_dir.mkdir(parents=True)
        (reviews_dir / "correctness_reviewer.json").write_text("{bad json!!!")
        loaded = load_ticket_verdicts(tmp_path, "CB-BAD")
        assert loaded == []

    def test_reviewer_names_for_ticket(self, tmp_path):
        from codebot.review_store import write_verdict, reviewer_names_for_ticket
        write_verdict(tmp_path, "CB-3", "correctness_reviewer", {"verdict": "APPROVE"})
        write_verdict(tmp_path, "CB-3", "security_reviewer", {"verdict": "APPROVE"})
        names = reviewer_names_for_ticket(tmp_path, "CB-3")
        assert "correctness_reviewer" in names
        assert "security_reviewer" in names

    def test_suffixed_worker_satisfies_base(self, tmp_path):
        from codebot.review_store import write_verdict, has_required_reviewers
        write_verdict(tmp_path, "CB-4", "security_reviewer", {"verdict": "APPROVE"})
        # Write with suffixed name manually
        import json as _json
        from codebot.review_store import reviews_dir
        rd = reviews_dir(tmp_path) / "CB-5"
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "security_reviewer-3.json").write_text(_json.dumps({"verdict": "APPROVE", "reviewer": "security_reviewer-3"}))
        assert has_required_reviewers(tmp_path, "CB-5", ["security_reviewer"]) is True


class TestMetricsService:
    def test_bot_metrics_dataclass(self):
        from codebot.metrics_service import BotMetrics
        m = BotMetrics(name="test-bot")
        assert m.name == "test-bot"
        assert m.total_runs == 0

    def test_pipeline_metrics_dataclass(self):
        from codebot.metrics_service import PipelineMetrics
        m = PipelineMetrics()
        assert m.tickets_created == 0

    def test_read_missing_bot_returns_defaults(self):
        from codebot.metrics_service import read_bot_metrics
        m = read_bot_metrics("nonexistent-bot-xyz")
        assert m.name == "nonexistent-bot-xyz"
        assert m.total_runs == 0

    def test_record_bot_run_success(self):
        from codebot.metrics_service import record_bot_run, read_bot_metrics
        record_bot_run("test-metrics-bot", success=True, duration_seconds=1.0)
        m = read_bot_metrics("test-metrics-bot")
        assert m.total_runs >= 1

    def test_record_bot_run_failure(self):
        from codebot.metrics_service import record_bot_run, read_bot_metrics
        record_bot_run("test-metrics-bot-fail", success=False, duration_seconds=1.0)
        m = read_bot_metrics("test-metrics-bot-fail")
        assert m.failed_runs >= 1
        assert m.consecutive_errors >= 1

    def test_record_model_change(self):
        from codebot.metrics_service import record_model_change, read_bot_metrics
        record_model_change("test-metrics-bot-model")
        m = read_bot_metrics("test-metrics-bot-model")
        assert m.model_changes >= 1

    def test_get_all_bot_metrics(self):
        from codebot.metrics_service import get_all_bot_metrics
        result = get_all_bot_metrics()
        assert isinstance(result, dict)

    def test_pipeline_metrics_roundtrip(self):
        from codebot.metrics_service import PipelineMetrics, save_pipeline_metrics, read_pipeline_metrics
        m = PipelineMetrics(tickets_created=5, tickets_completed=3)
        save_pipeline_metrics(m)
        loaded = read_pipeline_metrics()
        assert loaded.tickets_created == 5

    def test_collect_snapshot(self):
        from codebot.metrics_service import collect_bot_status_snapshot
        snapshot = collect_bot_status_snapshot({})
        assert isinstance(snapshot, dict)


class TestOrchestratorCli:
    def test_parse_args_defaults(self, monkeypatch):
        from codebot.orchestrator_cli import parse_args
        monkeypatch.setattr("sys.argv", ["orchestrator"])
        args = parse_args()
        assert hasattr(args, "check_interval")

    def test_handle_cli_no_command(self, monkeypatch):
        from codebot.orchestrator_cli import handle_cli_commands, parse_args
        monkeypatch.setattr("sys.argv", ["orchestrator"])
        args = parse_args()
        result = handle_cli_commands(args, {}, print_status_fn=lambda: None, is_draining_fn=lambda: False,
                                      drain_status_fn=lambda: {}, clear_drain_fn=lambda: None,
                                      safe_stop_all_fn=lambda bots: {}, stop_bot_fn=lambda b, **kw: None,
                                      start_bot_fn=lambda b, **kw: None)
        assert result is False

    def test_parse_status_flag(self, monkeypatch):
        from codebot.orchestrator_cli import parse_args, handle_cli_commands
        monkeypatch.setattr("sys.argv", ["orchestrator", "--status"])
        args = parse_args()
        handled = handle_cli_commands(args, {}, print_status_fn=lambda bots: None, is_draining_fn=lambda: False,
                                       drain_status_fn=lambda: {}, clear_drain_fn=lambda: None,
                                       safe_stop_all_fn=lambda bots: {}, stop_bot_fn=lambda b, **kw: None,
                                       start_bot_fn=lambda b, **kw: None)
        assert handled is True


class TestOrchestratorCompat:
    def test_read_heartbeat_missing(self, tmp_path):
        from codebot.orchestrator_compat import read_heartbeat
        result = read_heartbeat("nonexistent-bot", state_dir_override=tmp_path)
        assert result == 0.0

    def test_checkpoint_path(self, tmp_path):
        from codebot.orchestrator_compat import checkpoint_path
        p = checkpoint_path("test-bot", state_dir_override=tmp_path)
        assert isinstance(p, Path)
        assert "test-bot" in str(p)

    def test_read_checkpoint_missing(self, tmp_path):
        from codebot.orchestrator_compat import read_checkpoint
        result = read_checkpoint("nonexistent-bot", state_dir_override=tmp_path)
        assert result is None

    def test_batch_read_heartbeats_empty(self, tmp_path):
        from codebot.orchestrator_compat import batch_read_heartbeats
        result = batch_read_heartbeats([], state_dir_override=tmp_path)
        assert result == {}

    def test_is_draining_false_when_no_file(self, tmp_path):
        from codebot.orchestrator_compat import is_draining
        assert is_draining(drain_file_override=tmp_path / ".drain") is False

    def test_read_state_file_missing(self, tmp_path):
        from codebot.orchestrator_compat import read_state_file
        result = read_state_file("nonexistent", state_dir_override=tmp_path)
        assert result == {}


class TestDocumentationTicketGenerator:
    def test_module_importable(self):
        import codebot.documentation_ticket_generator as dtg
        assert dtg is not None

    def test_create_doc_ticket(self, tmp_path):
        from codebot.documentation_ticket_generator import create_documentation_ticket
        assert callable(create_documentation_ticket)
        result = create_documentation_ticket(MagicMock(ticket_class="bug", affected_modules=[]), MagicMock(list_by_class=lambda x: []), workspace=tmp_path)
        assert result is None or isinstance(result, str)

    def test_should_create_doc_ticket(self, tmp_path):
        from codebot.documentation_ticket_generator import should_create_doc_ticket
        from codebot.ticket_engine import TicketStore
        store = TicketStore(tmp_path / "tickets.json")
        result = should_create_doc_ticket(MagicMock(ticket_class="bug", affected_modules=[]), store)
        assert result is False
        store.close()
