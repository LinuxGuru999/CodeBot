#!/usr/bin/env python3
"""Coverage runner — executes pytest-cov and parses results into structured data.

Purpose
-------
Runs pytest with coverage collection, parses the JSON report into
per-module and per-function coverage metrics, and persists results
for consumption by the coverage-to-ticket bridge and alignment scorer.

Why
---
The test_gap_auditor currently guesses at gaps via static file matching.
Actual line/branch coverage measurement is the only reliable signal for
directing test-writing workers toward genuinely untested code paths.

Invariants
----------
- stdlib-only (subprocess, json, pathlib, time)
- Fail-open: if pytest-cov not installed, returns empty result without crashing
- Never modifies source code
- Output is atomic (tmp + rename)
- Timeout bounded (default 300s)
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ModuleCoverage:
    """Coverage data for a single module."""
    path: str
    statements: int
    covered: int
    missing: list[int]
    coverage_pct: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CoverageReport:
    """Aggregate coverage report across all measured modules."""
    total_statements: int
    total_covered: int
    total_coverage_pct: float
    modules: dict[str, ModuleCoverage]
    timestamp: float
    pytest_exit_code: int
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "total_statements": self.total_statements,
            "total_covered": self.total_covered,
            "total_coverage_pct": self.total_coverage_pct,
            "timestamp": self.timestamp,
            "pytest_exit_code": self.pytest_exit_code,
            "error": self.error,
            "modules": {k: v.to_dict() for k, v in self.modules.items()},
        }
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CoverageReport:
        modules = {}
        for k, v in data.get("modules", {}).items():
            modules[k] = ModuleCoverage(**v)
        return cls(
            total_statements=data["total_statements"],
            total_covered=data["total_covered"],
            total_coverage_pct=data["total_coverage_pct"],
            modules=modules,
            timestamp=data["timestamp"],
            pytest_exit_code=data["pytest_exit_code"],
            error=data.get("error"),
        )

    def below_threshold(self, min_pct: float) -> list[ModuleCoverage]:
        """Return modules with coverage below the given percentage."""
        return sorted(
            [m for m in self.modules.values() if m.coverage_pct < min_pct],
            key=lambda m: m.coverage_pct,
        )

    def uncovered_lines(self, module_path: str) -> list[int]:
        """Return uncovered line numbers for a specific module."""
        mod = self.modules.get(module_path)
        if mod is None:
            return []
        return mod.missing


def run_coverage(
    project_root: Path,
    test_dirs: list[str] | None = None,
    source_dirs: list[str] | None = None,
    timeout: int = 300,
    extra_args: list[str] | None = None,
) -> CoverageReport:
    """Execute pytest-cov and parse JSON coverage report.

    Args:
        project_root: Project root directory containing source and tests.
        test_dirs: Test directories to run. Defaults to ["tests/"].
        source_dirs: Source directories to measure. Defaults to auto-detect.
        timeout: Maximum seconds before killing pytest.
        extra_args: Additional pytest arguments.

    Returns:
        CoverageReport with per-module breakdown. Empty if pytest-cov unavailable.
    """
    root = Path(project_root).resolve()
    cov_json = root / ".codebot" / "state" / "coverage.json"
    cov_json.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "python3", "-m", "pytest",
        "--cov-report=json:" + str(cov_json),
        "--cov-report=term",
        "-q", "--tb=no",
    ]

    if source_dirs:
        for sd in source_dirs:
            cmd.append(f"--cov={sd}")
    else:
        for candidate in ("src", "lib", "codebot", "lib_common"):
            if (root / candidate).is_dir():
                cmd.append(f"--cov={candidate}")

    if test_dirs:
        cmd.extend(test_dirs)
    else:
        for candidate in ("tests", "test"):
            if (root / candidate).is_dir():
                cmd.append(candidate)
                break

    if extra_args:
        cmd.extend(extra_args)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return _empty_report(error="python3 not found")
    except subprocess.TimeoutExpired:
        return _empty_report(error=f"pytest timed out after {timeout}s")
    except Exception as e:
        return _empty_report(error=str(e)[:500])

    if not cov_json.exists():
        stderr_snippet = (proc.stderr or "")[:300]
        if "No module named pytest_cov" in stderr_snippet or "pytest-cov" in stderr_snippet:
            return _empty_report(error="pytest-cov not installed")
        return _empty_report(
            error=f"coverage.json not produced. exit={proc.returncode}: {stderr_snippet}",
            exit_code=proc.returncode,
        )

    return _parse_coverage_json(cov_json, proc.returncode)


def _parse_coverage_json(cov_path: Path, exit_code: int) -> CoverageReport:
    try:
        raw = json.loads(cov_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return _empty_report(error=f"failed to parse coverage.json: {e}", exit_code=exit_code)

    totals = raw.get("totals", {})
    files = raw.get("files", {})

    modules: dict[str, ModuleCoverage] = {}
    for filepath, fdata in files.items():
        summary = fdata.get("summary", {})
        stmts = summary.get("num_statements", 0)
        covered = summary.get("covered_lines", 0)
        pct = summary.get("percent_covered", 0.0)
        if isinstance(pct, dict):
            pct = pct.get("display", 0.0)
        missing_raw = fdata.get("missing_lines", [])
        missing = _parse_missing_lines(missing_raw)
        modules[filepath] = ModuleCoverage(
            path=filepath,
            statements=stmts,
            covered=covered,
            missing=missing,
            coverage_pct=float(pct) if pct else 0.0,
        )

    total_stmts = totals.get("num_statements", 0)
    total_covered = totals.get("covered_lines", 0)
    total_pct = totals.get("percent_covered", 0.0)
    if isinstance(total_pct, dict):
        total_pct = total_pct.get("display", 0.0)

    return CoverageReport(
        total_statements=total_stmts,
        total_covered=total_covered,
        total_coverage_pct=float(total_pct) if total_pct else 0.0,
        modules=modules,
        timestamp=time.time(),
        pytest_exit_code=exit_code,
    )


def _parse_missing_lines(raw: Any) -> list[int]:
    if isinstance(raw, list):
        lines: list[int] = []
        for item in raw:
            if isinstance(item, int):
                lines.append(item)
            elif isinstance(item, str):
                for part in item.split(","):
                    part = part.strip()
                    if "-" in part:
                        try:
                            start, end = part.split("-", 1)
                            lines.extend(range(int(start), int(end) + 1))
                        except ValueError:
                            pass
                    elif part.isdigit():
                        lines.append(int(part))
        return sorted(set(lines))
    return []


def _empty_report(error: str | None = None, exit_code: int = -1) -> CoverageReport:
    return CoverageReport(
        total_statements=0,
        total_covered=0,
        total_coverage_pct=0.0,
        modules={},
        timestamp=time.time(),
        pytest_exit_code=exit_code,
        error=error,
    )


def save_coverage_report(report: CoverageReport, state_dir: Path) -> Path:
    """Persist coverage report to state directory atomically."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "coverage_report.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(report.to_json(), encoding="utf-8")
    os.replace(str(tmp), str(path))
    return path


def load_coverage_report(state_dir: Path) -> CoverageReport | None:
    """Load previously saved coverage report."""
    path = state_dir / "coverage_report.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return CoverageReport.from_dict(data)
    except (json.JSONDecodeError, KeyError, OSError):
        return None
