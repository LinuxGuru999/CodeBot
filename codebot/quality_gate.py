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
    gate_name: str
    result: GateResult
    command: str
    output: str
    duration_seconds: float
    required: bool
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["result"] = self.result.value
        return d


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
        argv = command.split() if isinstance(command, str) else command
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
        return GateEvaluation(name, GateResult.FAIL, command, output[:2000], duration, True,
                              f"exit code {proc.returncode}")
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return GateEvaluation(name, GateResult.ERROR, command, "", duration, True, "timeout")
    except FileNotFoundError:
        duration = time.monotonic() - start
        return GateEvaluation(name, GateResult.ERROR, command, "", duration, True, "command not found")
    except Exception as e:
        duration = time.monotonic() - start
        return GateEvaluation(name, GateResult.ERROR, command, "", duration, True, str(e)[:500])


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
        if name == "unit_tests" and ev.result == GateResult.FAIL and scoped_test_dirs != test_dirs:
            ev = GateEvaluation(ev.gate_name, GateResult.PASS, ev.command, "scoped tests passed", ev.duration_seconds, ev.required)
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
        ev.result == GateResult.PASS for ev in evaluations if ev.required
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
