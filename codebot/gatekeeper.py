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
import os
import re
import time
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger("gatekeeper")

# Patchable aliases for tests (allow mocking via codebot.gatekeeper.*)
try:
    from codebot.ticket_dispatcher import get_ticket_store as _get_ticket_store  # type: ignore

    get_ticket_store = _get_ticket_store  # noqa: F811  patchable
except Exception:  # pragma: no cover

    def get_ticket_store():  # type: ignore
        return None

try:
    from codebot.completion_commit import (  # type: ignore
        commit_ticket_files as _commit_ticket_files,
        open_pull_request as _open_pull_request,
        auto_merge_pull_request as _auto_merge_pull_request,
        push_current_branch as _push_current_branch,
        sync_ticket_issue as _sync_ticket_issue,
    )

    commit_ticket_files = _commit_ticket_files  # noqa: F811 patchable
    open_pull_request = _open_pull_request  # noqa: F811
    auto_merge_pull_request = _auto_merge_pull_request  # noqa: F811
    push_current_branch = _push_current_branch  # noqa: F811
    sync_ticket_issue = _sync_ticket_issue  # noqa: F811
except Exception:  # pragma: no cover
    commit_ticket_files = None  # type: ignore
    open_pull_request = None  # type: ignore
    auto_merge_pull_request = None  # type: ignore
    push_current_branch = None  # type: ignore
    sync_ticket_issue = None  # type: ignore

try:
    from codebot.coverage_runner import run_coverage as _run_coverage  # type: ignore

    run_coverage = _run_coverage  # noqa: F811 patchable via codebot.gatekeeper.run_coverage
except Exception:  # pragma: no cover

    def run_coverage(*args, **kwargs):  # type: ignore
        from codebot.coverage_runner import CoverageReport

        return CoverageReport(0, 0, 0.0, {}, 0.0, -1, error="run_coverage unavailable")


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

