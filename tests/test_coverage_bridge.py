"""Tests for codebot/coverage_bridge.py — measured coverage to ticket conversion."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path for direct imports
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import codebot.coverage_bridge as cb


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _make_module(path="codebot/foo.py", statements=100, covered=60, missing=None, coverage_pct=None):
    if coverage_pct is None:
        coverage_pct = (covered / statements * 100) if statements > 0 else 0.0
    return SimpleNamespace(
        path=path,
        statements=statements,
        covered=covered,
        missing=missing or list(range(covered + 1, statements + 1)),
        coverage_pct=coverage_pct,
    )


def _make_report(modules=None, error=False, total_coverage_pct=60.0):
    mods = modules or []
    mod_dict = {m.path: m for m in mods}
    report = SimpleNamespace(
        modules=mod_dict,
        error=error,
        total_coverage_pct=total_coverage_pct,
    )
    # below_threshold returns modules under the given pct
    def below_threshold(pct):
        return [m for m in mods if m.coverage_pct < pct]
    report.below_threshold = below_threshold
    return report


# ---------------------------------------------------------------------------
# Threshold-to-severity mapping tests
# ---------------------------------------------------------------------------

class TestSeverityForCoverage:
    def test_below_30_is_critical(self):
        assert cb._severity_for_coverage(10.0) == "critical"
        assert cb._severity_for_coverage(29.9) == "critical"

    def test_30_to_50_is_high(self):
        assert cb._severity_for_coverage(30.0) == "high"
        assert cb._severity_for_coverage(49.9) == "high"

    def test_50_to_70_is_medium(self):
        assert cb._severity_for_coverage(50.0) == "medium"
        assert cb._severity_for_coverage(69.9) == "medium"

    def test_70_and_above_is_low(self):
        assert cb._severity_for_coverage(70.0) == "low"
        assert cb._severity_for_coverage(100.0) == "low"


class TestRiskForCoverage:
    def test_below_30_is_critical(self):
        assert cb._risk_for_coverage(10.0) == "critical"

    def test_30_to_50_is_high(self):
        assert cb._risk_for_coverage(30.0) == "high"
        assert cb._risk_for_coverage(49.9) == "high"

    def test_50_and_above_is_medium(self):
        assert cb._risk_for_coverage(50.0) == "medium"
        assert cb._risk_for_coverage(100.0) == "medium"


# ---------------------------------------------------------------------------
# Constants verification
# ---------------------------------------------------------------------------

class TestConstants:
    def test_default_min_coverage(self):
        assert cb.DEFAULT_MIN_COVERAGE_PCT == 60.0

    def test_critical_threshold(self):
        assert cb.CRITICAL_THRESHOLD_PCT == 30.0

    def test_high_threshold(self):
        assert cb.HIGH_THRESHOLD_PCT == 50.0

    def test_medium_threshold(self):
        assert cb.MEDIUM_THRESHOLD_PCT == 70.0

    def test_max_lines_per_ticket(self):
        assert cb.MAX_LINES_PER_TICKET == 50


# ---------------------------------------------------------------------------
# Line range grouping tests (_chunk_missing_lines)
# ---------------------------------------------------------------------------

class TestChunkMissingLines:
    def test_empty_missing_returns_single_empty_chunk(self):
        chunks = cb._chunk_missing_lines([], 50)
        assert chunks == [[]]

    def test_under_max_returns_single_chunk(self):
        lines = list(range(1, 11))
        chunks = cb._chunk_missing_lines(lines, 50)
        assert len(chunks) == 1
        assert chunks[0] == lines

    def test_exactly_max_returns_single_chunk(self):
        lines = list(range(1, 51))
        chunks = cb._chunk_missing_lines(lines, 50)
        assert len(chunks) == 1
        assert len(chunks[0]) == 50

    def test_over_max_splits_into_multiple_chunks(self):
        lines = list(range(1, 101))
        chunks = cb._chunk_missing_lines(lines, 50)
        assert len(chunks) == 2
        assert len(chunks[0]) == 50
        assert len(chunks[1]) == 50

    def test_non_even_split(self):
        lines = list(range(1, 76))
        chunks = cb._chunk_missing_lines(lines, 50)
        assert len(chunks) == 2
        assert len(chunks[0]) == 50
        assert len(chunks[1]) == 25

    def test_sorts_input_lines(self):
        lines = [10, 5, 1, 3]
        chunks = cb._chunk_missing_lines(lines, 50)
        assert chunks[0] == [1, 3, 5, 10]


# ---------------------------------------------------------------------------
# _format_line_ranges tests
# ---------------------------------------------------------------------------

class TestFormatLineRanges:
    def test_empty_list_returns_all(self):
        assert cb._format_line_ranges([]) == "all"

    def test_single_line(self):
        assert cb._format_line_ranges([5]) == "5"

    def test_consecutive_lines_form_range(self):
        assert cb._format_line_ranges([1, 2, 3]) == "1-3"

    def test_non_consecutive_lines_separated(self):
        assert cb._format_line_ranges([1, 5, 10]) == "1, 5, 10"

    def test_mixed_ranges_and_singles(self):
        result = cb._format_line_ranges([1, 2, 3, 7, 8, 15])
        assert result == "1-3, 7-8, 15"


# ---------------------------------------------------------------------------
# Protected path exclusion tests
# ---------------------------------------------------------------------------

class TestIsProtected:
    def test_protected_prefix_matches(self):
        assert cb._is_protected("codebot/orchestrator.py", {"codebot/orchestrator"}) is True

    def test_unprotected_path_returns_false(self):
        assert cb._is_protected("codebot/foo.py", {"codebot/bar"}) is False

    def test_empty_protected_set(self):
        assert cb._is_protected("codebot/foo.py", set()) is False

    def test_exact_prefix_match(self):
        assert cb._is_protected("codebot/core/engine.py", {"codebot/core/"}) is True


# ---------------------------------------------------------------------------
# generate_coverage_tickets tests
# ---------------------------------------------------------------------------

class TestGenerateCoverageTickets:
    @patch("codebot.coverage_bridge._create_test_ticket")
    def test_error_report_returns_empty(self, mock_create):
        report = _make_report(error=True)
        store = MagicMock()
        result = cb.generate_coverage_tickets(report, store)
        assert result == []
        mock_create.assert_not_called()

    @patch("codebot.coverage_bridge._create_test_ticket")
    def test_empty_modules_returns_empty(self, mock_create):
        report = _make_report(modules=[])
        store = MagicMock()
        result = cb.generate_coverage_tickets(report, store)
        assert result == []
        mock_create.assert_not_called()

    @patch("codebot.coverage_bridge._create_test_ticket")
    def test_skips_protected_paths(self, mock_create):
        mod = _make_module(path="protected/mod.py", coverage_pct=10.0)
        report = _make_report(modules=[mod])
        store = MagicMock()
        result = cb.generate_coverage_tickets(report, store, protected_paths={"protected/"})
        assert result == []
        mock_create.assert_not_called()

    @patch("codebot.coverage_bridge._create_test_ticket")
    def test_skips_zero_statement_modules(self, mock_create):
        mod = _make_module(path="empty.py", statements=0, covered=0, missing=[], coverage_pct=0.0)
        report = _make_report(modules=[mod])
        store = MagicMock()
        result = cb.generate_coverage_tickets(report, store)
        assert result == []
        mock_create.assert_not_called()

    @patch("codebot.coverage_bridge._create_test_ticket")
    def test_respects_max_tickets_cap(self, mock_create):
        mock_create.return_value = "TICKET-ID"
        mods = [_make_module(path=f"mod{i}.py", coverage_pct=10.0, statements=100, missing=list(range(1, 101))) for i in range(30)]
        report = _make_report(modules=mods)
        store = MagicMock()
        result = cb.generate_coverage_tickets(report, store, max_tickets=5)
        assert len(result) == 5

    @patch("codebot.coverage_bridge._create_test_ticket")
    def test_creates_tickets_for_below_threshold_modules(self, mock_create):
        mock_create.return_value = "T-1"
        mod = _make_module(path="low.py", coverage_pct=20.0, statements=100, missing=list(range(1, 81)))
        report = _make_report(modules=[mod])
        store = MagicMock()
        result = cb.generate_coverage_tickets(report, store)
        assert len(result) >= 1
        assert mock_create.called

    @patch("codebot.coverage_bridge._create_test_ticket")
    def test_handles_none_return_from_create_ticket(self, mock_create):
        mock_create.return_value = None
        mod = _make_module(path="fail.py", coverage_pct=10.0, statements=10, missing=list(range(1, 10)))
        report = _make_report(modules=[mod])
        store = MagicMock()
        result = cb.generate_coverage_tickets(report, store)
        assert result == []


# ---------------------------------------------------------------------------
# coverage_delta_score tests
# ---------------------------------------------------------------------------

class TestCoverageDeltaScore:
    def test_no_old_report_returns_zero(self):
        new_report = _make_report(total_coverage_pct=70.0)
        assert cb.coverage_delta_score(None, new_report) == 0.0

    def test_old_report_with_error_returns_zero(self):
        old_report = _make_report(error=True)
        new_report = _make_report(total_coverage_pct=70.0)
        assert cb.coverage_delta_score(old_report, new_report) == 0.0

    def test_new_report_with_error_returns_zero(self):
        old_report = _make_report(total_coverage_pct=50.0)
        new_report = _make_report(error=True)
        assert cb.coverage_delta_score(old_report, new_report) == 0.0

    def test_positive_delta_capped_at_0_1(self):
        old_report = _make_report(total_coverage_pct=0.0)
        new_report = _make_report(total_coverage_pct=100.0)
        score = cb.coverage_delta_score(old_report, new_report)
        assert score == 0.1  # capped

    def test_negative_delta_capped_at_neg_0_1(self):
        old_report = _make_report(total_coverage_pct=100.0)
        new_report = _make_report(total_coverage_pct=0.0)
        score = cb.coverage_delta_score(old_report, new_report)
        assert score == -0.1  # capped

    def test_module_specific_delta(self):
        mod_old = _make_module(path="a.py", coverage_pct=50.0)
        mod_new = _make_module(path="a.py", coverage_pct=60.0)
        old_report = _make_report(modules=[mod_old])
        new_report = _make_report(modules=[mod_new])
        score = cb.coverage_delta_score(old_report, new_report, module_path="a.py")
        assert score == pytest.approx(0.1, abs=0.01)  # 10% / 100 = 0.1

    def test_missing_module_returns_zero(self):
        old_report = _make_report(modules=[])
        new_report = _make_report(modules=[])
        score = cb.coverage_delta_score(old_report, new_report, module_path="ghost.py")
        assert score == 0.0
