"""Unit tests for codebot.coverage_runner module.

Covers dataclass serialization, parsing helpers, threshold queries,
and fail-open behavior of run_coverage.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from codebot.coverage_runner import (
    CoverageReport,
    ModuleCoverage,
    _empty_report,
    _parse_missing_lines,
    _parse_coverage_json,
    run_coverage,
    save_coverage_report,
    load_coverage_report,
)


# ---------------------------------------------------------------------------
# ModuleCoverage tests
# ---------------------------------------------------------------------------

class TestModuleCoverage:
    def test_to_dict(self) -> None:
        mc = ModuleCoverage(
            path="codebot/foo.py",
            statements=100,
            covered=80,
            missing=[21, 22, 23],
            coverage_pct=80.0,
        )
        d = mc.to_dict()
        assert d["path"] == "codebot/foo.py"
        assert d["statements"] == 100
        assert d["covered"] == 80
        assert d["missing"] == [21, 22, 23]
        assert d["coverage_pct"] == 80.0

    def test_frozen(self) -> None:
        mc = ModuleCoverage(path="x", statements=1, covered=1, missing=[], coverage_pct=100.0)
        with pytest.raises(AttributeError):
            mc.path = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CoverageReport tests
# ---------------------------------------------------------------------------

class TestCoverageReport:
    @pytest.fixture()
    def sample_report(self) -> CoverageReport:
        return CoverageReport(
            total_statements=200,
            total_covered=150,
            total_coverage_pct=75.0,
            modules={
                "a.py": ModuleCoverage("a.py", 100, 90, [11, 12], 90.0),
                "b.py": ModuleCoverage("b.py", 100, 60, list(range(41, 81)), 60.0),
            },
            timestamp=1700000000.0,
            pytest_exit_code=0,
        )

    def test_to_dict_and_json(self, sample_report: CoverageReport) -> None:
        d = sample_report.to_dict()
        assert d["total_statements"] == 200
        assert "a.py" in d["modules"]
        j = sample_report.to_json()
        parsed = json.loads(j)
        assert parsed["total_coverage_pct"] == 75.0

    def test_from_dict_roundtrip(self, sample_report: CoverageReport) -> None:
        d = sample_report.to_dict()
        restored = CoverageReport.from_dict(d)
        assert restored.total_statements == sample_report.total_statements
        assert restored.modules["b.py"].coverage_pct == 60.0
        assert restored.error is None

    def test_to_dict_with_error(self, sample_report: CoverageReport) -> None:
        """to_dict includes error field when set."""
        report_with_error = CoverageReport(
            total_statements=0,
            total_covered=0,
            total_coverage_pct=0.0,
            modules={},
            timestamp=1700000000.0,
            pytest_exit_code=-1,
            error="something went wrong",
        )
        d = report_with_error.to_dict()
        assert d["error"] == "something went wrong"
        j = report_with_error.to_json()
        parsed = json.loads(j)
        assert parsed["error"] == "something went wrong"

    def test_from_dict_with_error(self) -> None:
        """from_dict restores error field."""
        data = {
            "total_statements": 0,
            "total_covered": 0,
            "total_coverage_pct": 0.0,
            "modules": {},
            "timestamp": 1700000000.0,
            "pytest_exit_code": -1,
            "error": "pytest-cov not installed",
        }
        restored = CoverageReport.from_dict(data)
        assert restored.error == "pytest-cov not installed"

    def test_below_threshold(self, sample_report: CoverageReport) -> None:
        below = sample_report.below_threshold(80.0)
        assert len(below) == 1
        assert below[0].path == "b.py"

    def test_below_threshold_none(self, sample_report: CoverageReport) -> None:
        below = sample_report.below_threshold(50.0)
        assert below == []

    def test_uncovered_lines(self, sample_report: CoverageReport) -> None:
        lines = sample_report.uncovered_lines("a.py")
        assert lines == [11, 12]

    def test_uncovered_lines_missing_module(self, sample_report: CoverageReport) -> None:
        assert sample_report.uncovered_lines("nonexistent.py") == []


# ---------------------------------------------------------------------------
# _parse_missing_lines tests
# ---------------------------------------------------------------------------

class TestParseMissingLines:
    def test_integer_list(self) -> None:
        assert _parse_missing_lines([1, 5, 3]) == [1, 3, 5]

    def test_string_ranges(self) -> None:
        assert _parse_missing_lines(["1-3", "10"]) == [1, 2, 3, 10]

    def test_mixed(self) -> None:
        result = _parse_missing_lines([1, "3-5", 7])
        assert result == [1, 3, 4, 5, 7]

    def test_empty_list(self) -> None:
        assert _parse_missing_lines([]) == []

    def test_non_list_returns_empty(self) -> None:
        assert _parse_missing_lines("not a list") == []
        assert _parse_missing_lines(None) == []

    def test_deduplication(self) -> None:
        assert _parse_missing_lines([1, 1, "1-2"]) == [1, 2]

    def test_invalid_range_skipped(self) -> None:
        # ValueError on int() conversion should be caught
        assert _parse_missing_lines(["abc-def"]) == []


# ---------------------------------------------------------------------------
# _empty_report tests
# ---------------------------------------------------------------------------

class TestEmptyReport:
    def test_defaults(self) -> None:
        r = _empty_report()
        assert r.total_statements == 0
        assert r.error is None
        assert r.pytest_exit_code == -1

    def test_with_error(self) -> None:
        r = _empty_report(error="something broke", exit_code=2)
        assert r.error == "something broke"
        assert r.pytest_exit_code == 2


# ---------------------------------------------------------------------------
# _parse_coverage_json tests
# ---------------------------------------------------------------------------

class TestParseCoverageJson:
    def test_valid_json(self, tmp_path: Path) -> None:
        cov_data = {
            "totals": {
                "num_statements": 50,
                "covered_lines": 40,
                "percent_covered": 80.0,
            },
            "files": {
                "mod.py": {
                    "summary": {
                        "num_statements": 50,
                        "covered_lines": 40,
                        "percent_covered": 80.0,
                    },
                    "missing_lines": [10, "20-22"],
                }
            },
        }
        cov_file = tmp_path / "coverage.json"
        cov_file.write_text(json.dumps(cov_data), encoding="utf-8")

        report = _parse_coverage_json(cov_file, exit_code=0)
        assert report.total_statements == 50
        assert report.total_coverage_pct == 80.0
        assert "mod.py" in report.modules
        assert report.modules["mod.py"].missing == [10, 20, 21, 22]
        assert report.pytest_exit_code == 0

    def test_percent_covered_as_dict(self, tmp_path: Path) -> None:
        cov_data = {
            "totals": {"num_statements": 10, "covered_lines": 5, "percent_covered": {"display": 50.0}},
            "files": {
                "x.py": {
                    "summary": {"num_statements": 10, "covered_lines": 5, "percent_covered": {"display": 50.0}},
                    "missing_lines": [],
                }
            },
        }
        cov_file = tmp_path / "coverage.json"
        cov_file.write_text(json.dumps(cov_data), encoding="utf-8")
        report = _parse_coverage_json(cov_file, exit_code=0)
        assert report.total_coverage_pct == 50.0
        assert report.modules["x.py"].coverage_pct == 50.0

    def test_invalid_json(self, tmp_path: Path) -> None:
        cov_file = tmp_path / "coverage.json"
        cov_file.write_text("{bad json", encoding="utf-8")
        report = _parse_coverage_json(cov_file, exit_code=1)
        assert report.error is not None
        assert "failed to parse" in report.error

    def test_missing_file(self, tmp_path: Path) -> None:
        report = _parse_coverage_json(tmp_path / "nope.json", exit_code=1)
        assert report.error is not None


# ---------------------------------------------------------------------------
# run_coverage tests (mocked subprocess)
# ---------------------------------------------------------------------------

class TestRunCoverage:
    def test_python_not_found(self, tmp_path: Path) -> None:
        with patch("codebot.coverage_runner.subprocess.run", side_effect=FileNotFoundError):
            report = run_coverage(tmp_path)
        assert report.error == "python3 not found"
        assert report.total_statements == 0

    def test_timeout(self, tmp_path: Path) -> None:
        import subprocess as sp
        with patch("codebot.coverage_runner.subprocess.run", side_effect=sp.TimeoutExpired(cmd="pytest", timeout=5)):
            report = run_coverage(tmp_path, timeout=5)
        assert report.error is not None
        assert "timed out" in report.error

    def test_generic_exception(self, tmp_path: Path) -> None:
        with patch("codebot.coverage_runner.subprocess.run", side_effect=OSError("disk full")):
            report = run_coverage(tmp_path)
        assert report.error is not None
        assert "disk full" in report.error

    def test_no_cov_json_pytest_cov_missing(self, tmp_path: Path) -> None:
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "No module named pytest_cov"
        with patch("codebot.coverage_runner.subprocess.run", return_value=mock_proc):
            report = run_coverage(tmp_path)
        assert report.error == "pytest-cov not installed"

    def test_no_cov_json_generic_error(self, tmp_path: Path) -> None:
        """When cov_json not produced and stderr doesn't mention pytest-cov."""
        mock_proc = MagicMock()
        mock_proc.returncode = 2
        mock_proc.stderr = "ERROR: some other failure"
        with patch("codebot.coverage_runner.subprocess.run", return_value=mock_proc):
            report = run_coverage(tmp_path)
        assert report.error is not None
        assert "coverage.json not produced" in report.error
        assert report.pytest_exit_code == 2

    def test_no_cov_json_stderr_none(self, tmp_path: Path) -> None:
        """When cov_json not produced and stderr is None."""
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = None
        with patch("codebot.coverage_runner.subprocess.run", return_value=mock_proc):
            report = run_coverage(tmp_path)
        assert report.error is not None
        assert "coverage.json not produced" in report.error
        assert report.pytest_exit_code == 1

    def test_successful_run(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        cov_data = {
            "totals": {"num_statements": 10, "covered_lines": 10, "percent_covered": 100.0},
            "files": {},
        }
        (state_dir / "coverage.json").write_text(json.dumps(cov_data), encoding="utf-8")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = ""
        with patch("codebot.coverage_runner.subprocess.run", return_value=mock_proc):
            report = run_coverage(tmp_path)
        assert report.total_statements == 10
        assert report.pytest_exit_code == 0

    def test_source_dirs_auto_detect(self, tmp_path: Path) -> None:
        (tmp_path / "codebot").mkdir()
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "coverage.json").write_text('{"totals":{},"files":{}}', encoding="utf-8")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = ""
        with patch("codebot.coverage_runner.subprocess.run", return_value=mock_proc) as mock_run:
            run_coverage(tmp_path)
        cmd = mock_run.call_args[0][0]
        assert "--cov=codebot" in cmd

    def test_extra_args_passed(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "coverage.json").write_text('{"totals":{},"files":{}}', encoding="utf-8")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = ""
        with patch("codebot.coverage_runner.subprocess.run", return_value=mock_proc) as mock_run:
            run_coverage(tmp_path, extra_args=["-k", "test_foo"])
        cmd = mock_run.call_args[0][0]
        assert "-k" in cmd
        assert "test_foo" in cmd

    def test_explicit_test_dirs(self, tmp_path: Path) -> None:
        """Explicit test_dirs are passed instead of auto-detect."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "coverage.json").write_text('{"totals":{},"files":{}}', encoding="utf-8")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = ""
        with patch("codebot.coverage_runner.subprocess.run", return_value=mock_proc) as mock_run:
            run_coverage(tmp_path, test_dirs=["custom_tests/", "extra_tests/"])
        cmd = mock_run.call_args[0][0]
        assert "custom_tests/" in cmd
        assert "extra_tests/" in cmd
        # Should NOT auto-detect tests/
        # (no "tests" as bare arg)

    def test_explicit_source_dirs(self, tmp_path: Path) -> None:
        """Explicit source_dirs are passed instead of auto-detect."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "coverage.json").write_text('{"totals":{},"files":{}}', encoding="utf-8")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = ""
        with patch("codebot.coverage_runner.subprocess.run", return_value=mock_proc) as mock_run:
            run_coverage(tmp_path, source_dirs=["src/", "lib/"])
        cmd = mock_run.call_args[0][0]
        assert "--cov=src/" in cmd
        assert "--cov=lib/" in cmd

    def test_no_auto_detect_when_no_source_dirs(self, tmp_path: Path) -> None:
        """No --cov flag when source_dirs is None and no candidate dir exists."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "coverage.json").write_text('{"totals":{},"files":{}}', encoding="utf-8")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = ""
        with patch("codebot.coverage_runner.subprocess.run", return_value=mock_proc) as mock_run:
            run_coverage(tmp_path)
        cmd = mock_run.call_args[0][0]
        # None of src, lib, codebot, lib_common should be in cmd as --cov
        assert "--cov=src" not in " ".join(cmd)
        assert "--cov=lib" not in " ".join(cmd)
        assert "--cov=codebot" not in " ".join(cmd)
        assert "--cov=lib_common" not in " ".join(cmd)


# ---------------------------------------------------------------------------
# save / load round-trip tests
# ---------------------------------------------------------------------------

class TestSaveLoadCoverageReport:
    def test_roundtrip(self, tmp_path: Path) -> None:
        report = CoverageReport(
            total_statements=42,
            total_covered=21,
            total_coverage_pct=50.0,
            modules={
                "m.py": ModuleCoverage("m.py", 42, 21, [1, 2], 50.0),
            },
            timestamp=1700000000.0,
            pytest_exit_code=0,
        )
        saved = save_coverage_report(report, tmp_path)
        assert saved.exists()

        loaded = load_coverage_report(tmp_path)
        assert loaded is not None
        assert loaded.total_statements == 42
        assert loaded.modules["m.py"].missing == [1, 2]

    def test_load_missing(self, tmp_path: Path) -> None:
        assert load_coverage_report(tmp_path) is None

    def test_load_corrupt(self, tmp_path: Path) -> None:
        (tmp_path / "coverage_report.json").write_text("not json", encoding="utf-8")
        assert load_coverage_report(tmp_path) is None

    def test_load_missing_keys(self, tmp_path: Path) -> None:
        """When JSON is valid but missing required keys, from_dict raises KeyError."""
        (tmp_path / "coverage_report.json").write_text('{"foo": "bar"}', encoding="utf-8")
        assert load_coverage_report(tmp_path) is None

    def test_load_os_error(self, tmp_path: Path) -> None:
        """When reading the file raises OSError, return None."""
        report_file = tmp_path / "coverage_report.json"
        report_file.write_text('{"total_statements": 1}', encoding="utf-8")
        # Make the file unreadable to trigger OSError on read
        report_file.chmod(0o000)
        try:
            assert load_coverage_report(tmp_path) is None
        finally:
            report_file.chmod(0o644)  # Restore permissions for cleanup

    def test_save_creates_state_dir(self, tmp_path: Path) -> None:
        """save_coverage_report creates state dir if it doesn't exist."""
        nested = tmp_path / "a" / "b" / "c"
        report = CoverageReport(
            total_statements=1,
            total_covered=1,
            total_coverage_pct=100.0,
            modules={},
            timestamp=1700000000.0,
            pytest_exit_code=0,
        )
        saved = save_coverage_report(report, nested)
        assert saved.exists()
        loaded = load_coverage_report(nested)
        assert loaded is not None
        assert loaded.total_statements == 1
