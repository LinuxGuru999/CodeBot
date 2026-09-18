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
        self._transition_ticket(ticket_id, decision, failed_gates)
        return result

    def _transition_ticket(self, ticket_id: str, decision: str, failed_gates: list[str] | None = None) -> None:
        try:
            from codebot.ticket_engine import TicketStore, TicketState

            store_path = self._state_dir / "tickets.json"
            if not store_path.exists():
                for alt in [
                    Path(".codebot/state/tickets.json"),
                    self._state_dir.parent / "tickets.json",
                ]:
                    if alt.exists():
                        store_path = alt
                        break

            if not store_path.exists():
                logger.warning("tickets.json not found for ticket %s", ticket_id)
                return

            store = TicketStore(store_path)
            ticket = store.get(ticket_id)

            if ticket is None:
                logger.warning("ticket %s not found in store", ticket_id)
                return

            if decision == "COMPLETE":
                if ticket.state == TicketState.VERIFYING:
                    store.transition(ticket_id, TicketState.COMPLETE)
                    logger.info("ticket %s transitioned to COMPLETE", ticket_id)
                else:
                    logger.info(
                        "ticket %s in state %s — skipping COMPLETE transition",
                        ticket_id, ticket.state.value,
                    )
            elif decision == "REWORK":
                if ticket.state in (TicketState.REVIEWING, TicketState.VERIFYING):
                    reviewer_feedback = self._collect_reviewer_feedback(ticket_id, failed_gates)
                    store.transition(ticket_id, TicketState.REWORK, reviewer_feedback)
                    logger.info("ticket %s transitioned to REWORK with %d feedback items", ticket_id, len(reviewer_feedback))
                else:
                    logger.info(
                        "ticket %s in state %s — skipping REWORK transition",
                        ticket_id, ticket.state.value,
                    )
        except Exception as e:
            logger.error("failed to transition ticket %s: %s", ticket_id, e)

    def _collect_reviewer_feedback(self, ticket_id: str, failed_gates: list[str] | None = None) -> list[dict]:
        feedback = []
        review_patterns = [
            "correctness_review.json",
            "security_review.json",
            "architecture_review.json",
            "performance_review.json",
            "simplicity_review.json",
            "test_review.json",
            "documentation_review.json",
        ]
        for pattern in review_patterns:
            review_path = self._state_dir / pattern
            if not review_path.exists():
                for alt in [
                    Path(".codebot/state") / pattern,
                    self._state_dir.parent / pattern,
                ]:
                    if alt.exists():
                        review_path = alt
                        break
            if not review_path.exists():
                continue
            try:
                import json
                raw = review_path.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("verdict") == "REWORK":
                    findings = data.get("findings", [])
                    reviewer = data.get("reviewer", pattern.replace("_review.json", ""))
                    for finding in findings:
                        if isinstance(finding, dict):
                            feedback.append({
                                "reviewer": reviewer,
                                "file": finding.get("file", ""),
                                "severity": finding.get("severity", "medium"),
                                "category": finding.get("category", ""),
                                "description": finding.get("description", ""),
                                "recommendation": finding.get("recommendation", ""),
                                "timestamp": data.get("review_completed_at", ""),
                            })
            except Exception as e:
                logger.debug("failed to read review file %s: %s", review_path, e)
        return feedback

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
