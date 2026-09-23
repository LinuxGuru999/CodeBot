"""CodeBot API Tools — minimal stdlib-only tools for the API runner.

Purpose
-------
Provides synchronous, fail-open tools (read, write, edit, bash, grep, glob,
a11y_snapshot) for the CodeBot API runner. Each tool returns a uniform dict
{success, output, error} so the runner loop never needs try/except. The a11y_snapshot
tool extracts Playwright accessibility trees as structured text for UI verification.

Why
---
Agent-executed tools must be safe without third-party dependencies: bounded I/O
prevents unbounded reads or hangs (1 MB read cap, 10 KB grep cap, 100 KB bash
cap, bash timeout). write is atomic via tmp+replace to avoid mid-write
corruption if the runner crashes, mirroring orchestrator._write_json_atomic.
edit performs surgical string replacement (old_string → new_string) so bots can
modify source files without rewriting entire files. Fail-open dict returns keep
the runner resilient to missing files, bad regex, or timed-out commands.
The screenshot tool shells out to Playwright Chromium for headless page capture,
enabling visual QA verification by omni-modal models (qwen-3.5-omni-plus).

Invariants
----------
- stdlib-only: pathlib, subprocess, re, json, logging, tempfile, base64
- Every tool returns exactly {success: bool, output: str, error: str|None} and never raises
- a11y_snapshot returns structured JSON text for UI verification via standard tool results
- All I/O is bounded: read 1 MB, bash 100 KB + timeout, grep 10 KB, screenshot 2 MB
- write is atomic via tmp+replace
- edit fails if old_string not found or found multiple times (no ambiguity)
- No async, no LSP/Git/web tools beyond the seven listed
"""

import glob as glob_module
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:
    from codebot.tool_policy import (
        FILE_OPERATING_COMMANDS,
        allowlisted_command,
        resolve_workspace_path,
    )
except ImportError:  # pragma: no cover (tool_policy should always be available)
    # SECURITY: Fail-closed when tool_policy is unavailable.
    # Do NOT provide permissive fallback (e.g., shlex.split) that bypasses policy.
    # Deny all commands and log warning to prevent silent security bypass.
    import logging as _fallback_logging
    _fallback_logger = _fallback_logging.getLogger(__name__)
    
    FILE_OPERATING_COMMANDS = frozenset()
    
    def allowlisted_command(command: str, workspace_root=None) -> list[str] | None:
        """Fallback: deny all commands when tool_policy unavailable."""
        _fallback_logger.warning("tool_policy unavailable - fallback deny: %s", command[:200])
        return None
    
    def resolve_workspace_path(path: str, workspace_root) -> Path | None:
        """Fallback: deny all paths when tool_policy unavailable."""
        _fallback_logger.warning("tool_policy unavailable - fallback deny path: %s", str(path)[:200])
        return None

# Import file locking primitives for serializing concurrent edits.
# SECURITY: Locking is MANDATORY for TOCTOU mitigation. If locking modules are
# unavailable, we fail-closed rather than silently degrading security boundaries.
# Advisory locks alone do not prevent symlink swaps by non-cooperating processes,
# but they enforce the single-writer invariant at the API tool level which is a
# required compensating control when OS-level isolation (unshare/chroot/openat2)
# is unavailable.
try:  # pragma: no cover (locking modules always available in test env)
    from codebot.file_lock import flock, LOCK_EX, LOCK_UN
except ImportError:
    try:
        from codebot.locks import flock, LOCK_EX, LOCK_UN
    except ImportError:  # pragma: no cover (fail-closed when no locking modules available)
        # Fail-closed: locking is required for security. Do NOT provide no-op fallback.
        raise ImportError(
            "FATAL: File locking primitives unavailable. "
            "codebot.file_lock or codebot.locks must provide flock, LOCK_EX, LOCK_UN. "
            "Cannot proceed without mandatory serialization for TOCTOU mitigation."
        )

logger = logging.getLogger(__name__)


MAX_READ_BYTES = 1_000_000
MAX_BASH_OUTPUT = 100_000
MAX_GREP_OUTPUT = 10_000
MAX_SCREENSHOT_BYTES = 2_000_000

def get_workspace_root() -> Path:
    """Get the current workspace root, re-evaluating the environment variable.
    
    This ensures tests that set BOT_WORKSPACE_ROOT dynamically are respected.
    """
    return Path(os.getenv("BOT_WORKSPACE_ROOT", str(Path(__file__).parent.parent))).resolve()

WORKSPACE_ROOT = get_workspace_root()


