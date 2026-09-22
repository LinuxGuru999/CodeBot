"""Tests for gatekeeper.py."""
import json
import pytest
from pathlib import Path
import sys
from unittest.mock import patch, MagicMock
sys.path.insert(0, str(Path(__file__).parent.parent))
from codebot.coverage_runner import CoverageReport, ModuleCoverage
from codebot.gatekeeper import Gatekeeper, Decision, MAX_REWORK_ATTEMPTS, _push_enabled_for_prs

def _policy(tmp_path, cmd):
    p = tmp_path / "gates.yaml"
    p.write_text(f"required:\n  - name: t\n    command: {cmd}\n")
    return p


def _coverage_report(path: str, percentage: float) -> CoverageReport:
    covered = int(percentage)
    return CoverageReport(
        total_statements=100,
        total_covered=covered,
        total_coverage_pct=percentage,
        modules={path: ModuleCoverage(path, 100, covered, [], percentage)},
        timestamp=0.0,
        pytest_exit_code=0,
    )


class TestGatekeeperVerify:
    def test_gate_pass_yields_complete(self, tmp_path):
        """Gate passes → COMPLETE (reviewer verdicts are trusted)."""
        ws = tmp_path / "ws"; ws.mkdir()
        state = tmp_path / "state"
        gk = Gatekeeper(state, policy_path=_policy(tmp_path, "echo ok"), workspace=ws)
        from codebot.ticket_engine import TicketState
        mock_store = MagicMock()
        mock_ticket = MagicMock()
        mock_ticket.state = TicketState.REVIEW
        mock_ticket.updated_at = 0.0
        mock_store.get.return_value = mock_ticket
        mock_store.transition.return_value = True
        r = gk.verify_ticket("CB-1", "bug", ["f.py"], store=mock_store)
        assert r["decision"] == "COMPLETE"
        assert r["passed"] is True
        # Verify transition to COMPLETE was attempted
        assert mock_store.transition.called

    def test_gate_fail_yields_rework(self, tmp_path):
        """Gate fails → REWORK."""
        ws = tmp_path / "ws"; ws.mkdir()
        gk = Gatekeeper(tmp_path / "state", policy_path=_policy(tmp_path, "false"), workspace=ws)
        r = gk.verify_ticket("CB-1", "bug", ["f.py"], rework_count=0)
        assert r["decision"] == "REWORK"

    def test_incomplete_affected_module_coverage_yields_rework(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".codebot").mkdir()
        (ws / ".codebot" / "review.yaml").write_text(
            "gatekeeper:\n  require_full_coverage: true\n",
            encoding="utf-8",
        )
        report = _coverage_report("codebot/feature.py", 99.0)
        gk = Gatekeeper(tmp_path / "state", policy_path=_policy(tmp_path, "echo ok"), workspace=ws)

        with patch("codebot.coverage_runner.run_coverage", return_value=report):
            result = gk.verify_ticket("CB-1", "bug", ["codebot/feature.py"])

        assert result["decision"] == "REWORK"
        assert "coverage" in result["failed_gates"]

    def test_full_affected_module_coverage_yields_complete(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".codebot").mkdir()
        (ws / ".codebot" / "review.yaml").write_text(
            "gatekeeper:\n  require_full_coverage: true\n",
            encoding="utf-8",
        )
        report = _coverage_report(str(ws / "codebot" / "feature.py"), 100.0)
        gk = Gatekeeper(tmp_path / "state", policy_path=_policy(tmp_path, "echo ok"), workspace=ws)
        from codebot.ticket_engine import TicketState
        mock_store = MagicMock()
        mock_ticket = MagicMock()
        mock_ticket.state = TicketState.REVIEW
        mock_ticket.updated_at = 0.0
        mock_store.get.return_value = mock_ticket
        mock_store.transition.return_value = True

        with patch("codebot.coverage_runner.run_coverage", return_value=report):
            result = gk.verify_ticket("CB-1", "bug", ["codebot/feature.py"], store=mock_store)

        assert result["decision"] == "COMPLETE"
        assert "coverage" not in result["failed_gates"]

    def test_fail_yields_rework(self, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir()
        gk = Gatekeeper(tmp_path / "state", policy_path=_policy(tmp_path, "false"), workspace=ws)
        r = gk.verify_ticket("CB-1", "bug", ["f.py"], rework_count=0)
        assert r["decision"] == "REWORK"

    def test_max_rework_yields_rework(self, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir()
        gk = Gatekeeper(tmp_path / "state", policy_path=_policy(tmp_path, "false"), workspace=ws)
        r = gk.verify_ticket("CB-1", "bug", ["f.py"], rework_count=MAX_REWORK_ATTEMPTS)
        assert r["decision"] == "REWORK"

    def test_decision_logged(self, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir()
        gk = Gatekeeper(tmp_path / "state", policy_path=_policy(tmp_path, "echo ok"), workspace=ws)
        from codebot.ticket_engine import TicketState
        mock_store = MagicMock()
        mock_ticket = MagicMock()
        mock_ticket.state = TicketState.REVIEW
        mock_ticket.updated_at = 0.0
        mock_store.get.return_value = mock_ticket
        mock_store.transition.return_value = True
        gk.verify_ticket("CB-1", "bug", ["f.py"], store=mock_store)
        log = tmp_path / "state" / "gate_results.jsonl"
        # gate_results.jsonl now contains multiple JSON lines (one per gate + decision log)
        # Parse the last line which should be the Gatekeeper decision
        lines = [line for line in log.read_text().strip().splitlines() if line.strip()]
        decision_entries = [json.loads(line) for line in lines if "decision" in json.loads(line)]
        assert any(e["ticket_id"] == "CB-1" for e in decision_entries)

    def test_history(self, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir()
        gk = Gatekeeper(tmp_path / "state", policy_path=_policy(tmp_path, "echo ok"), workspace=ws)
        from codebot.ticket_engine import TicketState
        mock_store = MagicMock()
        mock_ticket = MagicMock()
        mock_ticket.state = TicketState.REVIEW
        mock_ticket.updated_at = 0.0
        mock_store.get.return_value = mock_ticket
        mock_store.transition.return_value = True
        gk.verify_ticket("CB-1", "bug", ["f.py"], store=mock_store)
        gk.verify_ticket("CB-2", "bug", ["f.py"], store=mock_store)
        # get_history returns all entries for ticket_id from gate_results.jsonl
        # Filter to decision entries (those with 'decision' key)
        history = gk.get_history("CB-1")
        decision_history = [e for e in history if "decision" in e]
        assert len(decision_history) == 1

    def test_max_rework_is_3(self):
        assert MAX_REWORK_ATTEMPTS == 3

    def test_decision_stub_values(self):
        assert Decision.PASS.value == "PASS"
        assert Decision.FAIL.value == "FAIL"
        assert Decision.WARN.value == "WARN"
        assert {m.value for m in Decision} == {"PASS", "FAIL", "WARN"}
        assert hasattr(Gatekeeper, "verify_ticket")

    def test_verify_ticket_exception_fail_closed_logs_decision(self, tmp_path):
        """Exception in verify_ticket returns REWORK and transitions ticket to REWORK state."""
        ws = tmp_path / "ws"
        ws.mkdir()
        state = tmp_path / "state"
        gk = Gatekeeper(state, policy_path=_policy(tmp_path, "echo ok"), workspace=ws)

        from codebot.ticket_engine import TicketState
        mock_store = MagicMock()
        mock_ticket = MagicMock()
        mock_ticket.state = TicketState.REVIEW
        mock_ticket.updated_at = 0.0
        mock_store.get.return_value = mock_ticket

        with patch("codebot.quality_gate.run_quality_gates_with_cache", side_effect=RuntimeError("simulated failure")):
            result = gk.verify_ticket("CB-EXCEPT", "bug", ["f.py"], store=mock_store)

        # Must return REWORK to ensure correct state transition
        assert result["decision"] == "REWORK"
        assert result["passed"] is False
        assert "verification_error" in result["reason"]
        assert result["failed_gates"] == []
        assert result["total_gates"] == 0
        # Must transition ticket to REWORK to prevent orphaned tickets
        mock_store.transition.assert_called_once_with("CB-EXCEPT", TicketState.REWORK, [])
        # Verify the REWORK decision was logged to gate_results.jsonl
        log_path = state / "gate_results.jsonl"
        assert log_path.exists(), "gate_results.jsonl must exist after exception"
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) >= 1, "At least one log entry must exist"
        last_entry = json.loads(lines[-1])
        assert last_entry["ticket_id"] == "CB-EXCEPT"
        assert last_entry["decision"] == "REWORK"
        assert last_entry["passed"] is False
        assert "verification_error" in last_entry["reason"]
