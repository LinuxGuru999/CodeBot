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

YAML Schema
-----------
Gate definitions are loaded from YAML files with the following structure:

    gates:
      - name: build                    # Required: unique gate identifier
        check_type: command            # Optional: 'command', 'lint', 'test' (default: 'command')
        command: make build            # Required: shell command to execute
        timeout: 120                   # Optional: seconds before timeout (default: 120)
        pass_criteria: exit_code==0    # Optional: success condition (default: 'exit_code==0')

Example policy file (.codebot/quality_gates.yaml):

    required:
      - name: build
        command: python3 -m py_compile {file}
      - name: unit_tests
        command: python3 -m pytest -q {test_dirs}
    conditional:
      security_boundary:
        - name: security_review_verdict
          command: python3 scripts/gate_checks.py reviewer_verdict {ticket_id}
      data_migration:
        - name: migration_test
          command: python3 -m pytest -q -k migration {test_dirs}

Invariants
----------
- stdlib-only (json, subprocess, re, pathlib, time, enum, dataclasses)
- Gate evaluation never modifies source code
- All gate results are recorded for provenance
- Missing gate tools fail closed (gate not passed) unless configured optional
- Policy is loaded from .codebot/quality_gates.yaml or inline dict
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any


@contextlib.contextmanager
def _sigchld_dfl():
    """Temporarily restore SIGCHLD to SIG_DFL if it is SIG_IGN.

    On Linux, SIGCHLD == SIG_IGN causes the kernel to auto-reap children
    and discard their exit status, so ``waitpid``/``subprocess`` always
    reports 0 (spurious PASS).  The orchestrator sets SIG_IGN to avoid
    zombies; gate evaluation must not inherit that disposition.  Only the
    main thread may change signal handlers, so this is a no-op in workers
    — callers must enter the context in the main thread before dispatching.
    """
    old = None
    changed = False
    if hasattr(signal, "SIGCHLD"):
        try:
            if threading.current_thread() is threading.main_thread():
                cur = signal.getsignal(signal.SIGCHLD)
                if cur == signal.SIG_IGN:
                    old = cur
                    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
                    changed = True
        except (ValueError, OSError):
            old = None
            changed = False
    try:
        yield
    finally:
        if changed:
            try:
                signal.signal(signal.SIGCHLD, old)  # type: ignore[arg-type]
            except (ValueError, OSError):
                pass


class GateStatus(str, Enum):
    """Enumeration of possible gate execution statuses."""
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    ERROR = "error"


