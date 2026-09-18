"""Tests for coverage_runner.py and coverage_bridge.py."""
import json
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from codebot.coverage_runner import (
    CoverageReport,
    ModuleCoverage,
    _parse_missing_lines,
    _empty_report,
    save_coverage_report,
    load_coverage_report,
)
from codebot.coverage_bridge import (
    generate_coverage_tickets,
    _chunk_missing_lines,
    _severity_for_coverage,
    _risk_for_coverage,
    _format_line_ranges,
    coverage_delta_score,
)


class TestModuleCoverage:
    def test_to_dict(self):
        mc = ModuleCoverage(path="src/x.py", statements=100, covered=80, missing=[1, 2, 3], coverage_pct=80.0)
        d = mc.to_dict()
        assert d["path"] == "src/x.py"
        assert d["coverage_pct"] == 80.0
        assert d["missing"] == [1, 2, 3]


class TestCoverageReport:
    def _make_report(self) -> CoverageReport:
        return CoverageReport(
            total_statements=200,
            total_covered=140,
            total_coverage_pct=70.0,
            modules={
                "src/a.py": ModuleCoverage("src/a.py", 100, 90, [1, 2], 90.0),
                "src/b.py": ModuleCoverage("src/b.py", 100, 50, list(range(50)), 50.0),
            },
            timestamp=1000.0,
            pytest_exit_code=0,
        )

    def test_below_threshold(self):
        r = self._make_report()
        below = r.below_threshold(60.0)
        assert len(below) == 1
        assert below[0].path == "src/b.py"

    def test_below_threshold_all_pass(self):
        r = self._make_report()
        below = r.below_threshold(40.0)
        assert len(below) == 0

    def test_uncovered_lines(self):
        r = self._make_report()
        assert r.uncovered_lines("src/a.py") == [1, 2]
        assert r.uncovered_lines("nonexistent.py") == []

    def test_serialization_roundtrip(self):
        r = self._make_report()
        raw = r.to_json()
        r2 = CoverageReport.from_dict(json.loads(raw))
        assert r2.total_coverage_pct == 70.0
        assert len(r2.modules) == 2
        assert r2.modules["src/a.py"].coverage_pct == 90.0

    def test_save_and_load(self, tmp_path):
        r = self._make_report()
        save_coverage_report(r, tmp_path)
        loaded = load_coverage_report(tmp_path)
        assert loaded is not None
        assert loaded.total_coverage_pct == 70.0

    def test_load_missing_returns_none(self, tmp_path):
        assert load_coverage_report(tmp_path / "nonexistent") is None


class TestParseMissingLines:
    def test_integer_list(self):
        assert _parse_missing_lines([1, 2, 3, 5]) == [1, 2, 3, 5]

    def test_string_ranges(self):
        assert _parse_missing_lines(["1-3", "5", "10-12"]) == [1, 2, 3, 5, 10, 11, 12]

    def test_empty(self):
        assert _parse_missing_lines([]) == []

    def test_non_list(self):
        assert _parse_missing_lines(None) == []


class TestEmptyReport:
    def test_has_error(self):
        r = _empty_report(error="test error")
        assert r.error == "test error"
        assert r.total_coverage_pct == 0.0
        assert r.modules == {}


class TestChunkMissingLines:
    def test_empty(self):
        assert _chunk_missing_lines([], 50) == [[]]

    def test_single_chunk(self):
        lines = list(range(1, 11))
        chunks = _chunk_missing_lines(lines, 50)
        assert len(chunks) == 1
        assert chunks[0] == lines

    def test_multiple_chunks(self):
        lines = list(range(1, 101))
        chunks = _chunk_missing_lines(lines, 30)
        assert len(chunks) == 4
        assert len(chunks[0]) == 30
        assert len(chunks[-1]) == 10


