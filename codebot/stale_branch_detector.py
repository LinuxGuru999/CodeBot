#!/usr/bin/env python3
"""Stale branch detector and cleanup mechanism.

Purpose
-------
Identifies branches that have become stale (older than threshold or linked
to abandoned tickets) and manages their cleanup through a warning-then-delete
workflow.

Why
---
ROADMAP.md §11 requires stale branch detection and cleanup. Without this,
abandoned branches accumulate in the repository, creating clutter and
potential confusion about which branches are actively being worked on.

Invariants
----------
- stdlib-only (dataclasses)
- Branches older than threshold OR linked to abandoned tickets are stale
- Cleanup follows warn-then-delete pattern with configurable warning period
- Activity refresh cancels pending cleanup
- Cleanup execution removes branch from registry
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BranchRecord:
    """Represents a branch with its metadata and activity timestamp."""
    branch_name: str
    ticket_id: str
    last_activity_ts: float
    last_commit_sha: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CleanupAction:
    """Represents a scheduled cleanup action for a branch."""
    action: str  # "none", "warn", "delete"
    branch_name: str
    scheduled_at: float = 0.0
    reason: str = ""


class StaleBranchDetector:
    """Detects and manages cleanup of stale branches.

    Usage:
        detector = StaleBranchDetector(
            stale_threshold_seconds=7*24*3600,
            warning_period_seconds=24*3600,
        )
        detector.register(BranchRecord(
            branch_name="feat/old",
            ticket_id="CB-100",
            last_activity_ts=time.time() - 8*24*3600,
            last_commit_sha="abc123",
        ))
        stale = detector.detect_stale(now=time.time())
        # Returns list of stale BranchRecords
        
        action = detector.schedule_cleanup("feat/old", now=time.time())
        # Returns CleanupAction(action="warn", ...)
        
        # After warning period:
        action = detector.schedule_cleanup("feat/old", now=time.time() + 25*3600)
        # Returns CleanupAction(action="delete", ...)
        
        result = detector.execute_cleanup("feat/old", now=time.time() + 25*3600)
        # Returns {"status": "deleted", ...}
    """

    def __init__(
        self,
        stale_threshold_seconds: int = 7 * 24 * 3600,  # 7 days default
        warning_period_seconds: int = 24 * 3600,  # 24 hours default
    ) -> None:
        self.stale_threshold_seconds = stale_threshold_seconds
        self.warning_period_seconds = warning_period_seconds
        self._branches: dict[str, BranchRecord] = {}  # branch_name -> BranchRecord
        self._warnings: dict[str, float] = {}  # branch_name -> warning_timestamp

    def register(self, record: BranchRecord) -> None:
        """Register a branch for tracking."""
        self._branches[record.branch_name] = record

    def get_branch(self, branch_name: str) -> BranchRecord | None:
        """Get a branch record by name."""
        return self._branches.get(branch_name)

    def detect_stale(
        self,
        now: float,
        abandoned_tickets: set[str] | None = None,
    ) -> list[BranchRecord]:
        """Detect branches that are stale based on age or abandoned tickets.

        Args:
            now: current timestamp
            abandoned_tickets: set of ticket IDs that are closed/abandoned

        Returns:
            List of stale BranchRecords
        """
        abandoned = abandoned_tickets or set()
        stale: list[BranchRecord] = []

        for record in self._branches.values():
            age = now - record.last_activity_ts
            is_old = age > self.stale_threshold_seconds
            is_abandoned = record.ticket_id in abandoned

            if is_old or is_abandoned:
                stale.append(record)

        return stale

    def schedule_cleanup(self, branch_name: str, now: float) -> CleanupAction:
        """Schedule a cleanup action for a stale branch.

        First call issues a warning. After warning period expires,
        subsequent calls return a delete action.

        Args:
            branch_name: name of the branch
            now: current timestamp

        Returns:
            CleanupAction indicating what should happen
        """
        record = self._branches.get(branch_name)
        if record is None:
            return CleanupAction(
                action="none",
                branch_name=branch_name,
                reason="branch not found",
            )

        # Check if branch is actually stale
        age = now - record.last_activity_ts
        if age <= self.stale_threshold_seconds:
            return CleanupAction(
                action="none",
                branch_name=branch_name,
                reason="branch not stale",
            )

        # Check if warning was already issued
        if branch_name in self._warnings:
            warning_ts = self._warnings[branch_name]
            elapsed = now - warning_ts

            if elapsed >= self.warning_period_seconds:
                # Warning period expired, schedule delete
                return CleanupAction(
                    action="delete",
                    branch_name=branch_name,
                    scheduled_at=now,
                    reason="warning period expired",
                )
            else:
                # Still in warning period
                return CleanupAction(
                    action="warn",
                    branch_name=branch_name,
                    scheduled_at=warning_ts,
                    reason="warning period active",
                )
        else:
            # First time: issue warning
            self._warnings[branch_name] = now
            return CleanupAction(
                action="warn",
                branch_name=branch_name,
                scheduled_at=now,
                reason="initial warning",
            )

    def execute_cleanup(self, branch_name: str, now: float) -> dict[str, Any]:
        """Execute cleanup by removing the branch from the registry.

        Args:
            branch_name: name of the branch to clean up
            now: current timestamp

        Returns:
            Dict with status and details
        """
        record = self._branches.get(branch_name)
        if record is None:
            return {
                "status": "not_found",
                "branch_name": branch_name,
            }

        # Check if warning period has elapsed
        if branch_name in self._warnings:
            warning_ts = self._warnings[branch_name]
            elapsed = now - warning_ts
            if elapsed < self.warning_period_seconds:
                return {
                    "status": "warning_active",
                    "branch_name": branch_name,
                    "time_remaining": self.warning_period_seconds - elapsed,
                }

        # Execute deletion
        del self._branches[branch_name]
        self._warnings.pop(branch_name, None)

        return {
            "status": "deleted",
            "branch_name": branch_name,
            "ticket_id": record.ticket_id,
            "last_commit_sha": record.last_commit_sha,
        }

    def update_activity(
        self,
        branch_name: str,
        new_ts: float,
        new_sha: str,
    ) -> None:
        """Update branch activity timestamp and commit SHA.

        This cancels any pending cleanup warnings.

        Args:
            branch_name: name of the branch
            new_ts: new activity timestamp
            new_sha: new commit SHA
        """
        record = self._branches.get(branch_name)
        if record is None:
            return

        # Create updated record
        updated = BranchRecord(
            branch_name=record.branch_name,
            ticket_id=record.ticket_id,
            last_activity_ts=new_ts,
            last_commit_sha=new_sha,
            metadata=record.metadata,
        )
        self._branches[branch_name] = updated

        # Cancel any pending warnings
        self._warnings.pop(branch_name, None)

    def list_all_branches(self) -> list[BranchRecord]:
        """Return all registered branches."""
        return list(self._branches.values())

    def summary(self) -> dict[str, Any]:
        """Return summary statistics."""
        return {
            "total_branches": len(self._branches),
            "pending_warnings": len(self._warnings),
        }