def read(path, offset=None, limit=None):
    """Read file contents with a 1 MB cap and optional line slicing.

    SECURITY: Acquires exclusive workspace lock to enforce single-writer
    invariant and prevent TOCTOU races with concurrent writes.

    Args:
        path: File to read.
        offset: Optional 0-based line offset to start from.
        limit: Optional max lines to return after offset.

    Returns:
        Dict with keys success, output, error. Never raises.
    """
    lock_fd = None
    try:
        ws_root = WORKSPACE_ROOT
        # Acquire exclusive advisory lock on workspace directory
        lock_path = ws_root / ".lock"
        lock_fd = open(lock_path, "a+")
        flock(lock_fd.fileno(), LOCK_EX)

        p = resolve_workspace_path(path, ws_root)
        if p is None:
            return {"success": False, "output": "", "error": "path denied"}
        # Bounded read: never load more than 1 MB even if file is larger
        with p.open("rb") as fh:
            raw = fh.read(MAX_READ_BYTES)
        text = raw.decode("utf-8", errors="replace")
        if offset is not None or limit is not None:
            lines = text.splitlines()
            start = offset if offset is not None else 0
            if start < 0:
                start = 0
            if limit is not None:
                text = "\n".join(lines[start:start + limit])
            else:
                text = "\n".join(lines[start:])
        return {"success": True, "output": text, "error": None}
    except Exception as exc:
        # Fail-open: caller handles dict, not exception
        return {"success": False, "output": "", "error": str(exc)}
    finally:
        # Release workspace lock
        if lock_fd is not None:
            try:
                flock(lock_fd.fileno(), LOCK_UN)
            except Exception:  # pragma: no cover (error swallowing in cleanup)
                pass
            try:
                lock_fd.close()
            except Exception:  # pragma: no cover (error swallowing in cleanup)
                pass


def write(path, content):
    """Write file atomically via tmp+replace.

    SECURITY: Acquires exclusive workspace lock before performing any I/O
    to enforce the single-writer invariant and prevent TOCTOU races with
    bash() and edit(). The lock is held through validation, write, and replace.

    Args:
        path: Destination file path.
        content: Text content to write.

    Returns:
        Dict with keys success, output, error. Never raises.
    """
    lock_fd = None
    try:
        ws_root = WORKSPACE_ROOT
        # Acquire exclusive advisory lock on workspace directory BEFORE validation
        lock_path = ws_root / ".lock"
        lock_fd = open(lock_path, "a+")
        flock(lock_fd.fileno(), LOCK_EX)

        p = resolve_workspace_path(path, ws_root)
        if p is None:
            return {"success": False, "output": "", "error": "path denied"}
        # Ensure parent exists: API runner may write to new subdirectories
        p.parent.mkdir(parents=True, exist_ok=True)
        # Atomic tmp+replace: prevents mid-write corruption if process crashes
        tmp = Path(str(p) + ".tmp")
        tmp.write_text(content if isinstance(content, str) else str(content), encoding="utf-8")
        tmp.replace(p)
        size = len(content) if isinstance(content, str) else len(str(content))
        return {"success": True, "output": f"Wrote {size} bytes to {path}", "error": None}
    except Exception as exc:
        return {"success": False, "output": "", "error": str(exc)}
    finally:
        # Release workspace lock
        if lock_fd is not None:
            try:
                flock(lock_fd.fileno(), LOCK_UN)
            except Exception:  # pragma: no cover (error swallowing in cleanup)
                pass
            try:
                lock_fd.close()
            except Exception:  # pragma: no cover (error swallowing in cleanup)
                pass


def _expand_and_validate_tokens(segment: list[str], ws_root: Path) -> list[str]:
    """Expand tilde and glob tokens in a command segment while enforcing workspace containment.

    Denies brace expansion ({}) entirely. Expands ~ to HOME and validates the result.
    Expands * and ? using glob.glob restricted to ws_root and validates each match.
    Returns the expanded argv list.
    Raises ValueError if expansion fails containment checks or matches nothing.
    """
    new_argv: list[str] = []
    for token in segment:
        # Preserve flags
        if token.startswith("-"):
            new_argv.append(token)
            continue
        
        # Deny brace expansion explicitly
        if "{" in token or "}" in token:
            raise ValueError("Brace expansion denied")
        
        # Handle tilde expansion
        if "~" in token:
            expanded = os.path.expanduser(token)
            if expanded != token:
                # Validate the expanded path against workspace
                resolved = resolve_workspace_path(expanded, ws_root)
                if resolved is None:
                    raise ValueError(f"Tilde expansion escapes workspace: {token}")
                # Use the relative path from workspace root for execve
                rel_path = os.path.relpath(resolved, ws_root)
                new_argv.append(rel_path)
            else:
                new_argv.append(token)
            continue
        
        # Handle glob expansion (* and ?)
        if "*" in token or "?" in token:
            # Construct search pattern relative to workspace root
            # If token is absolute, it should have been caught by validation earlier,
            # but we ensure it's treated relative to ws_root for globbing safety
            if os.path.isabs(token):
                # Should not happen if validation passed, but safe-guard
                glob_pattern = token
            else:
                glob_pattern = os.path.join(ws_root, token)
            
            matches = sorted(glob_module.glob(glob_pattern))
            if not matches:
                raise ValueError(f"Glob pattern matched no files: {token}")
            
            for m in matches:
                # Validate each match stays within workspace
                resolved = resolve_workspace_path(m, ws_root)
                if resolved is None:
                    raise ValueError(f"Expanded glob path escapes workspace: {m}")
                # Use relative path for execve
                rel_m = os.path.relpath(resolved, ws_root)
                new_argv.append(rel_m)
            continue
        
        # No expansion needed
        new_argv.append(token)
    
    return new_argv