class TestSeverityForCoverage:
    def test_critical(self):
        assert _severity_for_coverage(10.0) == "critical"
        assert _severity_for_coverage(29.9) == "critical"

    def test_high(self):
        assert _severity_for_coverage(30.0) == "high"
        assert _severity_for_coverage(49.9) == "high"

    def test_medium(self):
        assert _severity_for_coverage(50.0) == "medium"
        assert _severity_for_coverage(69.9) == "medium"

    def test_low(self):
        assert _severity_for_coverage(70.0) == "low"
        assert _severity_for_coverage(100.0) == "low"


class TestRiskForCoverage:
    def test_critical(self):
        assert _risk_for_coverage(20.0) == "critical"

    def test_high(self):
        assert _risk_for_coverage(40.0) == "high"

    def test_medium(self):
        assert _risk_for_coverage(60.0) == "medium"


class TestFormatLineRanges:
    def test_consecutive(self):
        assert _format_line_ranges([1, 2, 3, 4]) == "1-4"

    def test_gaps(self):
        assert _format_line_ranges([1, 2, 5, 6, 10]) == "1-2, 5-6, 10"

    def test_single(self):
        assert _format_line_ranges([42]) == "42"

    def test_empty(self):
        assert _format_line_ranges([]) == "all"


class TestGenerateCoverageTickets:
    def test_empty_report_skips(self):
        report = _empty_report(error="no data")
        from codebot.ticket_engine import TicketStore
        store = TicketStore(Path("/dev/null"))
        ids = generate_coverage_tickets(report, store)
        assert ids == []

    def test_above_threshold_skips(self):
        report = CoverageReport(
            total_statements=100, total_covered=90, total_coverage_pct=90.0,
            modules={"src/a.py": ModuleCoverage("src/a.py", 100, 90, [1], 90.0)},
            timestamp=1000.0, pytest_exit_code=0,
        )
        from codebot.ticket_engine import TicketStore
        store = TicketStore(Path("/dev/null"))
        ids = generate_coverage_tickets(report, store, min_coverage_pct=60.0)
        assert ids == []

    def test_protected_paths_skipped(self):
        report = CoverageReport(
            total_statements=100, total_covered=20, total_coverage_pct=20.0,
            modules={"secret/auth.py": ModuleCoverage("secret/auth.py", 100, 20, list(range(80)), 20.0)},
            timestamp=1000.0, pytest_exit_code=0,
        )
        from codebot.ticket_engine import TicketStore
        store = TicketStore(Path("/dev/null"))
        ids = generate_coverage_tickets(report, store, protected_paths={"secret/"})
        assert ids == []


class TestCoverageDeltaScore:
    def test_no_old_report(self):
        assert coverage_delta_score(None, _empty_report()) == 0.0

    def test_improvement(self):
        old = CoverageReport(0, 0, 60.0, {}, 1000.0, 0)
        new = CoverageReport(0, 0, 70.0, {}, 1001.0, 0)
        score = coverage_delta_score(old, new)
        assert score > 0
        assert score <= 0.1

    def test_regression(self):
        old = CoverageReport(0, 0, 70.0, {}, 1000.0, 0)
        new = CoverageReport(0, 0, 60.0, {}, 1001.0, 0)
        score = coverage_delta_score(old, new)
        assert score < 0
        assert score >= -0.1

    def test_bounded(self):
        old = CoverageReport(0, 0, 0.0, {}, 1000.0, 0)
        new = CoverageReport(0, 0, 100.0, {}, 1001.0, 0)
        score = coverage_delta_score(old, new)
        assert score == 0.1

    def test_module_specific(self):
        old = CoverageReport(0, 0, 50.0, {"a.py": ModuleCoverage("a.py", 100, 50, [], 50.0)}, 1000.0, 0)
        new = CoverageReport(0, 0, 50.0, {"a.py": ModuleCoverage("a.py", 100, 70, [], 70.0)}, 1001.0, 0)
        score = coverage_delta_score(old, new, module_path="a.py")
        assert score > 0

    def test_error_report_zero(self):
        old = CoverageReport(0, 0, 50.0, {}, 1000.0, 0)
        new = _empty_report(error="fail")
        assert coverage_delta_score(old, new) == 0.0
