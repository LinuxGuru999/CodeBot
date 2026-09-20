#!/usr/bin/env python3
"""Completion Commit — per-ticket scoped git commit at COMPLETE stage.

Purpose
-------
Commits exactly the files a completed ticket touched, with a message that
traces back to the ticket ID, and returns the resulting commit SHA for
ticket traceability. Called AFTER the VERIFYING -> COMPLETE transition
succeeds — never before, never at agent exit.

Why
---
The old api_runner._auto_commit ran at IMPLEMENTING exit (unverified work),
in a subprocess without adapter/paths (always fail-closed), matched
hardcoded Monitor repo names (always empty on CodeBot), and used
`git add -A` (would bundle concurrent tickets' work). This module runs in
the orchestrator/gatekeeper process where paths and the store are
available, scopes to the ticket's own files, and records the SHA.

Invariants
----------
- stdlib-only (subprocess, shlex, pathlib)
- Fail-open: any git error returns (False, "") and never raises —
  a commit failure must not undo a COMPLETE transition or crash the tick
- Scoped: only files listed in affected_modules are staged; never -A
- Idempotent: re-calling for an already-recorded SHA is a no-op decision
  left to the caller (this module just commits what's dirty)
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

logger = logging.getLogger("completion_commit")


def _run_git(repo: Path, *args: str, timeout: int = 30) -> tuple[bool, str]:
    """Run a git command in repo. Returns (success, stripped output)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return proc.returncode == 0, out
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return False, str(e)


def _resolve_repo(path: Path) -> Path | None:
    """Walk up from path to the containing git repo root, or None."""
    try:
        p = path.resolve() if path.exists() else path.absolute()
        for parent in [p] + list(p.parents):
            if (parent / ".git").exists():
                return parent
        return None
    except OSError:
        return None


def commit_ticket_files(
    workspace: Path,
    ticket_id: str,
    title: str,
    files: list[str],
    timeout: int = 60,
) -> tuple[bool, str]:
    """Stage + commit exactly `files` in workspace. Returns (ok, sha).

    - Resolves each file against workspace; skips missing files and files
      outside the repo (never commits stray absolute paths).
    - Stages per-file (`git add -- <file>`), commits once with message
      `[<ticket_id>] <title>`, returns the new HEAD SHA.
    - "Nothing to commit" (files already committed/clean) is success with
      the current HEAD SHA — the work is already in the tree.
    - Any failure returns (False, "") and logs at WARNING. No push:
      push stays batched/periodic, commit is per-ticket synchronous.
    """
    workspace = Path(workspace)
    if not ticket_id or not files:
        return False, ""

    scoped: list[str] = []
    for f in files:
        p = Path(f) if Path(f).is_absolute() else workspace / f
        try:
            rel = p.resolve().relative_to(workspace.resolve())
        except (OSError, ValueError):
            logger.warning("ticket %s: skipping out-of-tree file %s", ticket_id, f)
            continue
        if not (workspace / rel).exists():
            logger.warning("ticket %s: skipping missing file %s", ticket_id, f)
            continue
        repo = _resolve_repo(workspace / rel)
        if repo is None:
            logger.warning("ticket %s: skipping file outside git repo %s", ticket_id, f)
            continue
        scoped.append(str(rel))

    if not scoped:
        logger.warning("ticket %s: no committable files after scoping", ticket_id)
        return False, ""

    ok, status = _run_git(workspace, "status", "--short", "--", *scoped)
    if not ok:
        logger.warning("ticket %s: git status failed: %s", ticket_id, status[:200])
        return False, ""

    ok, _ = _run_git(workspace, "add", "--", *scoped)
    if not ok:
        logger.warning("ticket %s: git add failed", ticket_id)
        return False, ""

    ok, head_before = _run_git(workspace, "rev-parse", "HEAD")
    head_before = head_before.strip() if ok else ""

    msg = f"[{ticket_id}] {title[:120]}" if title else f"[{ticket_id}] completed work"
    ok, out = _run_git(
        workspace, "commit", "-m", msg,
        "--", *scoped, timeout=timeout,
    )
    if not ok:
        if "nothing to commit" in out.lower() or "nothing added to commit" in out.lower():
            ok, head = _run_git(workspace, "rev-parse", "HEAD")
            sha = head.strip() if ok and head.strip() else head_before
            if sha:
                logger.info("ticket %s: files already committed at %s", ticket_id, sha[:8])
                return True, sha
            return False, ""
        logger.warning("ticket %s: git commit failed: %s", ticket_id, out[:300])
        return False, ""

    ok, head = _run_git(workspace, "rev-parse", "HEAD")
    sha = head.strip() if ok and head.strip() else ""
    if not sha:
        logger.warning("ticket %s: commit ran but HEAD unreadable", ticket_id)
        return False, ""
    logger.info("ticket %s committed as %s (%d files)", ticket_id, sha[:8], len(scoped))
    return True, sha


def build_commit_message(ticket_id: str, title: str) -> str:
    """Format the per-ticket commit message (exported for tests)."""
    return f"[{ticket_id}] {title[:120]}" if title else f"[{ticket_id}] completed work"
