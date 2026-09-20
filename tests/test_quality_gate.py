"""Tests for quality_gate.py."""
import json
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from codebot.quality_gate import QualityGatePolicy, GateResult, load_policy, evaluate_gate, run_quality_gates, record_gate_results

class TestQualityGatePolicy:
    def test_default_has_required(self):
        p = QualityGatePolicy.default()
        names = [g["name"] for g in p.required]
        assert "build" in names
        assert "unit_tests" in names

    def test_from_dict(self):
        p = QualityGatePolicy.from_dict({"required": [{"name": "x", "command": "echo ok"}], "conditional": {}})
        assert len(p.required) == 1

class TestLoadPolicy:
    def test_missing_returns_default(self, tmp_path):
        p = load_policy(tmp_path / "nope.yaml")
        assert len(p.required) >= 1

class TestEvaluateGate:
    def test_passing(self, tmp_path):
        ev = evaluate_gate({"name": "ok", "command": "echo pass"}, tmp_path)
        assert ev.result == GateResult.PASS

    def test_failing(self, tmp_path):
        ev = evaluate_gate({"name": "bad", "command": "false"}, tmp_path)
        assert ev.result == GateResult.FAIL

    def test_timeout(self, tmp_path):
        ev = evaluate_gate({"name": "slow", "command": "sleep 10"}, tmp_path, timeout=1)
        assert ev.result == GateResult.ERROR

    def test_quoted_file_context_is_one_argument(self, tmp_path):
        ev = evaluate_gate(
            {"name": "quoted", "command": "echo '{file}'"},
            tmp_path,
            file_context="tests/path with spaces.py",
        )
        assert ev.passed is True
        assert ev.output.strip() == "tests/path with spaces.py"

class TestRunQualityGates:
    def test_all_pass(self, tmp_path):
        policy = QualityGatePolicy(required=[{"name": "ok", "command": "echo pass"}], conditional={})
        passed, evals = run_quality_gates(policy, tmp_path)
        assert passed is True

    def test_fail_blocks(self, tmp_path):
        policy = QualityGatePolicy(required=[{"name": "bad", "command": "false"}], conditional={})
        passed, _ = run_quality_gates(policy, tmp_path)
        assert passed is False

    def test_conditional_by_class(self, tmp_path):
        policy = QualityGatePolicy(required=[{"name": "ok", "command": "echo pass"}], conditional={"security_boundary": [{"name": "sec", "command": "echo sec"}]})
        _, evals = run_quality_gates(policy, tmp_path, ticket_class="security")
        assert any(e.gate_name == "sec" for e in evals)

class TestRecordGateResults:
    def test_appends_jsonl(self, tmp_path):
        from codebot.quality_gate import GateEvaluation
        evals = [GateEvaluation("t", GateResult.PASS, "echo", "ok", 0.1, True)]
        record_gate_results(tmp_path, "CB-1", True, evals)
        lines = (tmp_path / "gate_results.jsonl").read_text().strip().split("\n")
        assert json.loads(lines[0])["ticket_id"] == "CB-1"


