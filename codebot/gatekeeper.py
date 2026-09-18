#!/usr/bin/env python3
"""Central quality gatekeeper for CodeBot ticket completion.

Purpose
-------
The sole authority that transitions tickets from VERIFYING to COMPLETE.
Implementers and reviewers produce evidence; the gatekeeper evaluates it
against the quality gate policy and makes the final determination.

Why
---
CODEBOT-ROADMAP.md §5 mandates that implementers and reviewers must not
directly declare tickets complete. This separation of concerns prevents
agents from skipping verification steps or weakening acceptance criteria
to finish work faster. The gatekeeper is the enforcement point.

Invariants
----------
- stdlib-only
- Only this module may transition tickets to COMPLETE
- Every COMPLETE transition is logged with full gate evidence
- Gate failures trigger REWORK, not silent bypass
- Maximum 3 rework attempts before cycling back to REWORK for QA review
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("gatekeeper")

MAX_REWORK_ATTEMPTS = 3


class Gatekeeper:
    def __init__(
        self,
        state_dir: Path,
        policy_path: Path | None = None,
        workspace: Path | None = None,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._policy_path = policy_path
        self._workspace = workspace or Path.cwd()
        self._log_path = self._state_dir / "gatekeeper_log.jsonl"

    def verify_ticket(
        self,
        ticket_id: str,
        ticket_class: str,
        changed_files: list[str],
        test_dirs: str = "tests/",
        conditions: list[str] | None = None,
        rework_count: int = 0,
    ) -> dict[str, Any]:
        from codebot.quality_gate import (
            load_policy,
            run_quality_gates,
            record_gate_results,
        )

        policy = load_policy(self._policy_path)
        passed, evaluations = run_quality_gates(
            policy=policy,
            workspace=self._workspace,
            ticket_class=ticket_class,
            changed_files=changed_files,
            test_dirs=test_dirs,
            conditions=conditions,
        )

        record_gate_results(self._state_dir, ticket_id, passed, evaluations)

        if passed:
            decision = "COMPLETE"
            logger.info("ticket %s PASSED all quality gates", ticket_id)
        else:
            decision = "REWORK"
            if rework_count >= MAX_REWORK_ATTEMPTS:
                logger.warning(
                    "ticket %s FAILED %d attempts — triggering prompt evolution + REWORK",
                    ticket_id, rework_count + 1,
                )
            else:
                logger.info(
                    "ticket %s FAILED gates (attempt %d) — sending to REWORK",
                    ticket_id, rework_count + 1,
                )

        failed_gates = [
            ev.gate_name for ev in evaluations
            if ev.result.value not in ("pass", "skip")
        ]

        result = {
            "ticket_id": ticket_id,
            "decision": decision,
            "passed": passed,
            "failed_gates": failed_gates,
            "total_gates": len(evaluations),
            "rework_count": rework_count,
            "evolve_prompt": not passed and rework_count >= MAX_REWORK_ATTEMPTS,
            "timestamp": time.time(),
        }

        self._log_decision(result)
        return result

    def _log_decision(self, result: dict[str, Any]) -> None:
        line = json.dumps(result) + "\n"
        import os
        fd = os.open(
            str(self._log_path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)

    def get_history(self, ticket_id: str) -> list[dict[str, Any]]:
        if not self._log_path.exists():
            return []
        results = []
        try:
            with open(self._log_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("ticket_id") == ticket_id:
                            results.append(entry)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return results