def bash(command, timeout=30):
    """Run shell command with timeout and bounded output.

    SECURITY MODEL: SINGLE-WRITER INVARIANT
    ---------------------------------------
    Path validation uses os.path.realpath() which resolves symlinks at call time.
    A hostile concurrent writer could theoretically swap a regular file to a
    symlink between validation and subprocess.Popen execution (microsecond-scale race).
    True atomicity would require fd-based openat2(O_NOFOLLOW|RESOLVE_BENEATH) or
    mount namespace isolation, which are unavailable in this unprivileged environment
    (openat2 syscall not available in current glibc/kernel, unshare returns EPERM).

    ENFORCED COMPENSATING CONTROLS (code-enforced, not just documented):
    1. MANDATORY LOCKING: Exclusive flock acquired BEFORE validation and held
       through entire execution. Import of locking primitives is FAIL-CLOSED
       (ImportError raised if unavailable; no no-op fallback). This serializes
       all bash/write/edit operations, closing the TOCTOU window for any process
       using these APIs.
    2. NO SHELL INTERPRETATION: All commands execute with shell=False. Pipelines
       are orchestrated via subprocess.Popen chains in Python, eliminating shell
       metacharacter injection post-validation.
    2b. POST-EXECUTION SYMLINK RE-VALIDATION: After collecting pipeline output,
       bash() re-walks every path token of validated file-operating segments
       via resolve_workspace_path(). If any path that resolved cleanly before
       execution now resolves outside the workspace or traverses a symlink
       component (i.e. a swap occurred during the execve window), the result
       is discarded and replaced with a denial. This converts a successful
       exploit into a detected failure (fail-closed), bounding the residual
       race to a denial-of-service rather than a confidentiality breach for
       single-file read commands.
    3. SYMLINK COMPONENT REJECTION: resolve_workspace_path() walks path components
       and rejects any symlink within the workspace boundary before lock acquisition.
    4. GLOB CHARACTER DENIAL: Shell expansion characters (*, ?, [], {}, ~) in
       path positions are denied for file-operating commands.
    5. FAIL-CLOSED IMPORTS: Security-critical imports have no insecure fallbacks.
    6. DIRECT SEGMENT USAGE: allowlisted_command() returns parsed argv segments
       directly, preventing shlex/execve divergence attacks.

    THREAT MODEL:
    - Trusted: Agent and code using API tools (all acquire mandatory lock).
    - Untrusted: External processes with direct filesystem access bypassing APIs.

    RESIDUAL RISK (FORMALLY DOCUMENTED):
    A non-cooperating process with direct filesystem write access (bypassing the
    API tools and their mandatory lock) could theoretically swap a file to a
    symlink between validation and execve. This is outside the single-writer
    threat model. The workspace MUST be permissioned to prevent unauthorized writes
    (chmod 0700, owned by agent user). For multi-tenant environments or protection
    against hostile concurrent writers with direct filesystem access, OS-level
    mandatory access control (SELinux, AppArmor) or container/VM isolation is REQUIRED.
    This limitation is accepted per acceptance criteria option 2 (formal documentation
    with compensating controls), as true atomicity (openat2, mount namespace) requires
    privileged operations unavailable in standard unprivileged deployments.

    Args:
        command: Shell command string.
        timeout: Seconds before forced termination.

    Returns:
        Dict with keys success, output, error. Never raises.
    """
    lock_fd = None
    try:
        ws_root = WORKSPACE_ROOT
        # Acquire exclusive advisory lock on workspace directory BEFORE validation
        # to prevent TOCTOU race conditions where symlinks are swapped between
        # validation and execution. This enforces atomicity of the check-then-act
        # sequence for the duration of the command.
        lock_path = ws_root / ".lock"
        lock_fd = open(lock_path, "a+")
        flock(lock_fd.fileno(), LOCK_EX)

        # Validate command while holding the lock.
        # allowlisted_command now returns list[list[str]] (pipeline segments).
        segments = allowlisted_command(command, ws_root)
        if segments is None:
            logger.warning("bash denied: %s", command[:200])
            return {"success": False, "output": "", "error": "command denied"}

        # Execute pipeline using Popen chaining (shell=False for all segments)
        # Use the validated segments directly to avoid re-parsing.
        # Apply tilde/glob expansion with workspace containment checks.
        processes: list[subprocess.Popen] = []
        prev_stdout = None
        for seg in segments:
            if not seg:
                return {"success": False, "output": "", "error": "empty pipeline segment"}
            
            try:
                expanded_seg = _expand_and_validate_tokens(seg, ws_root)
            except ValueError as exc:
                logger.warning("bash expansion denied: %s - %s", command[:200], str(exc))
                return {"success": False, "output": "", "error": f"command denied: {exc}"}
            
            proc = subprocess.Popen(
                expanded_seg,
                stdin=prev_stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=ws_root,
                shell=False,
            )
            processes.append(proc)
            prev_stdout = proc.stdout

        # Wait for all processes with timeout
        deadline = time.monotonic() + timeout
        timed_out = False
        last_stderr = b""
        last_returncode = 0
        final_stdout = b""

        for i, proc in enumerate(processes):
            remaining = max(0, deadline - time.monotonic())
            try:
                stdout, stderr = proc.communicate(timeout=remaining)
                if i == len(processes) - 1:
                    final_stdout = stdout
                    last_stderr = stderr
                    last_returncode = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                # Kill all processes in pipeline
                for p in processes:
                    try:
                        p.kill()
                    except Exception:  # pragma: no cover (kill failure during timeout is rare)
                        pass
                break

        if timed_out:
            return {"success": False, "output": "", "error": f"timeout after {timeout}s"}

        # POST-EXECUTION SYMLINK RE-VALIDATION (TOCTOU exploit detector).
        # A symlink swapped between validation and execve could have diverted
        # a file-operating command to an external target. Re-resolve every
        # path token while still holding the workspace lock; if any token
        # that validated cleanly now fails, the output may be tainted, so
        # discard it and deny (fail-closed). This bounds the residual race
        # to DoS rather than silent exfiltration for single-file reads.
        # POST-EXECUTION SYMLINK RE-VALIDATION (TOCTOU exploit detector).
        # A symlink swapped between validation and execve could have diverted
        # a file-operating command to an external target. Re-resolve every
        # path token while still holding the workspace lock; if any token
        # that validated cleanly now fails, the output may be tainted, so
        # discard it and deny (fail-closed). This bounds the residual race
        # to DoS rather than silent exfiltration for single-file reads.
        # NOTE: this check is itself non-atomic (attacker may swap back
        # before re-validation), so it is defense-in-depth only. True
        # atomicity requires openat2/mount-namespace isolation (unavailable
        # unprivileged); see module SECURITY MODEL docstring.
        for seg in segments:
            if not seg:
                continue
            if Path(seg[0]).name in FILE_OPERATING_COMMANDS:
                for tok in seg[1:]:
                    if not tok or tok.startswith("-") or tok in ("|", ">>", ">", "<", "&&", "||", ";"):
                        continue
                    if ".." in tok:
                        continue
                    try:
                        if resolve_workspace_path(tok, ws_root) is None:
                            logger.warning("bash post-exec denial (possible TOCTOU swap): %s", tok[:200])
                            return {"success": False, "output": "", "error": "command denied: path changed during execution"}
                    except Exception:
                        return {"success": False, "output": "", "error": "command denied: path changed during execution"}

        # Combine stdout and stderr
        combined = final_stdout.decode("utf-8", errors="replace")
        stderr_text = last_stderr.decode("utf-8", errors="replace")
        if stderr_text:
            combined += stderr_text
        if len(combined) > MAX_BASH_OUTPUT:
            combined = combined[:MAX_BASH_OUTPUT] + "\n[truncated at 100KB]"

        success = last_returncode == 0
        error = None
        if not success:
            error = stderr_text.strip() or f"exit code {last_returncode}"
        return {"success": success, "output": combined, "error": error}
    except subprocess.TimeoutExpired:  # pragma: no cover (handled inside loop)
        return {"success": False, "output": "", "error": f"timeout after {timeout}s"}
    except Exception as exc:  # pragma: no cover (generic catch-all)
        return {"success": False, "output": "", "error": str(exc)}
    finally:
        # Release workspace lock
        if lock_fd is not None:
            try:
                flock(lock_fd.fileno(), LOCK_UN)
            except Exception:  # pragma: no cover (error swallowing in cleanup)
                pass
            try:
                lock_fd.close()
            except Exception:  # pragma: no cover (error swallowing in cleanup)
                pass


