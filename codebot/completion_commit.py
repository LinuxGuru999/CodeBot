#!/usr/bin/env python3
"""Completion Commit — per-ticket scoped git commit at COMPLETE stage.

Purpose
-------
Commits exactly the files a completed ticket touched, with a message that
traces back to the ticket ID, and returns the resulting commit SHA for
ticket traceability. Called AFTER the REVIEW -> COMPLETE transition
succeeds — never before, never at agent exit.

Why
---
The old api_runner._auto_commit ran at IMPLEMENT exit (unverified work),
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
import os
import shlex
import subprocess
from pathlib import Path

logger = logging.getLogger("completion_commit")


def _push_enabled() -> bool:
    """Push only when GITHUB_DRY_RUN is explicitly off. Default: no push."""
    try:
        from codebot.credentials import get_dry_run
        return not get_dry_run()
    except Exception:
        return os.environ.get("GITHUB_DRY_RUN", "1").lower() not in ("1", "true", "yes", "on")


def push_current_branch(
    repo: Path,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Push HEAD to origin (fail-open). Returns (ok, output).

    Never force-pushes. Uses credentials.setup_git_environment for SSH
    known_hosts/key wiring when no explicit env is given.
    """
    if not _push_enabled():
        return False, "push disabled: GITHUB_DRY_RUN is on"
    if env is None:
        try:
            from codebot.credentials import setup_git_environment
            env = setup_git_environment()
        except Exception as e:
            return False, f"git env setup failed: {e}"
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "push", "origin", "HEAD"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode == 0:
            logger.info("pushed %s to origin", repo)
            return True, out
        logger.warning("git push failed in %s: %s", repo, out[:300])
        return False, out
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return False, str(e)


def sync_ticket_issue(
    ticket_id: str,
    title: str,
    sha: str,
    state: str = "COMPLETE",
    timeout: int = 30,
) -> tuple[bool, str]:
    """Mirror a completed ticket to GitHub Issues via gh CLI (fail-open).

    COMPLETE closes the matching `[ticket_id]` issue (or creates it closed).
    Any other state ensures an open issue exists for visibility.
    Requires gh auth; degrades to (False, reason) without it.
    """
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            return False, "gh not authenticated"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return False, f"gh unavailable: {e}"

    def _gh(*args: str) -> tuple[bool, str]:
        try:
            p = subprocess.run(
                ["gh", *args], capture_output=True, text=True, timeout=timeout,
            )
            out = ((p.stdout or "") + (p.stderr or "")).strip()
            return p.returncode == 0, out
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            return False, str(e)