@dataclass(frozen=True)
class GateResult:
    """Simplified result of a quality gate evaluation.
    
    This is the foundational data structure for gate results, containing
    the essential information needed to determine if a gate passed.
    
    Attributes:
        gate_name: Name of the gate (e.g., 'build', 'unit_tests')
        passed: Whether the gate passed
        output: Captured stdout/stderr from gate execution
        duration_ms: Execution time in milliseconds
        error: Error message if gate failed or errored, empty string otherwise
    """
    gate_name: str
    passed: bool
    output: str
    duration_ms: float
    error: str = ""


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
    result: GateStatus
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
class GateReport:
    """Aggregated report of multiple gate evaluations.
    
    Attributes:
        gate_results: List of individual gate evaluation results
        total_gates: Total number of gates executed
        passed_gates: Number of gates that passed
        failed_gates: Number of gates that failed
        error_gates: Number of gates that errored
        skipped_gates: Number of gates that were skipped
        all_passed: Whether all required gates passed
        duration_ms: Total execution time in milliseconds
        summary: Human-readable summary string with counts (e.g., '5 passed, 0 failed, 1 warning')
    """
    gate_results: list[GateEvaluation]
    total_gates: int
    passed_gates: int
    failed_gates: int
    error_gates: int
    skipped_gates: int
    all_passed: bool
    duration_ms: float
    summary: str = ""

    @classmethod
    def from_evaluations(cls, evaluations: list[GateEvaluation], duration_ms: float) -> GateReport:
        """Create a GateReport from a list of GateEvaluation objects."""
        total = len(evaluations)
        passed = sum(1 for e in evaluations if e.result == GateStatus.PASS)
        failed = sum(1 for e in evaluations if e.result == GateStatus.FAIL)
        errors = sum(1 for e in evaluations if e.result == GateStatus.ERROR)
        skipped = sum(1 for e in evaluations if e.result == GateStatus.SKIP)
        all_passed = all(e.passed for e in evaluations if e.required)
        
        # Build summary string
        parts = []
        if passed:
            parts.append(f"{passed} passed")
        if failed:
            parts.append(f"{failed} failed")
        if errors:
            parts.append(f"{errors} error" + ("s" if errors != 1 else ""))
        if skipped:
            parts.append(f"{skipped} skipped")
        summary = ", ".join(parts) if parts else "0 passed"
        
        return cls(
            gate_results=evaluations,
            total_gates=total,
            passed_gates=passed,
            failed_gates=failed,
            error_gates=errors,
            skipped_gates=skipped,
            all_passed=all_passed,
            duration_ms=duration_ms,
            summary=summary,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert GateReport to dictionary representation."""
        return {
            "gate_results": [e.to_dict() for e in self.gate_results],
            "total_gates": self.total_gates,
            "passed_gates": self.passed_gates,
            "failed_gates": self.failed_gates,
            "error_gates": self.error_gates,
            "skipped_gates": self.skipped_gates,
            "all_passed": self.all_passed,
            "duration_ms": self.duration_ms,
            "summary": self.summary,
        }


def aggregate_verdict(gate_results: list[GateResult]) -> str:
    """Compute overall verdict from a list of GateResult objects.
    
    Returns:
        'PASS' if all gates pass
        'FAIL' if any gate fails (required gate failure)
        'WARN' if only non-blocking gates fail (i.e., no required gate failures but some failures exist)
    
    Args:
        gate_results: List of GateResult objects to evaluate.
    
    Returns:
        One of 'PASS', 'FAIL', or 'WARN'.
    """
    if not gate_results:
        return "PASS"
    
    has_failure = any(not r.passed for r in gate_results)
    has_required_failure = any(not r.passed and r.required for r in gate_results)
    
    if has_required_failure:
        return "FAIL"
    elif has_failure:
        return "WARN"
    else:
        return "PASS"


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


def _parse_gates_yaml(text: str) -> dict[str, Any]:
    """Parse a simple YAML file containing a 'gates:' list.

    Supports format:
        gates:
          - name: foo
            command: bar
            timeout: 120

    Returns dict with 'gates' key containing list of gate dicts.
    Falls back to empty gates list if format not recognized.
    """
    result: dict[str, Any] = {"gates": []}
    in_gates = False
    current_gate: dict[str, str] | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())

        if indent == 0 and stripped.startswith("gates:"):
            in_gates = True
            current_gate = None
            continue

        if in_gates:
            if indent == 0 and not stripped.startswith("-"):
                # New top-level key, stop parsing gates
                in_gates = False
                current_gate = None
                continue

            if stripped.startswith("- "):
                # Start of new gate item
                item_text = stripped[2:].strip()
                if ":" in item_text:
                    key, _, value = item_text.partition(":")
                    current_gate = {key.strip(): value.strip().strip('"').strip("'")}
                    result["gates"].append(current_gate)
                else:
                    current_gate = None
                continue

            if ":" in stripped and current_gate is not None and indent > 0:
                # Continuation of current gate
                key, _, value = stripped.partition(":")
                current_gate[key.strip()] = value.strip().strip('"').strip("'")
                continue

    return result


def load_gates_yaml(path: Path) -> list[Gate]:
    """Load gates from a YAML file.
    
    Supports both policy format (required/conditional) and direct gate list format (gates:).
    Raises ValueError if file cannot be parsed or gates are invalid.
    """
    if not path.exists():
        raise ValueError(f"Gate file not found: {path}")
    
    text = path.read_text(encoding="utf-8")
    # Try parsing as gate list first
    parsed = _parse_gates_yaml(text)
    if parsed.get("gates"):
        return load_gates_from_dict(parsed)
    # Fall back to policy format parser
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


def parse_gates(gates_dir: Path) -> list[Gate]:
    """Load all gates from YAML files in a directory.

    Recursively finds all .yaml and .yml files in the directory and loads
    gates from each file. Returns a combined list of all gates found.

    Args:
        gates_dir: Directory containing gate definition YAML files.

    Returns:
        List of Gate objects loaded from all YAML files in the directory.
        Returns empty list if directory doesn't exist or contains no valid gate files.
    """
    if not gates_dir.exists() or not gates_dir.is_dir():
        return []

    all_gates: list[Gate] = []
    for pattern in ["*.yaml", "*.yml"]:
        for yaml_file in gates_dir.glob(pattern):
            try:
                gates = load_gates_yaml(yaml_file)
                all_gates.extend(gates)
            except (ValueError, OSError):
                # Skip invalid files but continue processing others
                continue

    return all_gates


def run_all_gates(
    gates_dir: Path,
    max_concurrent: int = 4,
    workspace: Path | None = None,
    default_timeout: int = 120,
) -> GateReport:
    """Execute all gates from a directory concurrently with configurable concurrency.

    Loads gate definitions from YAML files in the specified directory, executes them
    concurrently using ThreadPoolExecutor with the specified maximum number of workers,
    enforces per-gate timeout via evaluate_gate(), and aggregates results into a GateReport.

    Args:
        gates_dir: Directory containing gate definition YAML files.
        max_concurrent: Maximum number of gates to execute simultaneously (default 4).
        workspace: Working directory for gate execution. Defaults to current directory.
        default_timeout: Default timeout in seconds for each gate (default 120).

    Returns:
        GateReport containing all gate results and aggregated statistics.

    Note:
        All I/O operations are bounded per Constitution §4. Gates are executed
        concurrently but never exceed max_concurrent simultaneous executions.
    """
    if workspace is None:
        workspace = Path.cwd()

    # Load all gates from the directory
    gates = parse_gates(gates_dir)
    if not gates:
        return GateReport(
            gate_results=[],
            total_gates=0,
            passed_gates=0,
            failed_gates=0,
            error_gates=0,
            skipped_gates=0,
            all_passed=True,
            duration_ms=0.0,
        )

    start_time = time.monotonic()

    def _execute_gate(gate: Gate) -> GateEvaluation:
        """Execute a single gate and return its evaluation."""
        gate_dict = gate.to_dict()
        return evaluate_gate(
            gate_dict,
            workspace=workspace,
            timeout=min(gate.timeout, default_timeout),
        )

    evaluations: list[GateEvaluation] = []

    # Execute gates concurrently with limited parallelism
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as executor:
        futures = {
            executor.submit(_execute_gate, gate): gate
            for gate in gates
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                evaluations.append(result)
            except Exception as e:
                # Should not happen as evaluate_gate catches all exceptions,
                # but handle it gracefully just in case
                evaluations.append(GateEvaluation(
                    gate_name="unknown",
                    result=GateStatus.ERROR,
                    command="",
                    output=str(e)[:2000],
                    duration_ms=0.0,
                    passed=False,
                    required=True,
                    error_message=f"Unexpected error: {e}",
                ))

    end_time = time.monotonic()
    duration_ms = (end_time - start_time) * 1000

    # Sort evaluations by gate name for consistent ordering
    evaluations.sort(key=lambda ev: ev.gate_name)

    return GateReport.from_evaluations(evaluations, duration_ms)


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
                    {"name": "security_review_verdict", "command": "python3 scripts/gate_checks.py reviewer_verdict {ticket_id} security_reviewer {state_dir}"},
                    {"name": "adversarial_test", "command": "python3 -m pytest -q -k security {test_dirs}"},
                ],
                "api_change": [
                    {"name": "contract_tests", "command": "python3 -m pytest -q -k contract {test_dirs}"},
                ],
                "data_migration": [
                    {"name": "migration_test", "command": "python3 -m pytest -q -k migration {test_dirs}"},
                    {"name": "rollback_test", "command": "python3 -m pytest -q -k rollback {test_dirs}"},
                ],
                "performance_sensitive": [
                    {"name": "benchmark", "command": "python3 -m pytest -q -k benchmark {test_dirs}"},
                ],
                "documentation_impact": [
                    {"name": "documentation_review", "command": "python3 scripts/gate_checks.py docs_changed {changed_files}"},
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
    result: dict[str, Any] = {"required": [], "conditional": {}}
    section = ""
    condition = ""
    current_gate: dict[str, str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            key, _, val = stripped.partition(":")
            section = key.strip()
            condition = ""
            current_gate = None
            if val:
                result[section] = val.strip().strip('"').strip("'")
            elif section == "required":
                result[section] = []
            elif section == "conditional":
                result[section] = {}
            continue
        if section == "conditional" and indent == 2 and stripped.endswith(":"):
            condition = stripped[:-1].strip()
            result["conditional"][condition] = []
            current_gate = None
            continue
        if stripped.startswith("- "):
            item_text = stripped[2:].strip()
            key, _, value = item_text.partition(":")
            current_gate = {key.strip(): value.strip().strip('"').strip("'")}
            if section == "required":
                result["required"].append(current_gate)
            elif section == "conditional" and condition:
                result["conditional"][condition].append(current_gate)
            continue
        if ":" in stripped and current_gate is not None:
            k, _, v = stripped.partition(":")
            current_gate[k.strip()] = v.strip().strip('"').strip("'")
    return result


def evaluate_gate(
    gate: dict[str, str],
    workspace: Path,
    file_context: str = "",
    test_dirs: str = "",
    timeout: int = 120,
    ticket_id: str = "",
    state_dir: str = "",
    changed_files: str = "",
) -> GateEvaluation:
    name = gate.get("name", "unknown")
    is_required = gate.get("required", "true")
    if isinstance(is_required, bool):
        required = is_required
    else:
        required = str(is_required).lower() not in ("false", "no", "0")
    command_template = gate.get("command", "true")
    def _q(s: str) -> str:
        if not s:
            return ""
        try:
            parts = shlex.split(s)
        except ValueError:
            return shlex.quote(s)
        return " ".join(shlex.quote(p) for p in parts)
    command = (
        command_template
        .replace("{file}", _q(file_context))
        .replace("{test_dirs}", _q(test_dirs))
        .replace("{ticket_id}", _q(ticket_id))
        .replace("{state_dir}", _q(state_dir))
        .replace("{changed_files}", _q(changed_files))
    )
    start = time.monotonic()
    try:
        argv = shlex.split(command) if isinstance(command, str) else command
        with _sigchld_dfl():
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
            return GateEvaluation(name, GateStatus.PASS, command, output[:2000], duration, True, required)
        if proc.returncode == 5:
            return GateEvaluation(name, GateStatus.ERROR, command, output[:2000], duration, False, required,
                                  "infrastructure: no tests collected (exit code 5)")
        return GateEvaluation(name, GateStatus.FAIL, command, output[:2000], duration, False, required,
                              f"exit code {proc.returncode}")
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return GateEvaluation(name, GateStatus.ERROR, command, "", duration, False, required, "timeout")
    except FileNotFoundError:
        duration = time.monotonic() - start
        return GateEvaluation(name, GateStatus.ERROR, command, "", duration, False, required, "command not found")
    except Exception as e:
        duration = time.monotonic() - start
        return GateEvaluation(name, GateStatus.ERROR, command, "", duration, False, required, str(e)[:500])


_GATE_PASS_CACHE = "gate_pass_cache.json"
_GATE_PASS_TTL_SECONDS = 24 * 3600


def _hash_files(workspace: Path, files: list[str]) -> str:
    digest = hashlib.sha256()
    try:
        workspace_resolved = workspace.resolve()
    except OSError:
        workspace_resolved = workspace
    for rel in sorted(files):
        p = workspace / rel
        # Validate path is within workspace and exists.
        # Invalid/missing files are skipped entirely to ensure deterministic
        # hashing regardless of sort order of invalid entries.
        try:
            candidate = p.resolve()
            try:
                is_inside = candidate.is_relative_to(workspace_resolved)
            except (ValueError, AttributeError):
                try:
                    candidate.relative_to(workspace_resolved)
                    is_inside = True
                except ValueError:
                    is_inside = False
            if not is_inside or not candidate.exists():
                continue
            with open(candidate, "rb") as f:
                digest.update(rel.encode("utf-8") + b"\x00")
                for chunk in iter(lambda: f.read(65536), b""):
                    digest.update(chunk)
                digest.update(b"\x00")
        except (OSError, ValueError):
            continue
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
        GateStatus.PASS,
        "cache-hit",
        f"all gates previously passed for unchanged files (ticket {ticket_id})",
        0.0,
        True,
    )


_policy_revision_cache: dict[int, str] = {}


def _policy_revision(policy: QualityGatePolicy) -> str:
    """Compute a deterministic revision fingerprint for the gate policy.

    Memoized by policy object identity — within a single tick the same
    policy object is reused across all tickets, so this avoids redundant
    SHA-256 computation.
    """
    pid = id(policy)
    cached = _policy_revision_cache.get(pid)
    if cached is not None:
        return cached
    parts: list[str] = []
    for g in policy.required:
        parts.append(f"{g.get('name', '')}:{g.get('command', '')}:{g.get('check_type', '')}:{g.get('timeout', 120)}:{g.get('pass_criteria', '')}")
    for condition, gates in sorted(policy.conditional.items()):
        for g in gates:
            parts.append(f"{condition}/{g.get('name', '')}:{g.get('command', '')}:{g.get('check_type', '')}:{g.get('timeout', 120)}:{g.get('pass_criteria', '')}")
    raw = "|".join(parts)
    result = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    _policy_revision_cache[pid] = result
    return result


def _workspace_revision(workspace: Path) -> str:
    """Compute a workspace revision fingerprint from git HEAD.

    For non-git workspaces, returns a stable constant. File content changes
    are already detected by the files_hash dimension, so mtime-based
    fingerprints are unnecessary and cause false cache invalidation when
    gate execution creates artifacts (e.g. __pycache__) in the workspace.
    """
    git_head = workspace / ".git" / "HEAD"
    try:
        if git_head.exists():
            content = git_head.read_text(encoding="utf-8").strip()
            if content.startswith("ref:"):
                ref_path = workspace / ".git" / content.split(":", 1)[1].strip()
                if ref_path.exists():
                    return ref_path.read_text(encoding="utf-8").strip()[:40]
            return content[:40]
    except OSError:
        pass
    return "no-git"


_review_evidence_cache: dict[str, tuple[float, int, str]] = {}


def _review_evidence_hash(state_dir: Path, ticket_id: str) -> str:
    cache_key = f"{state_dir}:{ticket_id}"
    try:
        from codebot.review_store import ticket_reviews_dir as _trd
        review_dir = _trd(state_dir, ticket_id)
    except Exception:
        review_dir = state_dir / "reviews" / ticket_id.replace("/", "_").replace("\\", "_")
    file_keys: list[tuple[str, float, int]] = []
    if review_dir.exists():
        for path in sorted(review_dir.glob("*.json")):
            if path.parent.name == "quarantine":
                continue
            try:
                st = path.stat()
                file_keys.append((path.name, st.st_mtime, st.st_size))
            except OSError:
                file_keys.append((path.name, 0.0, -1))
    fingerprint = str(file_keys)
    cached = _review_evidence_cache.get(cache_key)
    if cached is not None and cached[0] == hash(fingerprint):
        return cached[2]
    digest = hashlib.sha256()
    if review_dir.exists():
        for path in sorted(review_dir.glob("*.json")):
            if path.parent.name == "quarantine":
                continue
            try:
                data = path.read_text(encoding="utf-8")
                digest.update(path.name.encode("utf-8"))
                digest.update(data.encode("utf-8"))
            except OSError:
                digest.update(path.name.encode("utf-8"))
                digest.update(b"<unreadable>")
    result = digest.hexdigest()[:16]
    _review_evidence_cache[cache_key] = (hash(fingerprint), 0, result)
    return result


def run_quality_gates_with_cache(
    policy: QualityGatePolicy,
    workspace: Path,
    state_dir: Path,
    ticket_id: str,
    ticket_class: str = "",
    changed_files: list[str] | None = None,
    test_dirs: str = "tests/",
    conditions: list[str] | None = None,
    ticket_revision: float = 0.0,
) -> tuple[bool, list[GateEvaluation]]:
    """Run gates, skipping subprocesses on a verified cache hit.

    Only full passes are cached. Any miss runs every gate normally and
    records a fresh cache entry when everything passes.

    Cache validity requires ALL of these dimensions to match:
    - ticket_id, files list, files content hash
    - gate names (derived from policy + conditions + ticket_class)
    - ticket_revision (updated_at timestamp)
    - policy_revision (fingerprint of all gate commands/config)
    - conditions list
    - workspace_revision (git HEAD or mtime)
    - review_evidence_hash (structured review findings)

    Any mismatch forces a full gate re-run.
    """
    gate_names = [g.get("name", "") for g in policy.required]
    for condition in (conditions or []):
        gate_names.extend(g.get("name", "") for g in policy.conditional.get(condition, []))
    if ticket_class == "security":
        gate_names.extend(g.get("name", "") for g in policy.conditional.get("security_boundary", []))

    pol_rev = _policy_revision(policy)
    ws_rev = _workspace_revision(workspace)
    rev_ev = _review_evidence_hash(state_dir, ticket_id)
    cond_key = sorted(conditions or [])

    hit = check_gate_pass_cache(state_dir, ticket_id, workspace, changed_files, gate_names)
    if hit is not None:
        cache = _load_pass_cache(state_dir)
        entry = cache.get(ticket_id, {})
        if isinstance(entry, dict):
            if entry.get("ticket_revision", 0.0) != ticket_revision:
                hit = None
            elif entry.get("policy_revision", "") != pol_rev:
                hit = None
            elif sorted(entry.get("conditions", [])) != cond_key:
                hit = None
            elif entry.get("workspace_revision", "") != ws_rev:
                hit = None
            elif entry.get("review_evidence_hash", "") != rev_ev:
                hit = None
    if hit is not None:
        return True, [hit]

    passed, evaluations = run_quality_gates(
        policy, workspace, ticket_class, changed_files, test_dirs, conditions,
        ticket_id=ticket_id, state_dir=str(state_dir),
    )
    if passed and changed_files:
        cache = _load_pass_cache(state_dir)
        cache[ticket_id] = {
            "passed": True,
            "timestamp": time.time(),
            "files": sorted(changed_files),
            "files_hash": _hash_files(workspace, list(changed_files)),
            "gates": sorted(gate_names),
            "ticket_revision": ticket_revision,
            "policy_revision": pol_rev,
            "conditions": cond_key,
            "workspace_revision": ws_rev,
            "review_evidence_hash": rev_ev,
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
    ticket_id: str = "",
    state_dir: str = "",
) -> tuple[bool, list[GateEvaluation]]:
    evaluations: list[GateEvaluation] = []

    python_files = [
        f for f in (changed_files or [])
        if f.endswith(".py") and not f.startswith("tests/")
    ]
    # Pass raw space-joined strings; evaluate_gate's _q() handles all quoting.
    # Pre-quoting here would cause double-quoting when _q() re-quotes.
    file_ctx = " ".join(python_files[:5]) if python_files else ""
    changed_files_str = " ".join(changed_files or [])

    # Build scoped test_dirs as raw space-joined string; _q() quotes safely.
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
        else:
            scoped_test_dirs = test_dirs or ""
    else:
        scoped_test_dirs = test_dirs or ""

    gates_to_run: list[dict[str, str]] = []
    for gate in policy.required:
        name = gate.get("name", "")
        if name == "build" and not python_files:
            continue
        if name == "unit_tests" and not changed_files:
            continue
        gates_to_run.append(gate)

    active_conditions = set(conditions or [])
    if ticket_class == "security":
        active_conditions.add("security_boundary")
    if changed_files:
        if any("api" in f.lower() or "router" in f.lower() for f in changed_files):
            active_conditions.add("api_change")
        if any("migration" in f.lower() or "store" in f.lower() for f in changed_files):
            active_conditions.add("data_migration")
        auth_keywords = ("auth", "login", "session", "token", "credential", "password", "oauth")
        if any(kw in f.lower() for f in changed_files for kw in auth_keywords):
            active_conditions.add("auth_security_review")
        perf_keywords = ("benchmark", "perf", "profile", "timing")
        if any(kw in f.lower() for f in changed_files for kw in perf_keywords):
            active_conditions.add("performance_sensitive")
        doc_keywords = ("doc", "readme", "changelog", "adr")
        if any(kw in f.lower() for f in changed_files for kw in doc_keywords):
            active_conditions.add("documentation_impact")
        frontend_keywords = ("template", "static", "css", "html", "component", "view")
        if any(kw in f.lower() for f in changed_files for kw in frontend_keywords):
            active_conditions.add("frontend_change")

    for condition in active_conditions:
        for gate in policy.conditional.get(condition, []):
            gates_to_run.append(gate)

    def _eval(gate: dict[str, str]) -> GateEvaluation:
        return evaluate_gate(
            gate,
            workspace=workspace,
            file_context=file_ctx,
            test_dirs=scoped_test_dirs,
            ticket_id=ticket_id,
            state_dir=state_dir,
            changed_files=changed_files_str,
        )

    if len(gates_to_run) > 1:
        # Ensure SIGCHLD is DFL in main thread so children reaped with correct exit codes
        with _sigchld_dfl():
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(gates_to_run), 4)) as executor:
                futures = {
                    executor.submit(_eval, gate): gate
                    for gate in gates_to_run
                }
                evaluations = []
                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    evaluations.append(result)
                evaluations.sort(key=lambda ev: ev.gate_name)
    else:
        # Also wrap single-gate case for consistency
        with _sigchld_dfl():
            evaluations = [_eval(g) for g in gates_to_run]

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


# Bounded tail read: seek from end and read at most this many bytes.
_MAX_JSONL_BYTES = 1_048_576  # 1 MiB


def _load_jsonl_records(state_dir: Path) -> list[dict[str, Any]]:
    """Read gate_results.jsonl fail-open per line, bounded tail read.

    Uses streaming line-by-line reading with a byte counter to avoid
    allocating the entire bounded chunk as a single string in memory.
    """
    path = state_dir / "gate_results.jsonl"
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        size = path.stat().st_size
        read_size = min(size, _MAX_JSONL_BYTES)
        bytes_read = 0
        with open(path, "rb") as f:
            if size > read_size:
                f.seek(-read_size, os.SEEK_END)
                # Drop partial first line (since we may have started mid-line)
                discarded = f.readline()
                bytes_read += len(discarded)
            # Stream line-by-line with byte budget enforcement
            for raw_line in f:
                bytes_read += len(raw_line)
                if bytes_read > _MAX_JSONL_BYTES:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # skip corrupt lines
        # Ensure we return at most _MAX_JSONL_LINES records (newest last)
        if len(records) > _MAX_JSONL_LINES:
            records = records[-_MAX_JSONL_LINES:]
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
