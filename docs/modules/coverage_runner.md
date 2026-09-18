# coverage_runner.py

Runs pytest with coverage collection, parses the JSON report into per-module and per-function coverage metrics, and persists results for consumption by the coverage-to-ticket bridge and alignment scorer.

## Key Exports
- `ModuleCoverage`: Class
- `CoverageReport`: Class
- `run_coverage()`: Function
- `save_coverage_report()`: Function
- `load_coverage_report()`: Function
- `to_dict()`: Function
- `to_dict()`: Function

## Invariants
- stdlib-only (subprocess, json, pathlib, time)
- Fail-open: if pytest-cov not installed, returns empty result without crashing
- Never modifies source code
- Output is atomic (tmp + rename)
- Timeout bounded (default 300s)
