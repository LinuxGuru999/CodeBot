#!/usr/bin/env python3
"""Central quality gatekeeper for CodeBot ticket completion.

Purpose
-------
The sole authority that transitions tickets from REVIEW to COMPLETE.
Runs deterministic quality gates (build, tests) and makes the final
determination based on gate results.

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
- Every COMPLETE transition is logged with gate evidence
- Gate failures trigger REWORK, not silent bypass
- Maximum 3 rework attempts before cycling back to REWORK for QA review
- Reviewer verdicts are trusted — no re-checking of findings or checklists
"""

from __future__ import annotations

import json
import logging
import re
import time
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger("gatekeeper")


class Decision(str, Enum):
    """Placeholder stub decision for Gatekeeper.verify_ticket.

    Scoped as a stub type only; Gatekeeper.verify_ticket continues to
    return its established COMPLETE/REWORK dict shape (see GateDecision
    in codebot/review_types.py for the canonical verdict type).
    """

    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"


def _push_enabled_for_prs() -> bool:
    """PR flow shares the push gate: off when GITHUB_DRY_RUN is on."""
    try:
        from codebot.completion_commit import _push_enabled
        return _push_enabled()
    except Exception:
        return False

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
        self._log_path = self._state_dir / "gate_results.jsonl"

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
        store: Any | None = None,
    ) -> dict[str, Any]:
        """Run deterministic quality gates and transition ticket.

        Only runs build/test gates. Reviewer verdicts are trusted —
        no re-checking of findings, checklists, or tampering detection.
        """
        # Input validation: prevent path traversal in ticket_id and changed_files
        if not re.match(r'^[A-Za-z0-9_-]+$', ticket_id):
            logger.error("verify_ticket rejected: invalid ticket_id format: %r", ticket_id)
            validation_result = {
                "ticket_id": ticket_id,
                "decision": "REWORK",
                "passed": False,
                "reason": "verification_error: invalid_ticket_id_format",
                "failed_gates": [],
                "total_gates": 0,
                "rework_count": rework_count,
                "evolve_prompt": False,
                "timestamp": time.time(),
            }
            try:
                self._log_decision(validation_result)
            except OSError:
                pass
            try:
                self._transition_ticket(ticket_id, "REWORK", [], store)
            except Exception:
                logger.exception("failed to transition ticket %s to REWORK after validation failure", ticket_id)
            return validation_result
        for cf in changed_files:
            if not re.match(r'^[A-Za-z0-9_./-]+$', cf):
                logger.error("verify_ticket rejected: invalid changed_file path: %r", cf)
                validation_result = {
                    "ticket_id": ticket_id,
                    "decision": "REWORK",
                    "passed": False,
                    "reason": "verification_error: invalid_changed_file_path",
                    "failed_gates": [],
                    "total_gates": 0,
                    "rework_count": rework_count,
                    "evolve_prompt": False,
                    "timestamp": time.time(),
                }
                try:
                    self._log_decision(validation_result)
                except OSError:
                    pass
                try:
                    self._transition_ticket(ticket_id, "REWORK", [], store)
                except Exception:
                    logger.exception("failed to transition ticket %s to REWORK after validation failure", ticket_id)
                return validation_result
        try:
            from codebot.quality_gate import (
                load_policy,
                run_quality_gates_with_cache,
                record_gate_results,
            )

            policy = load_policy(self._policy_path)
            ticket_revision = 0.0
            resolved_store = store
            if resolved_store is None:
                try:
                    from codebot.ticket_dispatcher import get_ticket_store
                    resolved_store = get_ticket_store()
                except Exception:
                    resolved_store = None
            if resolved_store is not None:
                ticket = resolved_store.get(ticket_id)
                if ticket is not None:
                    ticket_revision = getattr(ticket, "updated_at", 0.0)
            passed, evaluations = run_quality_gates_with_cache(
                policy=policy,
                workspace=self._workspace,
                state_dir=self._state_dir,
                ticket_id=ticket_id,
                ticket_class=ticket_class,
                changed_files=changed_files,
                test_dirs=test_dirs,
                conditions=conditions,
                ticket_revision=ticket_revision,
            )
            coverage_evaluation = self._coverage_evaluation(changed_files, test_dirs)
            if coverage_evaluation is not None:
                evaluations.append(coverage_evaluation)
                passed = passed and coverage_evaluation.passed

            record_gate_results(self._state_dir, ticket_id, passed, evaluations)
            if resolved_store is not None:
                resolved_store.update_gate_approval(ticket_id, passed)
            self._write_verification_packet(ticket_id, ticket_revision, changed_files, evaluations)
            try:
                from codebot.lifecycle_packet import LifecyclePacketStore
                LifecyclePacketStore(self._state_dir).append_evidence(
                    ticket_id,
                    ticket_revision,
                    "verification",
                    {"passed": passed, "gates": [evaluation.to_dict() for evaluation in evaluations]},
                )
            except OSError:
                pass

            failed_gates = [ev.gate_name for ev in evaluations if not ev.passed]

            if not passed:
                decision = "REWORK"
                reason = "deterministic gates failed"
            else:
                decision = "COMPLETE"
                reason = "all gates passed"

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
                "rework_count": rework_count,
                "evolve_prompt": decision != "COMPLETE" and rework_count >= MAX_REWORK_ATTEMPTS,
                "timestamp": time.time(),
            }

            try:
                from codebot.review_metrics import record_gatekeeper_metric
                record_gatekeeper_metric(
                    ticket_id=ticket_id,
                    decision=decision,
                    gates_passed=passed,
                    blocking_findings=0,
                    review_count=0,
                    rework_count=rework_count,
                )
            except OSError:
                pass
            self._log_decision(result)
            transition_ok = self._transition_ticket(ticket_id, decision, failed_gates, store)
            if not transition_ok:
                logger.error("ticket %s: state transition failed for decision %s", ticket_id, decision)
                result["decision"] = "REWORK"
                result["passed"] = False
                result["reason"] = f"state_transition_failed: {result['reason']}"
                # Attempt to transition to REWORK instead
                try:
                    self._transition_ticket(ticket_id, "REWORK", failed_gates, store)
                except Exception:
                    logger.exception("failed to fallback-transition ticket %s to REWORK", ticket_id)
                return result

            # If COMPLETE, also verify commit succeeded (artifact provenance)
            if decision == "COMPLETE":
                commit_ok = self._commit_completed_ticket(ticket_id)
                if not commit_ok:
                    logger.error("ticket %s: artifact provenance failed after COMPLETE transition", ticket_id)
                    result["decision"] = "REWORK"
                    result["passed"] = False
                    result["reason"] = "artifact_provenance_failed"
                    # Transition back to REWORK since commit failed
                    try:
                        self._transition_ticket(ticket_id, "REWORK", failed_gates, store)
                    except Exception:
                        logger.exception("failed to rollback ticket %s to REWORK after commit failure", ticket_id)
                    return result

            return result
        except Exception as e:
            logger.error("verify_ticket failed for %s: %s", ticket_id, e, exc_info=True)
            decision = "REWORK"
            reason_msg = f"verification_error: {str(e)}"
            result = {
                "ticket_id": ticket_id,
                "decision": decision,
                "passed": False,
                "reason": reason_msg,
                "failed_gates": [],
                "total_gates": 0,
                "rework_count": rework_count,
                "evolve_prompt": False,
                "timestamp": time.time(),
            }
            try:
                self._log_decision(result)
            except OSError:
                pass
            # Transition to REWORK so the ticket is not orphaned when verification crashes.
            try:
                self._transition_ticket(ticket_id, "REWORK", [], store)
            except Exception:
                logger.exception("failed to transition ticket %s to REWORK after error", ticket_id)
            return result

    def _coverage_evaluation(self, changed_files: list[str], test_dirs: str):
        from codebot.review_config import load_review_config

        policy = load_review_config(self._workspace / ".codebot" / "review.yaml")
        def normalize_path(path: str) -> str:
            candidate = Path(path)
            if candidate.is_absolute():
                try:
                    return candidate.relative_to(self._workspace).as_posix()
                except ValueError:
                    return candidate.as_posix()
            return candidate.as_posix()

        affected_modules = [
            normalize_path(path)
            for path in changed_files
            if path.endswith(".py") and not Path(path).parts[0] == "tests"
        ]
        if not policy.require_full_coverage or not affected_modules:
            return None

        from codebot.coverage_runner import run_coverage
        from codebot.quality_gate import GateEvaluation, GateResult

        started_at = time.monotonic()
        report = run_coverage(self._workspace, test_dirs=[test_dirs])
        duration_ms = (time.monotonic() - started_at) * 1000
        if report.error:
            return GateEvaluation(
                "coverage", GateResult.ERROR, "python3 -m pytest --cov", "", duration_ms,
                False, True, report.error,
            )

        modules = {normalize_path(path): coverage for path, coverage in report.modules.items()}
        incomplete = [
            path
            for path in affected_modules
            if path not in modules or modules[path].coverage_pct < 100.0
        ]
        if incomplete:
            output = ", ".join(
                f"{path}={modules[path].coverage_pct:.1f}%" if path in modules else f"{path}=missing"
                for path in incomplete
            )
            return GateEvaluation(
                "coverage", GateResult.FAIL, "python3 -m pytest --cov", output, duration_ms,
                False, True, "affected modules require 100% coverage",
            )
        return GateEvaluation(
            "coverage", GateResult.PASS, "python3 -m pytest --cov", "", duration_ms, True,
        )

    def _write_verification_packet(
        self, ticket_id: str, ticket_revision: float, changed_files: list[str],
        evaluations: list[Any],
    ) -> None:
        directory = self._state_dir / "verification_packets"
        directory.mkdir(parents=True, exist_ok=True)
        packet = {
            "ticket_id": ticket_id,
            "ticket_revision": ticket_revision,
            "changed_files": changed_files,
            "gates": [evaluation.to_dict() for evaluation in evaluations],
            "generated_at": time.time(),
        }
        path = directory / f"{ticket_id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(packet, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _transition_ticket(
        self, ticket_id: str, decision: str, failed_gates: list[str] | None = None,
        store: Any | None = None,
    ) -> bool:
        """Transition ticket state. Returns True on success, False on failure."""
        try:
            from codebot.ticket_engine import TicketState
            from codebot.ticket_dispatcher import get_ticket_store

            store = store if store is not None else get_ticket_store()
            if store is None:
                logger.warning("TicketStore unavailable for ticket %s", ticket_id)
                return False
            ticket = store.get(ticket_id)

            if ticket is None:
                logger.warning("ticket %s not found in store", ticket_id)
                return False

            if decision == "COMPLETE":
                if ticket.state == TicketState.REVIEW:
                    store.transition(ticket_id, TicketState.COMPLETE)
                    logger.info("ticket %s transitioned to COMPLETE", ticket_id)
                    return True
                else:
                    logger.info(
                        "ticket %s in state %s — skipping COMPLETE transition",
                        ticket_id, ticket.state.value,
                    )
                    return False
            elif decision in ("REWORK", "FAIL"):
                if ticket.state == TicketState.REVIEW:
                    reviewer_feedback = self._collect_reviewer_feedback(ticket_id, failed_gates)
                    store.transition(ticket_id, TicketState.REWORK, reviewer_feedback)
                    logger.info("ticket %s transitioned to REWORK with %d feedback items", ticket_id, len(reviewer_feedback))
                    return True
                else:
                    logger.info(
                        "ticket %s in state %s — skipping REWORK transition",
                        ticket_id, ticket.state.value,
                    )
                    return False
            return False
        except Exception as e:
            logger.error("failed to transition ticket %s: %s", ticket_id, e, exc_info=True)
            return False

    def _commit_completed_ticket(self, ticket_id: str) -> bool:
        """Commit the ticket's files on cb/<ticket>, open PR, record SHA+URL. Returns True on success."""
        try:
            from codebot.ticket_dispatcher import get_ticket_store
            from codebot.completion_commit import (
                commit_ticket_files, open_pull_request, auto_merge_pull_request,
                push_current_branch, sync_ticket_issue,
            )

            store = get_ticket_store()
            if store is None:
                logger.warning("TicketStore unavailable for ticket %s commit", ticket_id)
                return False
            ticket = store.get(ticket_id)
            if ticket is None or getattr(ticket, "commit_sha", ""):
                return True  # Already committed or no ticket
            files = list(getattr(ticket, "affected_modules", None) or [])
            if not files:
                return True  # No files to commit
            title = getattr(ticket, "title", "")
            ok, sha = commit_ticket_files(
                self._workspace, ticket_id, title, files, branch=True,
            )
            if ok and sha:
                pr_url = getattr(ticket, "pr_url", "")
                if not pr_url and _push_enabled_for_prs():
                    pok, pr_url = open_pull_request(
                        self._workspace, ticket_id, title, sha,
                    )
                    if pok and pr_url:
                        auto_merge_pull_request(pr_url)
                    else:
                        pr_url = ""
                store.record_commit(ticket_id, sha, pr_url)
                push_current_branch(self._workspace)
                sync_ticket_issue(ticket_id, title, sha, "COMPLETE")

                # Auto-create documentation ticket if needed
                self._create_documentation_ticket_if_needed(ticket_id, store)
                return True
            else:
                logger.error("ticket %s: commit_ticket_files failed", ticket_id)
                return False

        except Exception as e:
            logger.error("ticket %s: completion commit failed with exception: %s", ticket_id, e, exc_info=True)
            return False

    def _create_documentation_ticket_if_needed(self, ticket_id: str, store: Any) -> None:
        """Create a documentation ticket if documentation wasn't updated."""
        try:
            from codebot.documentation_ticket_generator import create_documentation_ticket

            ticket = store.get(ticket_id)
            if ticket is None:
                return

            create_documentation_ticket(
                ticket=ticket,
                store=store,
                workspace=self._workspace,
            )

        except Exception as e:
            logger.debug("ticket %s: documentation ticket creation skipped: %s", ticket_id, e)

    def _collect_reviewer_feedback(self, ticket_id: str, failed_gates: list[str] | None = None) -> list[dict]:
        """Collect feedback for REWORK transitions.

        Since we trust reviewer verdicts and only gate on deterministic checks,
        feedback is just the list of failed gates.
        """
        feedback: list[dict] = []
        if failed_gates:
            for gate_name in failed_gates:
                feedback.append({
                    "reviewer": "gatekeeper",
                    "file": "",
                    "severity": "high",
                    "category": "quality_gate",
                    "description": f"Quality gate '{gate_name}' failed",
                    "recommendation": f"Fix the issue causing '{gate_name}' to fail",
                    "evidence": "",
                    "reproduction": "",
                    "timestamp": time.time(),
                })
        return feedback

    def _log_decision(self, result: dict[str, Any]) -> None:
        line = json.dumps(result) + "\n"
        import fcntl
        import os

        fd = os.open(
            str(self._log_path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
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