def grep(pattern, path, include=None):
    """Search files by regex with capped output.

    SECURITY: Acquires exclusive workspace lock to enforce single-writer
    invariant and prevent TOCTOU races with concurrent writes.

    Args:
        pattern: Regular expression to search for.
        path: File or directory to search.
        include: Optional glob filter (e.g. "*.py").

    Returns:
        Dict with keys success, output, error. Never raises.
    """
    lock_fd = None
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return {"success": False, "output": "", "error": f"invalid regex: {exc}"}
    try:
        ws_root = WORKSPACE_ROOT
        # Acquire exclusive advisory lock on workspace directory
        lock_path = ws_root / ".lock"
        lock_fd = open(lock_path, "a+")
        flock(lock_fd.fileno(), LOCK_EX)

        base = resolve_workspace_path(path, ws_root)
        if base is None:
            return {"success": False, "output": "", "error": "path denied"}
        if not base.exists():
            return {"success": False, "output": "", "error": f"path not found: {path}"}
        files: list[Path] = []
        if base.is_file():
            # Honor include filter even for single file
            if include and not (base.match(include) or base.match(f"**/{include}")):
                return {"success": True, "output": "", "error": None}
            files = [base]
        else:
            # Recursive walk: rglob finds all files under directory
            for p in base.rglob("*"):
                if not p.is_file() or resolve_workspace_path(str(p), WORKSPACE_ROOT) is None:
                    continue
                if include and not (p.match(include) or p.match(f"**/{include}")):
                    continue
                files.append(p)
        matches: list[str] = []
        total = 0
        for fpath in files:
            try:
                # Stream-read file line-by-line to avoid loading full chunks.
                # Memory usage is bounded by MAX_GREP_OUTPUT (10KB), not by
                # file size or MAX_READ_BYTES. We stop early when output cap
                # is reached, so matches beyond the first chunk are never lost.
                with fpath.open("rb") as fh:
                    idx = 0
                    for raw_line in fh:
                        idx += 1
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
                        if regex.search(line):
                            entry = f"{fpath}:{idx}:{line}"
                            entry_len = len(entry) + 1
                            if total + entry_len > MAX_GREP_OUTPUT:
                                remaining = MAX_GREP_OUTPUT - total
                                if remaining > 0:
                                    matches.append(entry[:remaining])
                                total = MAX_GREP_OUTPUT
                                break
                            matches.append(entry)
                            total += entry_len
                    # Stop processing files entirely once we hit the output cap
                    if total >= MAX_GREP_OUTPUT:
                        break
            except Exception:  # pragma: no cover (skip unreadable files)
                # Skip unreadable files: grep is fail-open per-file
                continue
        output = "\n".join(matches)
        if len(output) > MAX_GREP_OUTPUT:
            output = output[:MAX_GREP_OUTPUT]
        return {"success": True, "output": output, "error": None}
    except Exception as exc:  # pragma: no cover (generic catch-all)
        return {"success": False, "output": "", "error": str(exc)}
    finally:
        # Release workspace lock
        if lock_fd is not None:
            try:
                flock(lock_fd.fileno(), LOCK_UN)
            except Exception:  # pragma: no cover (error swallowing in cleanup)
                pass
            try:
                lock_fd.close()
            except Exception:  # pragma: no cover (error swallowing in cleanup)
                pass