class TestGateMetrics:
    """Tests for gate metrics collection and alerting."""

    def _write_jsonl_record(self, tmp_path, ticket_id, passed, gates, ts):
        """Helper to write a raw JSONL record."""
        import os
        record = {"ticket_id": ticket_id, "passed": passed, "timestamp": ts, "gates": gates}
        line = json.dumps(record) + "\n"
        fd = os.open(str(tmp_path / "gate_results.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)

    def test_metrics_recorded(self, tmp_path):
        """Gate execution metrics are recorded with duration and error counts."""
        from codebot.quality_gate import get_gate_metrics
        # Record 2 results: one pass, one fail
        self._write_jsonl_record(tmp_path, "CB-1", True, [
            {"gate_name": "build", "result": "pass", "duration_ms": 100.0, "command": "echo", "output": "", "passed": True, "required": True, "error_message": ""},
        ], 1000.0)
        self._write_jsonl_record(tmp_path, "CB-2", False, [
            {"gate_name": "build", "result": "fail", "duration_ms": 200.0, "command": "echo", "output": "err", "passed": False, "required": True, "error_message": "exit code 1"},
        ], 2000.0)

        snapshot = get_gate_metrics(tmp_path)
        assert snapshot["version"] == 1
        assert len(snapshot["metrics"]) == 1
        m = snapshot["metrics"][0]
        assert m["gate_name"] == "build"
        assert m["total"] == 2
        assert m["passes"] == 1
        assert m["fails"] == 1
        assert m["errors"] == 0
        assert m["failure_rate"] == 0.5
        assert m["avg_ms"] == 150.0

    def test_failure_rate_alert(self, tmp_path):
        """Configurable failure rate threshold triggers alert."""
        from codebot.quality_gate import check_gate_alerts, GateAlertConfig
        # 3 records for build: 2 failures, 1 pass -> failure_rate ~0.67
        records = []
        for i in range(3):
            result = "fail" if i < 2 else "pass"
            records.append({
                "gate_name": "build",
                "result": result,
                "duration_ms": 100.0,
                "command": "echo",
                "output": "",
                "passed": result == "pass",
                "required": True,
                "error_message": "",
            })
        metrics = []
        # Manually compute to test check_gate_alerts
        from codebot.quality_gate import compute_gate_metrics
        raw_records = [
            {"ticket_id": f"CB-{i}", "passed": r["passed"], "timestamp": 1000.0 + i, "gates": [r]}
            for i, r in enumerate(records)
        ]
        metrics = compute_gate_metrics(raw_records)
        config = GateAlertConfig(max_failure_rate=0.2, min_samples=3)
        alerts = check_gate_alerts(metrics, config)
        assert len(alerts) == 1
        assert alerts[0]["rule"] == "failure_rate"
        assert alerts[0]["gate"] == "build"
        assert alerts[0]["severity"] == "warning"

    def test_thresholds_configurable(self, tmp_path):
        """GateAlertConfig.from_dict and from_env override defaults."""
        from codebot.quality_gate import GateAlertConfig
        c = GateAlertConfig.from_dict({
            "max_failure_rate": 0.3,
            "max_error_rate": 0.1,
            "max_avg_duration_ms": 5000.0,
            "min_samples": 10,
            "streak_window": 3,
        })
        assert c.max_failure_rate == 0.3
        assert c.max_error_rate == 0.1
        assert c.max_avg_duration_ms == 5000.0
        assert c.min_samples == 10
        assert c.streak_window == 3

        d = c.to_dict()
        assert d["max_failure_rate"] == 0.3
        assert d["min_samples"] == 10

    def test_exposure_shape(self, tmp_path):
        """Metrics snapshot has the expected version, metrics, alerts, generated_at keys."""
        from codebot.quality_gate import get_gate_metrics
        snapshot = get_gate_metrics(tmp_path)
        assert "version" in snapshot
        assert "metrics" in snapshot
        assert "alerts" in snapshot
        assert "generated_at" in snapshot
        assert snapshot["version"] == 1
        assert isinstance(snapshot["metrics"], list)
        assert isinstance(snapshot["alerts"], list)
        assert isinstance(snapshot["generated_at"], float)

    def test_corrupt_line_fail_open(self, tmp_path):
        """Corrupt JSONL lines are skipped without breaking metrics."""
        import os
        from codebot.quality_gate import get_gate_metrics
        # Write a valid record
        self._write_jsonl_record(tmp_path, "CB-1", True, [
            {"gate_name": "build", "result": "pass", "duration_ms": 50.0, "command": "echo", "output": "", "passed": True, "required": True, "error_message": ""},
        ], 1000.0)
        # Append a corrupt line
        fd = os.open(str(tmp_path / "gate_results.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, b"{not valid json!!!\n")
        finally:
            os.close(fd)

        snapshot = get_gate_metrics(tmp_path)
        assert snapshot["version"] == 1
        assert len(snapshot["metrics"]) == 1
        assert snapshot["metrics"][0]["total"] == 1

    def test_record_gate_results_refreshes_cache(self, tmp_path):
        """record_gate_results should also create gate_metrics.json cache."""
        from codebot.quality_gate import GateEvaluation
        evals = [GateEvaluation("build", GateResult.PASS, "echo pass", "ok", 42.5, True)]
        record_gate_results(tmp_path, "CB-99", True, evals)
        cache = tmp_path / "gate_metrics.json"
        assert cache.exists()
        data = json.loads(cache.read_text())
        assert data["version"] == 1
        assert len(data["metrics"]) == 1
        assert data["metrics"][0]["gate_name"] == "build"

    def test_min_samples_guard(self, tmp_path):
        """Alerts are not fired when total < min_samples."""
        from codebot.quality_gate import check_gate_alerts, GateAlertConfig, compute_gate_metrics
        raw_records = [
            {"ticket_id": "CB-1", "passed": False, "timestamp": 1000.0, "gates": [
                {"gate_name": "build", "result": "fail", "duration_ms": 100.0, "command": "echo", "output": "", "passed": False, "required": True, "error_message": ""},
            ]},
            {"ticket_id": "CB-2", "passed": False, "timestamp": 2000.0, "gates": [
                {"gate_name": "build", "result": "fail", "duration_ms": 100.0, "command": "echo", "output": "", "passed": False, "required": True, "error_message": ""},
            ]},
        ]
        metrics = compute_gate_metrics(raw_records)
        config = GateAlertConfig(max_failure_rate=0.1, min_samples=5)
        alerts = check_gate_alerts(metrics, config)
        assert len(alerts) == 0  # only 2 samples, below min_samples=5

    def test_no_executions_returns_empty(self, tmp_path):
        """Missing/empty gate_results.jsonl returns empty metrics."""
        from codebot.quality_gate import get_gate_metrics
        snapshot = get_gate_metrics(tmp_path)
        assert snapshot["metrics"] == []
        assert snapshot["alerts"] == []
