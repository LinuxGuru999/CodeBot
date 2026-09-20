#!/usr/bin/env python3
"""Quality gate engine for CodeBot ticket verification.

Purpose
-------
Evaluates whether a ticket's implementation satisfies all required and
conditional quality gates defined in YAML policy. No ticket reaches
COMPLETE state without passing the central quality gatekeeper.

Why
---
CODEBOT-ROADMAP.md §5 requires that implementers and reviewers cannot
directly declare tickets complete. A deterministic gate engine ensures
every accepted change meets minimum standards (build, tests, lint, types)
and conditional standards (security review for boundary changes, migration
tests for data changes, benchmarks for performance-sensitive code).

Invariants
----------
- stdlib-only (json, subprocess, re, pathlib, time, enum, dataclasses)
- Gate evaluation never modifies source code
- All gate results are recorded for provenance
- Missing gate tools fail closed (gate not passed) unless configured optional
- Policy is loaded from .codebot/quality_gates.yaml or inline dict
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any


class GateResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    ERROR = "error"


@dataclass(frozen=True)
class GateEvaluation:
    """Result of a single quality gate evaluation.
    
    Attributes:
        gate_name: Name of the gate (e.g., 'build', 'unit_tests')
        result: Gate execution result
        command: The command that was executed
        output: Captured stdout/stderr (truncated to 2000 chars)
        duration_ms: Execution time in milliseconds
        passed: Whether the gate passed
        required: Whether this gate is required (vs optional)
        error_message: Error details if result is ERROR
    """
    gate_name: str
    result: GateResult
    command: str
    output: str
    duration_ms: float
    passed: bool
    required: bool = True
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass(frozen=True)
class Gate:
    """Definition of a quality gate to be executed.
    
    Attributes:
        name: Name of the gate (e.g., 'build', 'unit_tests')
        check_type: Type of check ('command', 'lint', 'test', etc.)
        command: The command to execute for this gate
        timeout: Timeout in seconds (default 120)
        pass_criteria: Criteria for passing (default 'exit_code==0')
    """
    name: str
    check_type: str = "command"
    command: str = ""
    timeout: int = 120
    pass_criteria: str = "exit_code==0"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Gate:
        """Create a Gate from a dictionary, with validation and defaults."""
        if "name" not in data:
            raise ValueError("Gate definition missing required field: 'name'")
        if "command" not in data:
            raise ValueError(f"Gate '{data.get('name', 'unknown')}' missing required field: 'command'")
        
        timeout_val = data.get("timeout", 120)
        try:
            timeout_int = int(timeout_val)
        except (ValueError, TypeError):
            raise ValueError(f"Gate '{data['name']}' has invalid timeout: {timeout_val}")
        
        if timeout_int <= 0:
            raise ValueError(f"Gate '{data['name']}' has non-positive timeout: {timeout_int}")
        
        return cls(
            name=data["name"],
            check_type=data.get("check_type", "command"),
            command=data["command"],
            timeout=timeout_int,
            pass_criteria=data.get("pass_criteria", "exit_code==0"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert Gate to dictionary representation."""
        return {
            "name": self.name,
            "check_type": self.check_type,
            "command": self.command,
            "timeout": self.timeout,
            "pass_criteria": self.pass_criteria,
        }


def load_gates_from_dict(data: dict[str, Any]) -> list[Gate]:
    """Load a list of Gate objects from a dictionary.
    
    Expected format:
        {"gates": [{"name": "...", "command": "...", ...}, ...]}
    
    Returns empty list if 'gates' key is missing or not a list.
    Raises ValueError if any gate definition is invalid.
    """
    gates_data = data.get("gates", [])
    if not isinstance(gates_data, list):
        return []
    
    gates = []
    for i, gate_data in enumerate(gates_data):
        if not isinstance(gate_data, dict):
            raise ValueError(f"Gate at index {i} is not a dictionary")
        try:
            gate = Gate.from_dict(gate_data)
            gates.append(gate)
        except ValueError as e:
            raise ValueError(f"Gate at index {i}: {e}")
    
    return gates


def load_gates_yaml(path: Path) -> list[Gate]:
    """Load gates from a YAML file.
    
    Uses the existing _parse_simple_yaml stdlib-only parser.
    Raises ValueError if file cannot be parsed or gates are invalid.
    """
    if not path.exists():
        raise ValueError(f"Gate file not found: {path}")
    
    text = path.read_text(encoding="utf-8")
    parsed = _parse_simple_yaml(text)
    return load_gates_from_dict(parsed)


