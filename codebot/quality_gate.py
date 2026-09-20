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

import json
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
