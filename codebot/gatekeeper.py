#!/usr/bin/env python3
"""Central quality gatekeeper for CodeBot ticket completion.

Purpose
-------
The sole authority that transitions tickets from VERIFYING to COMPLETE.
Implementers and reviewers produce evidence; the gatekeeper independently
evaluates it against the quality gate policy, structured review findings,
and completion-evidence requirements, then makes the final determination.

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
- Unresolved BLOCKER/CRITICAL/MAJOR findings from any reviewer block COMPLETE
- Changes to review/gatekeeper config files trigger elevated scrutiny
  (gate-tampering detection)
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from codebot.review_types import (
    CompletionEvidence,
    FindingSeverity,
    ReviewDecision,
    StructuredFinding,
    DEFAULT_BLOCKING_SEVERITIES,
)
from codebot.review_config import get_review_config

logger = logging.getLogger("gatekeeper")

MAX_REWORK_ATTEMPTS = 3

_REVIEW_FILE_PATTERNS: tuple[str, ...] = (
    "correctness_review.json",
    "security_review.json",
    "architecture_review.json",
    "performance_review.json",
    "simplicity_review.json",
    "test_review.json",
    "documentation_review.json",
    "adversarial_review.json",
)

# Gate-tampering detection: files whose modification could weaken CodeBot's
# own quality controls. Changes to these receive elevated scrutiny because
# an agent could otherwise improve its completion rate by loosening gates.
_GATE_TAMPER_PATTERNS: tuple[str, ...] = (
    "review.yaml",
    "quality_gates.yaml",
    "codebot/gatekeeper.py",
    "codebot/quality_gate.py",
    "codebot/review_config.py",
    "codebot/review_types.py",
    "codebot/review_metrics.py",
)


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

    # ------------------------------------------------------------------
    # Gate-tampering detection
    # ------------------------------------------------------------------

    def _detect_gate_tampering(self, changed_files: list[str]) -> list[str]:
        """Return changed files that touch review/gatekeeper configuration.

        Any non-empty result means the ticket modifies CodeBot's own quality
        controls and must receive elevated scrutiny.
        """
        flagged: list[str] = []
        for f in changed_files:
            for pat in _GATE_TAMPER_PATTERNS:
                if pat in f:
                    flagged.append(f)
                    break
        return flagged

    # ------------------------------------------------------------------
    # Structured review loading
    # ------------------------------------------------------------------

    def _load_review_decisions(self, ticket_id: str) -> list[ReviewDecision]:
        """Load all structured *_review.json verdicts for a ticket."""
        decisions: list[ReviewDecision] = []
        for pattern in _REVIEW_FILE_PATTERNS:
            review_path = self._state_dir / pattern
            if not review_path.exists():
                for alt in (
                    Path(".codebot/state") / pattern,
                    self._state_dir.parent / pattern,
                ):
                    if alt.exists():
                        review_path = alt
                        break
            if not review_path.exists():
                continue
            try:
                data = json.loads(review_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                if data.get("ticket_id", "") != ticket_id:
                    continue
                decisions.append(ReviewDecision.from_dict(data))
            except (json.JSONDecodeError, OSError, ValueError, KeyError):
                continue
        return decisions

    # ------------------------------------------------------------------
    # Completion evidence
    # ------------------------------------------------------------------

    def _build_completion_evidence(
        self,
        ticket_id: str,
        gates_passed: bool,
        evaluations: list,
        review_decisions: list[ReviewDecision],
        all_findings: list[StructuredFinding],
        tampered_files: list[str],
    ) -> CompletionEvidence:
        """Assemble the machine-readable evidence artifact for a decision."""
        finding_counts: dict[str, int] = {}
        for f in all_findings:
            if not f.resolved:
                finding_counts[f.severity.value] = (
                    finding_counts.get(f.severity.value, 0) + 1
                )

        tests_passed = sum(1 for ev in evaluations if ev.passed)
        tests_failed = sum(1 for ev in evaluations if not ev.passed)

        checklists_complete = (
            all(rd.checklist.is_complete() for rd in review_decisions)
            if review_decisions
            else False
        )

        requirement_verified = (
            any(
                rd.checklist.result_for("requirement_satisfied").value == "PASS"
                for rd in review_decisions
            )
            if review_decisions
            else False
        )

        acceptance_failed = sum(
            1
            for rd in review_decisions
            if rd.checklist.result_for("acceptance_criteria_satisfied").value == "FAIL"
        )
        acceptance_unknown = sum(
            1
            for rd in review_decisions
            if rd.checklist.result_for("acceptance_criteria_satisfied").value == "UNKNOWN"
        )

        return CompletionEvidence(
            requirement_verified=requirement_verified,
            acceptance_passed=len(review_decisions) - acceptance_failed - acceptance_unknown,
            acceptance_failed=acceptance_failed,
            acceptance_unknown=acceptance_unknown,
            tests_executed=len(evaluations),
            tests_passed=tests_passed,
            tests_failed=tests_failed,
            new_tests_added=0,
            finding_counts=finding_counts,
            build_verified=gates_passed,
            security_checked=any(
                rd.reviewer == "security_reviewer" for rd in review_decisions
            ),
            regression_checked=any(
                rd.checklist.result_for("no_obvious_regressions").value == "PASS"
                for rd in review_decisions
            ),
            deterministic_gates_passed=gates_passed,
            checklist_complete=checklists_complete,
            completion_confidence=0.0,
        )

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

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
            run_quality_gates_with_cache,
            record_gate_results,
        )

        cfg = get_review_config()
        blocking = cfg.blocking_severities

        policy = load_policy(self._policy_path)
        passed, evaluations = run_quality_gates_with_cache(
            policy=policy,
            workspace=self._workspace,
            state_dir=self._state_dir,
            ticket_id=ticket_id,
            ticket_class=ticket_class,
            changed_files=changed_files,
            test_dirs=test_dirs,
            conditions=conditions,
        )

        record_gate_results(self._state_dir, ticket_id, passed, evaluations)

        # Independent evidence collection: do not trust reviewer verdicts alone.
        review_decisions = self._load_review_decisions(ticket_id)
        all_findings: list[StructuredFinding] = []
        for rd in review_decisions:
            all_findings.extend(rd.findings)

        unresolved_blocking = [f for f in all_findings if f.is_blocking(blocking)]

        tampered_files = self._detect_gate_tampering(changed_files)
        if tampered_files:
            logger.warning(
                "ticket %s modifies gate/review config files: %s — elevated scrutiny",
                ticket_id, tampered_files,
            )

        evidence = self._build_completion_evidence(
            ticket_id=ticket_id,
            gates_passed=passed,
            evaluations=evaluations,
            review_decisions=review_decisions,
            all_findings=all_findings,
            tampered_files=tampered_files,
        )

        failed_gates = [ev.gate_name for ev in evaluations if not ev.passed]

        if not passed:
            decision = "REWORK"
            reason = "deterministic gates failed"
        elif unresolved_blocking:
            decision = "REWORK"
            reason = f"{len(unresolved_blocking)} unresolved blocking finding(s)"
        elif tampered_files:
            decision = "REWORK"
            reason = f"gate/review config modified: {tampered_files}"
        elif evidence.has_missing_evidence() and cfg.require_checklist_complete:
            decision = "REWORK"
            reason = "incomplete completion evidence"
        else:
            decision = "COMPLETE"
            reason = "all gates passed, no blocking findings, evidence complete"

        if decision == "COMPLETE":
            logger.info("ticket %s PASSED: %s", ticket_id, reason)
        else:
            if rework_count >= MAX_REWORK_ATTEMPTS:
                logger.warning(
                    "ticket %s FAILED %d attempts (%s) — triggering prompt evolution + REWORK",
                    ticket_id, rework_count + 1, reason,
                )
            else:
                logger.info(
                    "ticket %s FAILED (attempt %d): %s — sending to REWORK",
                    ticket_id, rework_count + 1, reason,
                )

        result = {
            "ticket_id": ticket_id,
            "decision": decision,
            "passed": passed,
            "reason": reason,
            "failed_gates": failed_gates,
            "total_gates": len(evaluations),
            "review_count": len(review_decisions),
            "total_findings": len(all_findings),
            "blocking_findings": len(unresolved_blocking),
            "tampered_files": tampered_files,
            "rework_count": rework_count,
            "evolve_prompt": decision != "COMPLETE" and rework_count >= MAX_REWORK_ATTEMPTS,
            "evidence": evidence.to_dict(),
            "timestamp": time.time(),
        }

        self._log_decision(result)
        self._transition_ticket(ticket_id, decision, failed_gates)
        return result

    def _transition_ticket(self, ticket_id: str, decision: str, failed_gates: list[str] | None = None) -> None:
        try:
            from codebot.ticket_engine import TicketState
            from codebot.ticket_dispatcher import get_ticket_store

            store = get_ticket_store()
            if store is None:
                logger.warning("TicketStore unavailable for ticket %s", ticket_id)
                return
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
        """Collect structured feedback from reviewer verdict files.

        Reads both structured findings (with severity/evidence fields) and
        legacy description/recommendation fields for backward compatibility.
        """
        feedback: list[dict] = []
        for pattern in _REVIEW_FILE_PATTERNS:
            review_path = self._state_dir / pattern
            if not review_path.exists():
                for alt in (
                    Path(".codebot/state") / pattern,
                    self._state_dir.parent / pattern,
                ):
                    if alt.exists():
                        review_path = alt
                        break
            if not review_path.exists():
                continue
            try:
                data = json.loads(review_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                if data.get("verdict") != "REWORK":
                    continue
                findings = data.get("findings", [])
                reviewer = data.get("reviewer", pattern.replace("_review.json", ""))
                for finding in findings:
                    if not isinstance(finding, dict):
                        continue
                    feedback.append({
                        "reviewer": reviewer,
                        "file": finding.get("file", ""),
                        "severity": finding.get("severity", "medium"),
                        "category": finding.get("category", ""),
                        "description": finding.get("description", ""),
                        "recommendation": finding.get("recommendation", ""),
                        "evidence": finding.get("evidence", ""),
                        "reproduction": finding.get("reproduction", ""),
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