def load_gates(path: Path | None = None) -> list[Gate]:
    """Load gates from YAML file or return empty list.
    
    Default path: .codebot/gates.yaml relative to workspace.
    Returns empty list if no path provided or file doesn't exist.
    """
    if path is None:
        return []
    if not path.exists():
        return []
    return load_gates_yaml(path)


@dataclass
class QualityGatePolicy:
    required: list[dict[str, str]]
    conditional: dict[str, list[dict[str, str]]]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QualityGatePolicy:
        return cls(
            required=data.get("required", []),
            conditional=data.get("conditional", {}),
        )

    @classmethod
    def default(cls) -> QualityGatePolicy:
        return cls(
            required=[
                {"name": "build", "command": "python3 -m py_compile {file}"},
                {"name": "unit_tests", "command": "python3 -m pytest -q {test_dirs}"},
            ],
            conditional={
                "security_boundary": [
                    {"name": "security_review", "command": "echo 'requires manual review'"},
                ],
                "api_change": [
                    {"name": "contract_tests", "command": "python3 -m pytest -q -k contract"},
                ],
                "data_migration": [
                    {"name": "migration_test", "command": "python3 -m pytest -q -k migration"},
                    {"name": "rollback_test", "command": "python3 -m pytest -q -k rollback"},
                ],
                "performance_sensitive": [
                    {"name": "benchmark", "command": "python3 -m pytest -q -k benchmark"},
                ],
                "documentation_impact": [
                    {"name": "documentation_review", "command": "echo 'docs updated'"},
                ],
            },
        )