def _add_to_fleet_project(number: str, timeout: int = 30) -> None:
    """Add an issue to the CodeBot Fleet project board (fail-open)."""
    try:
        import subprocess as _sp
        _sp.run(
            ["gh", "project", "item-add", "1", "--owner", "LinuxGuru999",
             "--url", f"https://github.com/LinuxGuru999/CodeBot/issues/{number}"],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        pass


    ok, listing = _gh("issue", "list", "--search", f"[{ticket_id}]", "--state", "all",
                      "--limit", "5", "--json", "number,state")
    number = ""
    if ok and listing:
        try:
            import json as _json
            items = _json.loads(listing)
            if items:
                number = str(items[0].get("number", ""))
        except Exception:
            number = ""
    body = f"Ticket {ticket_id} {state}.\n\nCommit: {sha}\nTitle: {title[:200]}"
    if state == "COMPLETE":
        if number:
            _gh("issue", "comment", number, "--body", f"Completed in {sha}\n\n{title[:200]}")
            ok, out = _gh("issue", "close", number)
            if ok:
                _add_to_fleet_project(number)
            return ok, number if ok else out
        ok, out = _gh("issue", "create", "--title", f"[{ticket_id}] {title[:120]}",
                      "--body", body, "--label", "codebot")
        if ok:
            _add_to_fleet_project(out.strip().split("/")[-1])
        return ok, out
    if number:
        return True, number
    ok, out = _gh("issue", "create", "--title", f"[{ticket_id}] {title[:120]}",
                  "--body", body, "--label", "codebot")
    if ok:
        _add_to_fleet_project(out.strip().split("/")[-1])
    return ok, out


def _branch_name(ticket_id: str) -> str:
    """Branch name for a ticket. Exported for tests."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in ticket_id)
    return f"cb/{safe}"


def open_ticket_branch(
    repo: Path,
    ticket_id: str,
    timeout: int = 30,
) -> tuple[bool, str]:
    """Create/reset branch cb/<ticket> at HEAD (fail-open). Returns (ok, branch)."""
    branch = _branch_name(ticket_id)
    ok, cur = _run_git(repo, "rev-parse", "--abbrev-ref", "HEAD", timeout=timeout)
    if ok and cur.strip() == branch:
        return True, branch
    ok, out = _run_git(repo, "checkout", "-B", branch, timeout=timeout)
    if not ok:
        logger.warning("ticket %s: branch checkout failed: %s", ticket_id, out[:200])
        return False, ""
    return True, branch


def open_pull_request(
    repo: Path,
    ticket_id: str,
    title: str,
    sha: str,
    base: str = "master",
    timeout: int = 60,
) -> tuple[bool, str]:
    """Push branch and open/update a PR via gh (fail-open). Returns (ok, url).

    Idempotent: if a PR already exists for the branch, returns its URL
    without creating a duplicate.
    """
    branch = _branch_name(ticket_id)
    ok, out = _run_git(repo, "push", "-u", "origin", branch, timeout=timeout)
    if not ok:
        logger.warning("ticket %s: branch push failed: %s", ticket_id, out[:200])
        return False, out
    try:
        proc = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--json", "url,state",
             "--jq", ".[0].url // empty"],
            capture_output=True, text=True, timeout=timeout, cwd=str(repo),
        )
        existing = (proc.stdout or "").strip()
        if proc.returncode == 0 and existing:
            return True, existing
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    msg = build_commit_message(ticket_id, title)
    try:
        proc = subprocess.run(
            ["gh", "pr", "create", "--base", base, "--head", branch,
             "--title", msg, "--body", f"Ticket {ticket_id}\n\nCommit: {sha}"],
            capture_output=True, text=True, timeout=timeout, cwd=str(repo),
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode == 0:
            url = out.splitlines()[0].strip() if out else ""
            logger.info("ticket %s PR opened: %s", ticket_id, url)
            return True, url
        logger.warning("ticket %s: PR create failed: %s", ticket_id, out[:300])
        return False, out
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return False, str(e)


def auto_merge_pull_request(
    pr_url_or_branch: str,
    timeout: int = 60,
) -> tuple[bool, str]:
    """Enable squash auto-merge on a PR (fail-open). Returns (ok, output)."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "merge", pr_url_or_branch, "--auto", "--squash"],
            capture_output=True, text=True, timeout=timeout,
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode == 0:
            return True, out
        logger.warning("auto-merge failed for %s: %s", pr_url_or_branch, out[:200])
        return False, out
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return False, str(e)


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
    branch: bool = False,
) -> tuple[bool, str]:
    """Stage + commit exactly `files` in workspace. Returns (ok, sha).

    - Resolves each file against workspace; skips missing files and files
      outside the repo (never commits stray absolute paths).
    - Stages per-file (`git add -- <file>`), commits once with message
      `[<ticket_id>] <title>`, returns the new HEAD SHA.
    - With branch=True, opens/resets branch cb/<ticket> first so the commit
      lands on the ticket branch instead of the current branch.
    - "Nothing to commit" (files already committed/clean) is success with
      the current HEAD SHA — the work is already in the tree.
    - Any failure returns (False, "") and logs at WARNING.
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

    if branch:
        ok, _ = open_ticket_branch(workspace, ticket_id, timeout=timeout)
        if not ok:
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
