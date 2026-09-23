#!/usr/bin/env python3
"""VCS Adapter — Git operations delegated from orchestrator.

Purpose
-------
Owns all version control operations (git commit, push, pull, etc.).
Orchestrator delegates to this module instead of calling subprocess directly.

Invariants
----------
- All git operations go through this module
- Never raises exceptions to callers (fail-open)
- Resolves paths independently (no upward dependency on state_manager)
- Accepts path injection via set_project_root() for testing
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Module-level project root (can be overridden via set_project_root for injection)
_project_root_override: Path | None = None

# Compute default project root independently
_VCS_ADAPTER_DIR = Path(__file__).parent
_DEFAULT_PROJECT_ROOT = _VCS_ADAPTER_DIR.parent


def set_project_root(path: Path) -> None:
    """Inject a custom project root (for testing or adapter injection).
    
    This allows external configuration without creating upward dependencies.
    """
    global _project_root_override
    _project_root_override = path


def _resolve_project_root() -> Path:
    """Resolve the project root independently.
    
    Returns injected path if set, otherwise computes default path.
    No dependency on state_manager module.
    """
    if _project_root_override is not None:
        return _project_root_override
    return _DEFAULT_PROJECT_ROOT


def git_commit(message: str, files: list[str] | None = None, cwd: Path | None = None) -> bool:
    """Stage files and create a git commit.
    
    Args:
        message: Commit message
        files: Optional list of files to stage. If None, stages all changes.
        cwd: Working directory. Defaults to project root.
    
    Returns:
        True if commit succeeded, False otherwise.
    """
    project_root = cwd or _resolve_project_root()
    try:
        if files:
            subprocess.run(
                ["git", "add"] + files,
                capture_output=True,
                timeout=30,
                cwd=str(project_root),
                check=True,
            )
        else:
            subprocess.run(
                ["git", "add", "-A"],
                capture_output=True,
                timeout=30,
                cwd=str(project_root),
                check=True,
            )
        subprocess.run(
            ["git", "commit", "-m", message],
            capture_output=True,
            timeout=30,
            cwd=str(project_root),
            check=True,
        )
        logger.info("Git commit successful: %s", message[:50])
        return True
    except subprocess.TimeoutExpired:
        logger.warning("Git commit timed out for: %s", message[:50])
        return False
    except subprocess.CalledProcessError as e:
        logger.warning("Git commit failed: %s (stderr: %s)", message[:50], e.stderr.decode()[:200] if e.stderr else "")
        return False
    except FileNotFoundError:
        logger.warning("Git not available")
        return False
    except Exception as e:
        logger.warning("Git commit error: %s", e)
        return False


def git_push(remote: str = "origin", branch: str | None = None, cwd: Path | None = None) -> bool:
    """Push commits to remote repository.
    
    Args:
        remote: Remote name (default: origin)
        branch: Branch name. If None, pushes current branch.
        cwd: Working directory. Defaults to project root.
    
    Returns:
        True if push succeeded, False otherwise.
    """
    project_root = cwd or _resolve_project_root()
    try:
        cmd = ["git", "push", remote]
        if branch:
            cmd.append(branch)
        subprocess.run(
            cmd,
            capture_output=True,
            timeout=60,
            cwd=str(project_root),
            check=True,
        )
        logger.info("Git push successful to %s/%s", remote, branch or "current")
        return True
    except subprocess.TimeoutExpired:
        logger.warning("Git push timed out")
        return False
    except subprocess.CalledProcessError as e:
        logger.warning("Git push failed (stderr: %s)", e.stderr.decode()[:200] if e.stderr else "")
        return False
    except FileNotFoundError:
        logger.warning("Git not available")
        return False
    except Exception as e:
        logger.warning("Git push error: %s", e)
        return False


def git_pull(remote: str = "origin", branch: str | None = None, cwd: Path | None = None) -> bool:
    """Pull latest changes from remote repository.
    
    Args:
        remote: Remote name (default: origin)
        branch: Branch name. If None, pulls current branch.
        cwd: Working directory. Defaults to project root.
    
    Returns:
        True if pull succeeded, False otherwise.
    """
    project_root = cwd or _resolve_project_root()
    try:
        cmd = ["git", "pull", remote]
        if branch:
            cmd.append(branch)
        subprocess.run(
            cmd,
            capture_output=True,
            timeout=60,
            cwd=str(project_root),
            check=True,
        )
        logger.info("Git pull successful from %s/%s", remote, branch or "current")
        return True
    except subprocess.TimeoutExpired:
        logger.warning("Git pull timed out")
        return False
    except subprocess.CalledProcessError as e:
        logger.warning("Git pull failed (stderr: %s)", e.stderr.decode()[:200] if e.stderr else "")
        return False
    except FileNotFoundError:
        logger.warning("Git not available")
        return False
    except Exception as e:
        logger.warning("Git pull error: %s", e)
        return False


def git_status(cwd: Path | None = None) -> dict[str, Any]:
    """Get git status information.
    
    Args:
        cwd: Working directory. Defaults to project root.
    
    Returns:
        Dict with status information, or empty dict on failure.
    """
    project_root = cwd or _resolve_project_root()
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            timeout=10,
            cwd=str(project_root),
            text=True,
        )
        lines = result.stdout.strip().splitlines() if result.stdout.strip() else []
        modified = []
        added = []
        deleted = []
        for line in lines:
            if not line:
                continue
            status_code = line[:2]
            filepath = line[3:].strip()
            if "M" in status_code:
                modified.append(filepath)
            if "A" in status_code or "??" in status_code:
                added.append(filepath)
            if "D" in status_code:
                deleted.append(filepath)
        return {
            "modified": modified,
            "added": added,
            "deleted": deleted,
            "clean": len(lines) == 0,
        }
    except Exception as e:
        logger.debug("Git status error: %s", e)
        return {}


def get_current_branch(cwd: Path | None = None) -> str:
    """Get current git branch name.
    
    Args:
        cwd: Working directory. Defaults to project root.
    
    Returns:
        Branch name, or empty string on failure.
    """
    project_root = cwd or _resolve_project_root()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            timeout=10,
            cwd=str(project_root),
            text=True,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def auto_commit_and_push(ticket_id: str, bot_name: str, message: str | None = None, cwd: Path | None = None) -> bool:
    """Auto-commit changes and push to remote.
    
    Convenience function that combines commit and push operations.
    
    Args:
        ticket_id: Ticket ID for commit message context
        bot_name: Bot name for attribution
        message: Optional custom commit message. If None, generates default.
        cwd: Working directory. Defaults to project root.
    
    Returns:
        True if both commit and push succeeded, False otherwise.
    """
    if message is None:
        message = f"Auto-commit by {bot_name} for ticket {ticket_id}"
    
    if not git_commit(message, cwd=cwd):
        return False
    
    return git_push(cwd=cwd)