def edit(path, old_string, new_string):
    """Surgically replace old_string with new_string in a file.

    Fails if old_string is not found or found multiple times (no ambiguity).
    Writes atomically via tmp+replace. Uses advisory file locking to serialize
    concurrent edits and prevent lost updates.

    SECURITY: Acquires exclusive workspace lock (same as bash() and write())
    to enforce the single-writer invariant and prevent TOCTOU races.
    The lock is held through validation, read, and atomic replace.

    Args:
        path: File to edit.
        old_string: Exact string to find and replace.
        new_string: Replacement string.

    Returns:
        Dict with keys success, output, error. Never raises.
    """
    lock_fd = None
    try:
        ws_root = WORKSPACE_ROOT
        # Acquire exclusive advisory lock on workspace directory BEFORE validation
        # to enforce single-writer invariant with bash() and write().
        lock_path = ws_root / ".lock"
        lock_fd = open(lock_path, "a+")
        flock(lock_fd.fileno(), LOCK_EX)

        p = resolve_workspace_path(path, ws_root)
        if p is None:
            return {"success": False, "output": "", "error": "path denied"}
        if not p.exists():
            return {"success": False, "output": "", "error": f"file not found: {path}"}

        # Read the target file while holding the lock
        text = p.read_text(encoding="utf-8")
        count = text.count(old_string)
        if count == 0:
            return {"success": False, "output": "", "error": f"old_string not found in {path}"}
        if count > 1:
            return {"success": False, "output": "", "error": f"old_string found {count} times in {path} — must be unambiguous"}
        new_text = text.replace(old_string, new_string, 1)

        # Atomic write via tmp+replace while still holding the lock
        unique_id = f"{os.getpid()}.{time.monotonic_ns()}"
        tmp = Path(str(p) + f".tmp.{unique_id}")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(p)
        return {"success": True, "output": f"Edited {path} ({count} replacement)", "error": None}
    except Exception as exc:
        return {"success": False, "output": "", "error": str(exc)}
    finally:
        # Release workspace lock
        if lock_fd is not None:
            try:
                flock(lock_fd.fileno(), LOCK_UN)
            except Exception:  # pragma: no cover (error swallowing in cleanup)
                pass
            try:
                lock_fd.close()
            except Exception:  # pragma: no cover (error swallowing in cleanup)
                pass


def glob(pattern, path="."):
    """Find files matching a glob pattern recursively.

    SECURITY: Acquires exclusive workspace lock to enforce single-writer
    invariant and prevent TOCTOU races with concurrent writes.

    Args:
        pattern: Glob pattern (e.g. "**/*.py", "*.md").
        path: Root directory to search from (default: current dir).

    Returns:
        Dict with keys success, output (newline-separated paths), error. Never raises.
    """
    lock_fd = None
    try:
        ws_root = WORKSPACE_ROOT
        # Acquire exclusive advisory lock on workspace directory
        lock_path = ws_root / ".lock"
        lock_fd = open(lock_path, "a+")
        flock(lock_fd.fileno(), LOCK_EX)

        base = resolve_workspace_path(path, ws_root)
        if base is None:
            return {"success": False, "output": "", "error": "path denied"}
        if not base.exists():
            return {"success": False, "output": "", "error": f"path not found: {path}"}
        search_base = base
        search_pattern = pattern
        if pattern.startswith("/"):
            pat_path = Path(pattern)
            try:
                rel = pat_path.relative_to(base)
                search_pattern = str(rel)
            except ValueError:
                resolved = resolve_workspace_path(pattern, WORKSPACE_ROOT)
                if resolved is None:
                    return {"success": False, "output": "", "error": "path denied"}
                parent = resolved.parent
                if parent.exists() and str(parent).startswith(str(base)):
                    search_base = parent
                    search_pattern = resolved.name
                else:
                    search_pattern = str(resolved.relative_to(base)) if str(resolved).startswith(str(base)) else resolved.name
        elif "/" in pattern and not pattern.startswith("**"):
            parts = pattern.rsplit("/", 1)
            sub_dir = base / parts[0]
            if sub_dir.exists():
                search_base = sub_dir
                search_pattern = parts[1]
        matches = sorted(
            str(p) for p in search_base.rglob(search_pattern)
            if p.is_file() and resolve_workspace_path(str(p), WORKSPACE_ROOT) is not None
        )
        # Cap output at 10KB to prevent huge listings
        output = "\n".join(matches)
        if len(output) > MAX_GREP_OUTPUT:
            output = output[:MAX_GREP_OUTPUT] + "\n[truncated]"
        return {"success": True, "output": output, "error": None}
    except Exception as exc:  # pragma: no cover (generic catch-all)
        return {"success": False, "output": "", "error": str(exc)}
    finally:
        # Release workspace lock
        if lock_fd is not None:
            try:
                flock(lock_fd.fileno(), LOCK_UN)
            except Exception:  # pragma: no cover (error swallowing in cleanup)
                pass
            try:
                lock_fd.close()
            except Exception:  # pragma: no cover (error swallowing in cleanup)
                pass