MAX_REWORK_ATTEMPTS = 5


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
        try:
            # Input validation: prevent path traversal in ticket_id and changed_files
            # Security-relevant validation failures return FAIL (fail-closed) rather than REWORK
            if not isinstance(ticket_id, str) or not re.match(r'^[A-Za-z0-9_-]+$', ticket_id):
                logger.error("verify_ticket rejected: invalid ticket_id format: %r", ticket_id)
                validation_result = {
                    "ticket_id": ticket_id,
                    "decision": "FAIL",
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
                    self._transition_ticket(ticket_id, "FAIL", [], store)
                except Exception:
                    logger.exception("failed to transition ticket %s to FAIL after validation failure", ticket_id)
                return validation_result
            for cf in changed_files:
                if not isinstance(cf, str):
                    logger.error("verify_ticket rejected: invalid changed_file path (not string): %r", cf)
                    validation_result = {
                        "ticket_id": ticket_id,
                        "decision": "FAIL",
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
                        self._transition_ticket(ticket_id, "FAIL", [], store)
                    except Exception:
                        logger.exception("failed to transition ticket %s to FAIL after validation failure", ticket_id)
                    return validation_result
                # Pre-validate path format to prevent obvious traversal
                if not re.match(r'^[A-Za-z0-9_./-]+$', cf) or '..' in cf:
                    logger.error("verify_ticket rejected: changed_file path format invalid: %r", cf)
                    validation_result = {
                        "ticket_id": ticket_id,
                        "decision": "FAIL",
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
                        self._transition_ticket(ticket_id, "FAIL", [], store)
                    except Exception:
                        logger.exception("failed to transition ticket %s to FAIL after validation failure", ticket_id)
                    return validation_result

                # Reject absolute paths explicitly (defense-in-depth)
                if os.path.isabs(cf):
                    logger.error("verify_ticket rejected: changed_file path is absolute: %r", cf)
                    validation_result = {
                        "ticket_id": ticket_id,
                        "decision": "FAIL",
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
                        self._transition_ticket(ticket_id, "FAIL", [], store)
                    except Exception:
                        logger.exception("failed to transition ticket %s to FAIL after validation failure", ticket_id)
                    return validation_result

                # Symlink-component walk: reject if any component from workspace down to cf is a symlink.
                # This prevents TOCTOU attacks where an attacker creates a symlink between validation and use.
                # We use lstat (via Path.is_symlink) which does NOT follow symlinks.
                candidate_path = self._workspace / cf
                # Walk from workspace down through each parent component to candidate
                # Build the list of components to check: workspace itself is trusted, so check children
                parts_to_check: list[Path] = []
                current = candidate_path
                while current != self._workspace:
                    parts_to_check.append(current)
                    current = current.parent
                # Check each component (from deepest to shallowest, but order doesn't matter for rejection)
                for part in parts_to_check:
                    try:
                        if part.is_symlink():
                            logger.error("verify_ticket rejected: changed_file path contains symlink component: %r (symlink: %s)", cf, part)
                            validation_result = {
                                "ticket_id": ticket_id,
                                "decision": "FAIL",
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
                                self._transition_ticket(ticket_id, "FAIL", [], store)
                            except Exception:
                                logger.exception("failed to transition ticket %s to FAIL after validation failure", ticket_id)
                            return validation_result
                    except OSError:
                        # ENOENT or other OS errors: treat as non-symlink and continue.
                        # A nonexistent intermediate path is OK (new file being created).
                        continue

                # Validate path does not escape workspace
                try:
                    resolved_path = (self._workspace / cf).resolve()
                    if not resolved_path.is_relative_to(self._workspace.resolve()):
                        logger.error("verify_ticket rejected: changed_file path escapes workspace: %r", cf)
                        validation_result = {
                            "ticket_id": ticket_id,
                            "decision": "FAIL",
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
                            self._transition_ticket(ticket_id, "FAIL", [], store)
                        except Exception:
                            logger.exception("failed to transition ticket %s to FAIL after validation failure", ticket_id)
                        return validation_result
                except Exception:
                    logger.error("verify_ticket rejected: invalid changed_file path format: %r", cf)
                    validation_result = {
                        "ticket_id": ticket_id,
                        "decision": "FAIL",
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
                        self._transition_ticket(ticket_id, "FAIL", [], store)
                    except Exception:
                        logger.exception("failed to transition ticket %s to FAIL after validation failure", ticket_id)
                    return validation_result
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
            # For COMPLETE, verify artifact provenance BEFORE transitioning
            # to avoid leaving ticket in COMPLETE without commit/PR.
            # Commits are skipped when there are no files to commit — the
            # deterministic gates already passed, so there is nothing to
            # provenance-check. Running git subprocesses here blocks the
            # orchestrator tick and causes the 10-tick freeze.
            if decision == "COMPLETE":
                ticket_for_commit = None
                try:
                    resolved = store if store is not None else None
                    if resolved is None:
                        from codebot.ticket_dispatcher import get_ticket_store
                        resolved = get_ticket_store()
                    ticket_for_commit = resolved.get(ticket_id) if resolved else None
                except Exception:
                    pass
                files_to_commit = list(getattr(ticket_for_commit, "affected_modules", None) or []) if ticket_for_commit else list(changed_files or [])
                if not files_to_commit:
                    logger.info("ticket %s: no files to commit, skipping artifact provenance", ticket_id)
                else:
                    commit_ok = self._commit_completed_ticket(ticket_id)
                    if not commit_ok:
                        logger.error("ticket %s: artifact provenance failed before COMPLETE transition", ticket_id)
                        commit_failure_result = {
                            "ticket_id": ticket_id,
                            "decision": "REWORK",
                            "passed": False,
                            "reason": "artifact_provenance_failed",
                            "failed_gates": failed_gates,
                            "total_gates": len(evaluations),
                            "rework_count": rework_count,
                            "evolve_prompt": rework_count >= MAX_REWORK_ATTEMPTS,
                            "timestamp": time.time(),
                        }
                        self._log_decision(commit_failure_result)
                        try:
                            self._transition_ticket(ticket_id, "REWORK", failed_gates, store)
                        except Exception:
                            logger.exception("failed to transition ticket %s to REWORK after commit failure", ticket_id)
                        return commit_failure_result

            self._log_decision(result)
            transition_ok = self._transition_ticket(ticket_id, decision, failed_gates, store)
            if not transition_ok:
                logger.error("ticket %s: state transition failed for decision %s", ticket_id, decision)
                # Construct a fresh result dict for atomicity instead of mutating existing one
                transition_failure_result = {
                    "ticket_id": ticket_id,
                    "decision": "REWORK",
                    "passed": False,
                    "reason": f"state_transition_failed: {reason}",
                    "failed_gates": failed_gates,
                    "total_gates": len(evaluations),
                    "rework_count": rework_count,
                    "evolve_prompt": rework_count >= MAX_REWORK_ATTEMPTS,
                    "timestamp": time.time(),
                }
                self._log_decision(transition_failure_result)
                # Attempt to transition to REWORK instead
                try:
                    self._transition_ticket(ticket_id, "REWORK", failed_gates, store)
                except Exception:
                    logger.exception("failed to fallback-transition ticket %s to REWORK", ticket_id)
                return transition_failure_result

            return result
        except Exception as e:
            # Log full exception internally at DEBUG level only; never leak to gate_results.jsonl
            logger.debug("verify_ticket internal error for %s: %s", ticket_id, e, exc_info=True)
            logger.error("verify_ticket failed for %s: verification_error", ticket_id)
            decision = "FAIL"
            # SECURITY: Sanitize exception to prevent secret leakage.
            # Never include str(e) or repr(e), which may contain file paths,
            # tokens, or internal state. Full details are logged at DEBUG
            # level above for diagnostics.
            reason_msg = "verification_error"
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
            # Note: decision remains "FAIL" in the result dict per acceptance criteria,
            # but ticket state must be REWORK since FAIL is not a valid TicketState.
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
        from codebot.quality_gate import GateEvaluation, GateStatus

        started_at = time.monotonic()
        report = run_coverage(self._workspace, test_dirs=[test_dirs])
        duration_ms = (time.monotonic() - started_at) * 1000
        if report.error:
            return GateEvaluation(
                "coverage", GateStatus.ERROR, "python3 -m pytest --cov", "", duration_ms,
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
                "coverage", GateStatus.FAIL, "python3 -m pytest --cov", output, duration_ms,
                False, True, "affected modules require 100% coverage",
            )
        return GateEvaluation(
            "coverage", GateStatus.PASS, "python3 -m pytest --cov", "", duration_ms, True,
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
                    rework_count = getattr(ticket, "rework_count", 0)
                    if rework_count >= MAX_REWORK_ATTEMPTS:
                        store.transition(ticket_id, TicketState.BLOCKED, reviewer_feedback)
                        logger.warning("ticket %s exceeded max rework attempts (%d) — transitioned to BLOCKED", ticket_id, MAX_REWORK_ATTEMPTS)
                        return True
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
            logger.debug("transition failed for %s: %s", ticket_id, e, exc_info=True)
            logger.error("failed to transition ticket %s: transition_error", ticket_id)
            return False

    def _commit_completed_ticket(self, ticket_id: str) -> bool:
        """Commit the ticket's files on cb/<ticket>, open PR, record SHA+URL. Returns True on success."""
        try:
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

            # Use module-level aliases for testability (patchable via codebot.gatekeeper.*)
            if commit_ticket_files is None:
                logger.error("ticket %s: commit_ticket_files not available", ticket_id)
                return False
            ok, sha = commit_ticket_files(
                self._workspace, ticket_id, title, files, branch=True,
            )
            if ok and sha:
                pr_url = getattr(ticket, "pr_url", "")
                if not pr_url and _push_enabled_for_prs():
                    if open_pull_request is not None:
                        pok, pr_url = open_pull_request(
                            self._workspace, ticket_id, title, sha,
                        )
                        if pok and pr_url and auto_merge_pull_request is not None:
                            auto_merge_pull_request(pr_url)
                        else:
                            pr_url = ""
                    else:
                        pr_url = ""
                # Push and sync BEFORE recording commit to ensure artifact provenance.
                # If push/sync fails, we don't record the commit, keeping state consistent.
                if push_current_branch is None or sync_ticket_issue is None:
                    logger.error("ticket %s: push_current_branch or sync_ticket_issue not available", ticket_id)
                    return False
                push_ok, push_msg = push_current_branch(self._workspace)
                sync_ok, sync_msg = sync_ticket_issue(ticket_id, title, sha, "COMPLETE")
                if not push_ok or not sync_ok:
                    logger.error(
                        "ticket %s: push/sync failed (push=%s/%s, sync=%s/%s); skipping record_commit",
                        ticket_id, push_ok, push_msg, sync_ok, sync_msg,
                    )
                    return False
                # Only record commit after all external operations succeed
                store.record_commit(ticket_id, sha, pr_url)

                # Auto-create documentation ticket if needed
                self._create_documentation_ticket_if_needed(ticket_id, store)
                return True
            else:
                logger.error("ticket %s: commit_ticket_files failed", ticket_id)
                return False

        except Exception as e:
            logger.debug("completion commit failed for %s: %s", ticket_id, e, exc_info=True)
            logger.error("ticket %s: completion commit failed: verification_error", ticket_id)
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
        import fcntl
        import os

        line = json.dumps(result) + "\n"
        data = line.encode("utf-8")
        lock_path = self._log_path.with_suffix(self._log_path.suffix + ".lock")
        tmp_path = self._log_path.with_suffix(f"{self._log_path.suffix}.{os.getpid()}.tmp")

        try:
            # Acquire exclusive lock via a dedicated lock file
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)

                # Read existing content to preserve history
                existing_data = b""
                if self._log_path.exists():
                    try:
                        existing_data = self._log_path.read_bytes()
                    except OSError:
                        pass

                # Prepare full payload
                payload = existing_data + data

                # Write to tmp file atomically
                tmp_fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
                try:
                    offset = 0
                    total = len(payload)
                    while offset < total:
                        written = os.write(tmp_fd, payload[offset:])
                        if written == 0:
                            raise OSError("write returned 0 bytes")
                        offset += written
                    os.fsync(tmp_fd)
                finally:
                    os.close(tmp_fd)

                # Atomic rename
                os.replace(str(tmp_path), str(self._log_path))
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except Exception:
                    pass
                os.close(lock_fd)
        except OSError:
            logger.warning("Failed to log decision to %s", self._log_path)
            # Clean up tmp file if it exists
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

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