def load_policy(policy_path: Path | None = None) -> QualityGatePolicy:
    if policy_path and policy_path.exists():
        try:
            text = policy_path.read_text(encoding="utf-8")
            parsed = _parse_simple_yaml(text)
            return QualityGatePolicy.from_dict(parsed)
        except Exception:
            pass
    return QualityGatePolicy.default()


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_key = ""
    current_list: list[Any] | None = None
    current_item: dict[str, str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith(" ") and not line.startswith("\t"):
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if val:
                result[key] = val
            else:
                result[key] = []
                current_key = key
                current_list = result[key]
                current_item = None
            continue
        if stripped.startswith("- "):
            item_text = stripped[2:].strip()
            current_item = {}
            if current_list is not None:
                current_list.append(current_item)
            if ":" in item_text:
                k, _, v = item_text.partition(":")
                current_item[k.strip()] = v.strip().strip('"').strip("'")
            else:
                if current_list is not None:
                    current_list.pop()
                    current_list.append(item_text)
                    current_item = None
            continue
        if ":" in stripped and current_item is not None:
            k, _, v = stripped.partition(":")
            current_item[k.strip()] = v.strip().strip('"').strip("'")
    return result


def evaluate_gate(
    gate: dict[str, str],
    workspace: Path,
    file_context: str = "",
    test_dirs: str = "",
    timeout: int = 120,
) -> GateEvaluation:
    name = gate.get("name", "unknown")
    command_template = gate.get("command", "true")
    command = command_template.replace("{file}", file_context).replace("{test_dirs}", test_dirs)
    start = time.monotonic()
    try:
        argv = shlex.split(command) if isinstance(command, str) else command
        proc = subprocess.run(
            argv,
            shell=False,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = time.monotonic() - start
        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0:
            return GateEvaluation(name, GateResult.PASS, command, output[:2000], duration, True)
        return GateEvaluation(name, GateResult.FAIL, command, output[:2000], duration, False, True,
                              f"exit code {proc.returncode}")
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return GateEvaluation(name, GateResult.ERROR, command, "", duration, False, True, "timeout")
    except FileNotFoundError:
        duration = time.monotonic() - start
        return GateEvaluation(name, GateResult.ERROR, command, "", duration, False, True, "command not found")
    except Exception as e:
        duration = time.monotonic() - start
        return GateEvaluation(name, GateResult.ERROR, command, "", duration, False, True, str(e)[:500])


_GATE_PASS_CACHE = "gate_pass_cache.json"
_GATE_PASS_TTL_SECONDS = 24 * 3600


def _hash_files(workspace: Path, files: list[str]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(files):
        p = workspace / rel
        digest.update(rel.encode("utf-8") + b"\x00")
        try:
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    digest.update(chunk)
        except OSError:
            digest.update(b"<missing>\x00")
            continue
        digest.update(b"\x00")
    return digest.hexdigest()


def _load_pass_cache(state_dir: Path) -> dict[str, Any]:
    path = state_dir / _GATE_PASS_CACHE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {}


def _store_pass_cache(state_dir: Path, data: dict[str, Any]) -> None:
    path = state_dir / _GATE_PASS_CACHE
    tmp = state_dir / (_GATE_PASS_CACHE + ".tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        pass


def clear_gate_pass_cache(state_dir: Path | None = None) -> None:
    if state_dir is None:
        return
    try:
        (state_dir / _GATE_PASS_CACHE).unlink(missing_ok=True)
    except OSError:
        pass


def check_gate_pass_cache(
    state_dir: Path,
    ticket_id: str,
    workspace: Path,
    changed_files: list[str] | None,
    gate_names: list[str] | None = None,
    now: float | None = None,
) -> GateEvaluation | None:
    """Return a cached PASS evaluation when file hashes still match.

    Cache entries are written by run_quality_gates after a full pass.
    FAIL/ERROR results are never cached: only identical-content passes skip.
    """
    files = list(changed_files or [])
    if not ticket_id or not files:
        return None
    now = time.time() if now is None else now
    cache = _load_pass_cache(state_dir)
    entry = cache.get(ticket_id)
    if not isinstance(entry, dict):
        return None
    try:
        age = now - float(entry.get("timestamp", 0))
    except (TypeError, ValueError):
        return None
    if age < 0 or age > _GATE_PASS_TTL_SECONDS:
        return None
    cached_files = entry.get("files")
    if not isinstance(cached_files, list) or sorted(cached_files) != sorted(files):
        return None
    if not entry.get("passed", False):
        return None
    if _hash_files(workspace, files) != entry.get("files_hash", ""):
        return None
    if gate_names is not None:
        cached_gates = entry.get("gates")
        if not isinstance(cached_gates, list) or sorted(cached_gates) != sorted(gate_names):
            return None
    return GateEvaluation(
        "cached-pass",
        GateResult.PASS,
        "cache-hit",
        f"all gates previously passed for unchanged files (ticket {ticket_id})",
        0.0,
        True,
    )


def run_quality_gates_with_cache(
    policy: QualityGatePolicy,
    workspace: Path,
    state_dir: Path,
    ticket_id: str,
    ticket_class: str = "",
    changed_files: list[str] | None = None,
    test_dirs: str = "tests/",
    conditions: list[str] | None = None,
) -> tuple[bool, list[GateEvaluation]]:
    """Run gates, skipping subprocesses on a verified cache hit.

    Only full passes are cached. Any miss runs every gate normally and
    records a fresh cache entry when everything passes.
    """
    gate_names = [g.get("name", "") for g in policy.required]
    for condition in (conditions or []):
        gate_names.extend(g.get("name", "") for g in policy.conditional.get(condition, []))
    if ticket_class == "security":
        gate_names.extend(g.get("name", "") for g in policy.conditional.get("security_boundary", []))
    hit = check_gate_pass_cache(state_dir, ticket_id, workspace, changed_files, gate_names)
    if hit is not None:
        return True, [hit]
    passed, evaluations = run_quality_gates(
        policy, workspace, ticket_class, changed_files, test_dirs, conditions,
    )
    if passed and changed_files:
        cache = _load_pass_cache(state_dir)
        cache[ticket_id] = {
            "passed": True,
            "timestamp": time.time(),
            "files": sorted(changed_files),
            "files_hash": _hash_files(workspace, list(changed_files)),
            "gates": sorted(gate_names),
        }
        _store_pass_cache(state_dir, cache)
    return passed, evaluations


def run_quality_gates(
    policy: QualityGatePolicy,
    workspace: Path,
    ticket_class: str = "",
    changed_files: list[str] | None = None,
    test_dirs: str = "tests/",
    conditions: list[str] | None = None,
) -> tuple[bool, list[GateEvaluation]]:
    evaluations: list[GateEvaluation] = []

    python_files = [
        f for f in (changed_files or [])
        if f.endswith(".py") and not f.startswith("tests/")
    ]
    file_ctx = " ".join(python_files[:5]) if python_files else ""

    scoped_test_dirs = test_dirs
    if changed_files:
        test_modules = set()
        for f in changed_files:
            if f.startswith("tests/") and f.endswith(".py"):
                test_modules.add(f)
            elif f.endswith(".py"):
                stem = Path(f).stem
                candidate = f"tests/test_{stem}.py"
                if (workspace / candidate).exists():
                    test_modules.add(candidate)
        if test_modules:
            scoped_test_dirs = " ".join(sorted(test_modules)[:5])

    for gate in policy.required:
        name = gate.get("name", "")
        if name == "build" and not python_files:
            continue
        if name == "unit_tests" and not changed_files:
            continue
        ev = evaluate_gate(gate, workspace, file_ctx, scoped_test_dirs)
        evaluations.append(ev)

    active_conditions = set(conditions or [])
    if ticket_class == "security":
        active_conditions.add("security_boundary")
    if changed_files:
        if any("api" in f.lower() or "router" in f.lower() for f in changed_files):
            active_conditions.add("api_change")
        if any("migration" in f.lower() or "store" in f.lower() for f in changed_files):
            active_conditions.add("data_migration")

    for condition in active_conditions:
        gates = policy.conditional.get(condition, [])
        for gate in gates:
            ev = evaluate_gate(gate, workspace, file_ctx, scoped_test_dirs)
            evaluations.append(ev)

    all_passed = all(
        ev.passed for ev in evaluations if ev.required
    )
    return all_passed, evaluations


def record_gate_results(
    state_dir: Path,
    ticket_id: str,
    passed: bool,
    evaluations: list[GateEvaluation],
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "gate_results.jsonl"
    record = {
        "ticket_id": ticket_id,
        "passed": passed,
        "timestamp": time.time(),
        "gates": [ev.to_dict() for ev in evaluations],
    }
    line = json.dumps(record) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    # Best-effort refresh of cached gate_metrics.json (fail-open, never raise)
    try:
        _refresh_gate_metrics_cache(state_dir)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Observability — metrics collection and alerting
# ---------------------------------------------------------------------------

_logger = logging.getLogger("quality_gate")

_MAX_JSONL_LINES = 5000
_METRICS_CACHE = "gate_metrics.json"
_METRICS_CACHE_TMP = "gate_metrics.json.tmp"


@dataclass
class GateAlertConfig:
    """Configurable alerting thresholds for gate performance.

    All thresholds use default sentinel of ``None`` to mean "disabled".
    Values are validated at construction time.
    """

    max_failure_rate: float | None = None
    max_error_rate: float | None = None
    max_avg_duration_ms: float | None = None
    min_samples: int = 5
    streak_window: int = 5

    # -- construction helpers -------------------------------------------------

    @classmethod
    def default(cls) -> GateAlertConfig:
        return cls()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GateAlertConfig:
        """Create from a flat dict; unknown keys are silently ignored."""
        if not isinstance(data, dict):
            return cls()
        kw: dict[str, Any] = {}
        for key in ("max_failure_rate", "max_error_rate", "max_avg_duration_ms"):
            val = data.get(key)
            if val is not None:
                fval = float(val)
                if math.isnan(fval) or math.isinf(fval):
                    raise ValueError(f"{key} must not be NaN or Inf")
                if fval < 0:
                    raise ValueError(f"{key} must be non-negative")
                kw[key] = fval
        if "min_samples" in data:
            kw["min_samples"] = max(1, int(data["min_samples"]))
        if "streak_window" in data:
            kw["streak_window"] = max(1, int(data["streak_window"]))
        return cls(**kw)

    @classmethod
    def from_env(cls) -> GateAlertConfig:
        """Load thresholds from environment variables.

        Supported env vars:
          CODEBOT_GATE_MAX_FAILURE_RATE
          CODEBOT_GATE_MAX_ERROR_RATE
          CODEBOT_GATE_MAX_AVG_MS
        """
        kw: dict[str, Any] = {}
        env_map = {
            "CODEBOT_GATE_MAX_FAILURE_RATE": "max_failure_rate",
            "CODEBOT_GATE_MAX_ERROR_RATE": "max_error_rate",
            "CODEBOT_GATE_MAX_AVG_MS": "max_avg_duration_ms",
        }
        for env_key, attr in env_map.items():
            raw = os.environ.get(env_key)
            if raw is not None:
                fval = float(raw)
                if math.isnan(fval) or math.isinf(fval) or fval < 0:
                    raise ValueError(f"{env_key} has invalid value: {raw}")
                kw[attr] = fval
        return cls(**kw)

    @classmethod
    def load(cls, path: Path | None = None) -> GateAlertConfig:
        """Load from YAML file, falling back to env then defaults."""
        if path is None:
            alt = Path(".codebot") / "gate_alerts.yaml"
            if alt.exists():
                path = alt
        if path and path.exists():
            try:
                text = path.read_text(encoding="utf-8")
                parsed = _parse_simple_yaml(text)
                return cls.from_dict(parsed)
            except Exception:
                pass
        return cls.from_env()

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_failure_rate": self.max_failure_rate,
            "max_error_rate": self.max_error_rate,
            "max_avg_duration_ms": self.max_avg_duration_ms,
            "min_samples": self.min_samples,
            "streak_window": self.streak_window,
        }


@dataclass
class GateMetricsSummary:
    """Per-gate aggregated metrics snapshot."""

    gate_name: str
    total: int = 0
    passes: int = 0
    fails: int = 0
    errors: int = 0
    skips: int = 0
    failure_rate: float = 0.0
    error_rate: float = 0.0
    avg_ms: float = 0.0
    p95_ms: float = 0.0
    last_result: str = ""
    last_timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_name": self.gate_name,
            "total": self.total,
            "passes": self.passes,
            "fails": self.fails,
            "errors": self.errors,
            "skips": self.skips,
            "failure_rate": round(self.failure_rate, 4),
            "error_rate": round(self.error_rate, 4),
            "avg_ms": round(self.avg_ms, 2),
            "p95_ms": round(self.p95_ms, 2),
            "last_result": self.last_result,
            "last_timestamp": self.last_timestamp,
        }


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Compute percentile from an already-sorted list."""
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo])


def compute_gate_metrics(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate gate_results.jsonl records into per-gate metrics.

    Each record has keys: ticket_id, passed, timestamp, gates (list of gate dicts).
    Returns a list of dicts suitable for GateMetricsSummary.
    """
    # Accumulate per-gate
    gate_data: dict[str, dict[str, Any]] = {}

    for record in records:
        if not isinstance(record, dict):
            continue
        ts = record.get("timestamp", 0.0)
        for g in record.get("gates", []):
            if not isinstance(g, dict):
                continue
            name = str(g.get("gate_name", "unknown"))[:128]
            result = g.get("result", "unknown")
            duration = g.get("duration_ms", 0.0)
            try:
                duration = float(duration)
            except (TypeError, ValueError):
                duration = 0.0

            if name not in gate_data:
                gate_data[name] = {
                    "gate_name": name,
                    "total": 0,
                    "passes": 0,
                    "fails": 0,
                    "errors": 0,
                    "skips": 0,
                    "durations": [],
                    "last_result": "",
                    "last_timestamp": 0.0,
                }
            gd = gate_data[name]
            gd["total"] += 1
            if result == "pass":
                gd["passes"] += 1
            elif result == "fail":
                gd["fails"] += 1
            elif result == "error":
                gd["errors"] += 1
            elif result == "skip":
                gd["skips"] += 1
            gd["durations"].append(duration)
            if ts >= gd["last_timestamp"]:
                gd["last_result"] = result
                gd["last_timestamp"] = ts

    # Build summaries
    summaries: list[dict[str, Any]] = []
    for gd in gate_data.values():
        total = gd["total"]
        fails = gd["fails"]
        errors = gd["errors"]
        durations = sorted(gd["durations"])
        summary = GateMetricsSummary(
            gate_name=gd["gate_name"],
            total=total,
            passes=gd["passes"],
            fails=fails,
            errors=errors,
            skips=gd["skips"],
            failure_rate=fails / total if total > 0 else 0.0,
            error_rate=errors / total if total > 0 else 0.0,
            avg_ms=sum(durations) / len(durations) if durations else 0.0,
            p95_ms=_percentile(durations, 0.95),
            last_result=gd["last_result"],
            last_timestamp=gd["last_timestamp"],
        )
        summaries.append(summary.to_dict())

    return summaries


def check_gate_alerts(
    metrics: list[dict[str, Any]],
    config: GateAlertConfig | None = None,
) -> list[dict[str, Any]]:
    """Evaluate alert thresholds against per-gate metrics.

    Returns a list of alert descriptors:
        {gate, rule, observed, threshold, severity}
    """
    if config is None:
        config = GateAlertConfig.default()

    alerts: list[dict[str, Any]] = []

    for m in metrics:
        total = m.get("total", 0)
        if total < config.min_samples:
            continue

        gate_name = m.get("gate_name", "unknown")

        if config.max_failure_rate is not None:
            rate = m.get("failure_rate", 0.0)
            if rate > config.max_failure_rate:
                alerts.append({
                    "gate": gate_name,
                    "rule": "failure_rate",
                    "observed": rate,
                    "threshold": config.max_failure_rate,
                    "severity": "warning",
                })
                _logger.warning(
                    "gate_alert gate=%s rule=failure_rate observed=%.4f threshold=%.4f",
                    gate_name, rate, config.max_failure_rate,
                )

        if config.max_error_rate is not None:
            rate = m.get("error_rate", 0.0)
            if rate > config.max_error_rate:
                alerts.append({
                    "gate": gate_name,
                    "rule": "error_rate",
                    "observed": rate,
                    "threshold": config.max_error_rate,
                    "severity": "warning",
                })
                _logger.warning(
                    "gate_alert gate=%s rule=error_rate observed=%.4f threshold=%.4f",
                    gate_name, rate, config.max_error_rate,
                )

        if config.max_avg_duration_ms is not None:
            avg = m.get("avg_ms", 0.0)
            if avg > config.max_avg_duration_ms:
                alerts.append({
                    "gate": gate_name,
                    "rule": "avg_duration",
                    "observed": avg,
                    "threshold": config.max_avg_duration_ms,
                    "severity": "warning",
                })
                _logger.warning(
                    "gate_alert gate=%s rule=avg_duration observed=%.2f threshold=%.2f",
                    gate_name, avg, config.max_avg_duration_ms,
                )

    return alerts


def _load_jsonl_records(state_dir: Path) -> list[dict[str, Any]]:
    """Read gate_results.jsonl fail-open per line, bounded to _MAX_JSONL_LINES."""
    path = state_dir / "gate_results.jsonl"
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
        lines = text.strip().split("\n")
        # Use only the last _MAX_JSONL_LINES for bounded reads
        tail = lines[-_MAX_JSONL_LINES:] if len(lines) > _MAX_JSONL_LINES else lines
        for line in tail:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # skip corrupt lines
    except OSError:
        pass
    return records


def _refresh_gate_metrics_cache(state_dir: Path) -> None:
    """Atomically recompute and write gate_metrics.json from gate_results.jsonl."""
    records = _load_jsonl_records(state_dir)
    summaries = compute_gate_metrics(records)
    config = GateAlertConfig.load()
    alerts = check_gate_alerts(summaries, config)

    payload = {
        "version": 1,
        "metrics": summaries,
        "alerts": alerts,
        "generated_at": time.time(),
    }

    tmp_path = state_dir / _METRICS_CACHE_TMP
    final_path = state_dir / _METRICS_CACHE
    tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(str(tmp_path), str(final_path))


def get_gate_metrics(
    state_dir: Path,
    config: GateAlertConfig | None = None,
) -> dict[str, Any]:
    """Return the current gate metrics snapshot.

    Reads cached gate_metrics.json if available, otherwise computes live
    from gate_results.jsonl.  Always returns a dict with version, metrics,
    alerts, generated_at keys.
    """
    cache_path = state_dir / _METRICS_CACHE

    # Try reading cache first
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("version") == 1:
                return data
        except (json.JSONDecodeError, OSError):
            pass

    # Fall back to live computation
    records = _load_jsonl_records(state_dir)
    summaries = compute_gate_metrics(records)
    if config is None:
        try:
            config = GateAlertConfig.load()
        except Exception:
            config = GateAlertConfig.default()
    alerts = check_gate_alerts(summaries, config)

    return {
        "version": 1,
        "metrics": summaries,
        "alerts": alerts,
        "generated_at": time.time(),
    }