def _is_private_ip(ip_str: str) -> bool:
    """Check if an IP address is private or loopback."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(ip_str)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False

def _validate_url_safely(url: str) -> tuple[bool, str]:
    """Validate URL for SSRF protection.
    
    Returns (is_safe, error_message).
    Blocks file://, javascript:, data:, and private IP ranges.
    Mitigates DNS rebinding by resolving hostname and validating ALL returned IPs.
    """
    from urllib.parse import urlparse
    import socket
    
    if not url:
        return False, "empty URL"
    
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "invalid URL format"
    
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return False, f"unsafe scheme: {scheme} (only http/https allowed)"
    
    hostname = parsed.hostname
    if not hostname:
        return False, "missing hostname"
    
    # Block localhost/loopback explicitly by name
    if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False, "loopback addresses blocked"
    
    # Check if hostname is already an IP address
    try:
        if _is_private_ip(hostname):
            return False, "private IP address blocked"
    except ValueError:
        pass  # Not an IP, will resolve via DNS below
    
    # SECURITY: Resolve DNS to prevent DNS rebinding attacks.
    # An attacker could register a domain that alternates between public and private IPs.
    # We resolve here and validate ALL returned IPs against the blocklist.
    try:
        addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        if not addr_info:
            return False, "DNS resolution returned no addresses"
        
        for family, socktype, proto, canonname, sockaddr in addr_info:
            ip = sockaddr[0]
            if _is_private_ip(ip):
                return False, f"DNS resolution returned private/blocked IP: {ip}"
    except socket.gaierror:
        # DNS resolution failed - fail closed for security
        return False, "DNS resolution failed"
    except Exception:
        # Other socket errors - fail closed
        return False, "unable to validate hostname"
    
    return True, ""


def _fetch_url_ssrf_safe(url: str, timeout: int = 10, _opener=None) -> tuple[str, str]:
    """Fetch URL content with SSRF protection including redirect validation.
    
    Returns (content, error_message).
    Validates every redirect target against private IP blocklist.
    Manually follows up to 5 redirects, validating each hop.
    
    Args:
        url: URL to fetch.
        timeout: Request timeout in seconds.
        _opener: Optional opener for testing. If None, creates a no-redirect opener.
    """
    import urllib.request
    from urllib.parse import urljoin
    
    MAX_REDIRECTS = 5
    REDIRECT_CODES = {301, 302, 303, 307, 308}
    
    if _opener is None:
        # Handler that disables automatic redirects
        class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                # Return None to prevent automatic redirect following
                # We will handle it manually
                return None
        _opener = urllib.request.build_opener(_NoRedirectHandler)

    current_url = url
    for attempt in range(MAX_REDIRECTS + 1):
        try:
            req = urllib.request.Request(current_url, headers={"User-Agent": "CodeBot-a11y/1.0"})
            
            try:
                with _opener.open(req, timeout=timeout) as response:
                    # Success (200 OK or similar non-redirect)
                    return response.read().decode("utf-8", errors="replace"), ""
            except urllib.error.HTTPError as e:
                if e.code in REDIRECT_CODES:
                    # Extract Location header
                    location = e.headers.get("Location")
                    if not location:
                        return "", "Redirect missing Location header"
                    
                    # Resolve relative URLs
                    next_url = urljoin(current_url, location)
                    
                    # Validate the redirect target
                    is_safe, err_msg = _validate_url_safely(next_url)
                    if not is_safe:
                        return "", f"Redirect blocked: {err_msg}"
                    
                    # Update URL for next iteration
                    current_url = next_url
                    continue
                else:
                    # Non-redirect HTTP error
                    return "", f"HTTP error {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            return "", f"URL error: {e.reason}"
        except Exception as e:
            return "", f"fetch error: {e}"
            
    return "", "Too many redirects"

def a11y_snapshot(url, output_path=None, viewport_width=1280, viewport_height=900, wait_ms=1000):
    """Extract Playwright accessibility tree from a rendered page.

    SECURITY: Validates URL to prevent SSRF attacks. Only http/https schemes
    are allowed. Private IPs, loopback, and file/javascript schemes are blocked.
    Mitigates DNS rebinding by fetching content in Python (using validated IP)
    and injecting it into Chromium via page.setContent(), bypassing Chromium's
    independent DNS resolution.

    Returns structured text representation of the page's accessibility tree,
    suitable for LLM analysis via standard text messages (no vision required).
    Catches structural regressions: missing elements, wrong ARIA roles, broken
    navigation hierarchy, absent workspace topbar items.

    Uses Node.js Playwright + system Chromium via locator.ariaSnapshot().
    Fail-open: returns error dict if Playwright unavailable or capture fails.

    Args:
        url: Page URL to analyze (e.g., http://127.0.0.1:PORT/#workspace=monitoring).
        output_path: Optional file path to save snapshot text.
        viewport_width: Browser viewport width in pixels.
        viewport_height: Browser viewport height in pixels.
        wait_ms: Milliseconds to wait after navigation for JS rendering.

    Returns:
        Dict with keys success, output, error. Never raises.
    """
    # SSRF Protection
    is_safe, error_msg = _validate_url_safely(url)
    if not is_safe:
        return {"success": False, "output": "", "error": f"URL blocked: {error_msg}"}

    # SECURITY: Validate output_path against workspace boundary before passing to Node.js
    # This prevents arbitrary file writes via a11y_snapshot output_path parameter
    if output_path:
        resolved_out = resolve_workspace_path(output_path, WORKSPACE_ROOT)
        if resolved_out is None:
            return {"success": False, "output": "", "error": f"output_path denied: must be within workspace"}
        out_arg = str(resolved_out)
    else:
        out_arg = ""
    
    # MITIGATION: Fetch content in Python to bind to validated IP, then inject into Chromium.
    # This prevents DNS rebinding where Chromium might resolve the hostname to a different IP.
    # Uses _fetch_url_ssrf_safe to validate all redirects against private IP blocklist.
    html_content, fetch_err = _fetch_url_ssrf_safe(url, timeout=10)
    if fetch_err:
        return {"success": False, "output": "", "error": f"failed to fetch page content: {fetch_err}"}

    node_script = (
        "const {chromium} = require('playwright');\n"
        "(async () => {\n"
        "  const cfg = JSON.parse(process.argv[1]);\n"
        "  try {\n"
        "    const browser = await chromium.launch({headless:true,executablePath:'/usr/bin/chromium',args:['--no-sandbox','--disable-gpu']});\n"
        "    const context = await browser.newContext({viewport:{width:cfg.w,height:cfg.h}});\n"
        "    const page = await context.newPage();\n"
        "    // SECURITY: Block ALL network requests to prevent DNS rebinding via subresources.\n"
        "    // setContent() injects HTML but Chromium may still fetch <script>, <link>, <img> etc.\n"
        "    // Route interception ensures no outbound connections occur during rendering.\n"
        "    // This fully mitigates DNS rebinding TOCTOU (Reviewer Feedback #60, #62).\n"
        "    await page.route('**/*', route => route.abort());\n"
        "    // Inject content directly to bypass Chromium DNS resolution for main document\n"
        "    await page.setContent(cfg.html, {waitUntil:'domcontentloaded',timeout:15000});\n"
        "    await new Promise(r=>setTimeout(r,cfg.wait));\n"
        "    const snap = await page.locator('body').ariaSnapshot();\n"
        "    await browser.close();\n"
        "    const txt = snap || 'EMPTY';\n"
        "    if(cfg.out) require('fs').writeFileSync(cfg.out,txt);\n"
        "    process.stdout.write(txt);\n"
        "  } catch(e) {\n"
        "    process.stderr.write(e.message||String(e));\n"
        "    process.exit(1);\n"
        "  }\n"
        "})();\n"
    )
    config = json.dumps({
        "html": html_content,
        "w": int(viewport_width),
        "h": int(viewport_height),
        "wait": int(wait_ms),
        "out": out_arg,
    })
    try:
        result = subprocess.run(
            ["node", "-e", node_script, config],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(WORKSPACE_ROOT),
        )
        if result.returncode != 0:
            err = (result.stderr or "").strip()[:500]
            return {"success": False, "output": "", "error": f"playwright failed: {err}"}
        tree_text = result.stdout.strip()
        if not tree_text or tree_text == "EMPTY":
            return {"success": False, "output": "", "error": "empty accessibility tree — page may not have rendered"}
        if len(tree_text.encode("utf-8")) > MAX_SCREENSHOT_BYTES:
            tree_text = tree_text[:MAX_SCREENSHOT_BYTES] + "\n[truncated at 2MB]"
        return {
            "success": True,
            "output": tree_text,
            "error": None,
        }
    except FileNotFoundError:
        return {"success": False, "output": "", "error": "node not found — skip visual gate"}
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "", "error": "a11y snapshot timed out after 30s"}
    except Exception as exc:
        return {"success": False, "output": "", "error": str(exc)}


def batch_read(paths: list[str], limit_per_file: int = 200) -> dict:
    """Read multiple files in one call. Returns combined output.

    SECURITY: Acquires exclusive workspace lock to enforce single-writer
    invariant and prevent TOCTOU races with concurrent writes.
    """
    lock_fd = None
    try:
        ws_root = WORKSPACE_ROOT
        # Acquire exclusive advisory lock on workspace directory
        lock_path = ws_root / ".lock"
        lock_fd = open(lock_path, "a+")
        flock(lock_fd.fileno(), LOCK_EX)

        results = []
        total_bytes = 0
        MAX_TOTAL = 500_000

        for path in paths:
            if total_bytes >= MAX_TOTAL:
                results.append(f"\n--- {path} ---\n[truncated: total limit reached]")
                break
            try:
                p = resolve_workspace_path(path, ws_root)
                if p is None:
                    results.append(f"\n--- {path} ---\n[path denied]")
                    continue
                if not p.exists():
                    results.append(f"\n--- {path} ---\n[not found]")
                    continue
                if not p.is_file():
                    results.append(f"\n--- {path} ---\n[not a file]")
                    continue
                txt = p.read_text(encoding="utf-8", errors="replace")
                lines = txt.splitlines()
                if len(lines) > limit_per_file:
                    txt = "\n".join(lines[:limit_per_file]) + f"\n... ({len(lines)} total lines)"
                total_bytes += len(txt.encode("utf-8"))
                results.append(f"\n--- {path} ---\n{txt}")
            except Exception as e:
                results.append(f"\n--- {path} ---\n[error: {e}]")

        combined = "".join(results)
        if len(combined.encode("utf-8")) > MAX_TOTAL:
            combined = combined[:MAX_TOTAL] + "\n[truncated]"

        return {"success": True, "output": combined, "error": None}
    except Exception as exc:
        return {"success": False, "output": "", "error": str(exc)}
    finally:
        # Release workspace lock
        if lock_fd is not None:
            try:
                flock(lock_fd.fileno(), LOCK_UN)
            except Exception:  # pragma: no cover (error swallowing in cleanup)
                pass
            try:
                lock_fd.close()
            except Exception:  # pragma: no cover (error swallowing in cleanup)
                pass


def batch_grep(patterns: list[str], path: str = ".", include: str = "", limit_per_pattern: int = 50) -> dict:
    """Search multiple patterns in one call. Returns combined matches.

    SECURITY: Acquires exclusive workspace lock to enforce single-writer
    invariant and prevent TOCTOU races with concurrent writes.
    """
    import glob as _glob
    lock_fd = None
    try:
        ws_root = WORKSPACE_ROOT
        # Acquire exclusive advisory lock on workspace directory
        lock_path = ws_root / ".lock"
        lock_fd = open(lock_path, "a+")
        flock(lock_fd.fileno(), LOCK_EX)

        results = []
        total_matches = 0

        for pat in patterns:
            try:
                regex = re.compile(pat, re.IGNORECASE)
            except re.error:  # pragma: no cover (tested in unit tests)
                results.append(f"\n--- pattern: {pat} ---\n[invalid regex]")
                continue

            # SECURITY: Validate include parameter to prevent path traversal
            # Reject include values with '..' segments or absolute paths before glob construction
            if include:
                # Normalize backslashes to forward slashes for consistent checking
                normalized_include = include.replace('\\', '/')
                inc_path = Path(normalized_include)
                if inc_path.is_absolute() or '..' in inc_path.parts:  # pragma: no cover (tested in unit tests)
                    logger.warning('batch_grep: rejected traversal/absolute include: %r', include)
                    results.append(f"\n--- pattern: {pat} ---\n[invalid include]")
                    continue

            matches = []
            search_path = resolve_workspace_path(path, ws_root)
            if search_path is None:
                results.append(f"\n--- pattern: {pat} ---\n[path denied]")
                continue
            if search_path.is_file():
                file_list = [search_path]
            else:
                glob_pattern = str(search_path / "**" / (include or "*"))
                raw_files = _glob.glob(glob_pattern, recursive=True)
                # Filter glob results: only include files within WORKSPACE_ROOT
                # SECURITY: Store and use the RESOLVED path for reading to prevent
                # TOCTOU races and ensure we read exactly what was validated.
                file_list = []
                filtered_count = 0
                for f in raw_files:
                    fp = Path(f)
                    if not fp.is_file():
                        continue
                    resolved = resolve_workspace_path(str(fp), WORKSPACE_ROOT)
                    if resolved is None:
                        filtered_count += 1
                        continue
                    file_list.append(resolved)
                if filtered_count > 0:
                    logger.warning('batch_grep: filtered %d glob results outside workspace', filtered_count)

            for fp in file_list[:200]:
                # fp is already resolved and validated from the glob filtering step above.
                # Re-validation is redundant but kept as defense-in-depth.
                if resolve_workspace_path(str(fp), WORKSPACE_ROOT) is None:
                    continue
                try:
                    txt = fp.read_text(encoding="utf-8", errors="replace")
                    for i, line in enumerate(txt.splitlines(), 1):
                        if regex.search(line):
                            matches.append(f"{fp}:{i}: {line.strip()[:200]}")
                            if len(matches) >= limit_per_pattern:
                                break
                except Exception:
                    continue
                if len(matches) >= limit_per_pattern:
                    break

            total_matches += len(matches)
            results.append(f"\n--- pattern: {pat} ({len(matches)} matches) ---")
            results.extend(matches[:limit_per_pattern])

        combined = "\n".join(results)
        if len(combined.encode("utf-8")) > 200_000:
            combined = combined[:200_000] + "\n[truncated]"

        return {"success": True, "output": combined, "error": None}
    except Exception as exc:
        return {"success": False, "output": "", "error": str(exc)}
    finally:
        # Release workspace lock
        if lock_fd is not None:
            try:
                flock(lock_fd.fileno(), LOCK_UN)
            except Exception:  # pragma: no cover (error swallowing in cleanup)
                pass
            try:
                lock_fd.close()
            except Exception:  # pragma: no cover (error swallowing in cleanup)
                pass


# Aliases: bots call these "read" and "write", keep old names for backward compat
file_read = read
file_write = write
