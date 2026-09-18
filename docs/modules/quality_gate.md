# quality_gate.py

Evaluates whether a ticket's implementation satisfies all required and conditional quality gates defined in YAML policy. No ticket reaches COMPLETE state without passing the central quality gatekeeper.

## Key Exports
- `GateResult`: Class
- `GateEvaluation`: Class
- `QualityGatePolicy`: Class
- `load_policy()`: Function
- `evaluate_gate()`: Function
- `run_quality_gates()`: Function
- `record_gate_results()`: Function
- `to_dict()`: Function

## Invariants
- stdlib-only (json, subprocess, re, pathlib, time, enum, dataclasses)
- Gate evaluation never modifies source code
- All gate results are recorded for provenance
- Missing gate tools fail closed (gate not passed) unless configured optional
- Policy is loaded from .codebot/quality_gates.yaml or inline dict
