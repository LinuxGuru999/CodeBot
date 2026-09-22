#!/usr/bin/env python3
"""Botnet control server — stdlib HTTP API for remote Fly.io management.

Purpose
-------
Small ThreadingHTTPServer that exposes botnet control to an authenticated
client. Runs alongside orchestrator.py on Fly.io (same VM, shared
~/Work/bots/state and ~/Work/bots/logs).

Why not reuse orchestrator.py's HTTP? Orchestrator is a pure controller
with no HTTP. This process is intentionally separate — stateless, fast
to restart, and never touches bot prompts. Fly routes external traffic
only here.

Endpoints
---------
GET  /health                    → {"status":"ok", "bots":...}
GET  /bots                      → list bots with model/interval/status/heartbeat age
GET  /bots/{name}               → single bot status + next_run_in/eff_timeout/risk
GET  /bots/{name}/logs?lines=200 → tail of logs/<name>.log
POST /bots/{name}/restart      → restart via orchestrator helper
POST /bots/{name}/pause        → disable (set .drain per-bot)
POST /bots/{name}/resume       → re-enable
POST /bots/start               → body {"bots":["issues","features"]} or {} for all
POST /bots/stop                → body {"bots": [...] } or {} for all
POST /control/drain            → create state/.drain
POST /control/clear-drain      → remove state/.drain
POST /control/update           → run safe_update.sh --force
GET  /scheduler/status         → versioned redacted ops status (budget, drain,
                                 dead-letter ids, queue/lease counts, paused/disabled,
                                 starvation ages, batch caps, per-model actuals)
GET  /scheduler/events?limit=N → versioned JSONL events, limit clamped to 100
GET  /scheduler/dead-letters   → bounded {id, reason} list (≤20) + count
POST /scheduler/dead-letters/{id}/retry → authenticated idempotent recovery

Auth
----
If CONTROL_TOKEN env is set, require `Authorization: Bearer <token>`.
Fly sets this via `fly secrets set CONTROL_TOKEN=...`.
If CONTROL_TOKEN is unset/empty, the server still rejects all authenticated
endpoints with 401 (fail-closed per Constitution §2).
To test locally, set CONTROL_TOKEN to any value and pass it in requests.

Stdlib-only, single file, no deps beyond orchestrator.py model profiles.
"""
from __future__ import annotations

import email.utils
import hashlib
import hmac
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

def _safe_int_env(key: str, default: int, min_val: int = 1) -> int:
    """Safely parse an integer environment variable with bounds validation.

    Args:
        key: Environment variable name.
        default: Default value if var is missing or invalid.
        min_val: Minimum allowed value (enforced lower bound).

    Returns:
        Parsed integer, clamped to min_val, or default if parsing fails.
    """
    try:
        val = int(os.environ.get(key, str(default)))
        return max(val, min_val)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid value for %s; using default %d", key, default
        )
        return default


# Rate limiting configuration
RATE_LIMIT_MAX_ATTEMPTS = _safe_int_env("RATE_LIMIT_MAX_ATTEMPTS", 5, 1)
RATE_LIMIT_WINDOW_SECONDS = _safe_int_env("RATE_LIMIT_WINDOW_SECONDS", 60, 1)
RATE_LIMIT_COOLDOWN_SECONDS = _safe_int_env("RATE_LIMIT_COOLDOWN_SECONDS", 300, 1)


class RateLimiter:
    """Thread-safe rate limiter for authentication attempts."""

    def __init__(self):
        self._lock = threading.Lock()
        # ip -> list of failure timestamps
        self._failures: dict[str, list[float]] = defaultdict(list)
        # ip -> cooldown until timestamp (blocked)
        self._blocked_until: dict[str, float] = {}

    def is_allowed(self, client_ip: str) -> tuple[bool, str | None]:
        """Check if request from client_ip is allowed.

        Returns (allowed, reason). If blocked, reason explains why.
        """
        now = time.time()
        with self._lock:
            # Check if currently in cooldown
            if client_ip in self._blocked_until:
                if now < self._blocked_until[client_ip]:
                    remaining = int(self._blocked_until[client_ip] - now)
                    return False, f"rate limit exceeded; blocked for {remaining}s (try again later)"
                else:
                    # Cooldown expired, clear block and reset failures
                    del self._blocked_until[client_ip]
                    self._failures[client_ip] = []

            # Clean old failures outside the window
            cutoff = now - RATE_LIMIT_WINDOW_SECONDS
            self._failures[client_ip] = [
                ts for ts in self._failures[client_ip] if ts > cutoff
            ]

            # Check if over limit
            if len(self._failures[client_ip]) >= RATE_LIMIT_MAX_ATTEMPTS:
                # Enter cooldown
                self._blocked_until[client_ip] = now + RATE_LIMIT_COOLDOWN_SECONDS
                return (
                    False,
                    f"rate limit exceeded ({RATE_LIMIT_MAX_ATTEMPTS} attempts in {RATE_LIMIT_WINDOW_SECONDS}s); blocked for {RATE_LIMIT_COOLDOWN_SECONDS}s (try again later)",
                )

            return True, None

    def record_failure(self, client_ip: str) -> None:
        """Record a failed authentication attempt for client_ip."""
        now = time.time()
        with self._lock:
            self._failures[client_ip].append(now)


# Global rate limiter instance for authenticated endpoints
_rate_limiter = RateLimiter()

# Separate rate limiter for /health endpoint to isolate it from auth-failure brute-force.
# This prevents an attacker from triggering rate limits via failed auth attempts that
# would block /health checks (causing orchestrator restart loops).
_health_rate_limiter = RateLimiter()

BOTS_DIR = Path(__file__).resolve().parent
STATE_DIR = BOTS_DIR / "state"
LOGS_DIR = BOTS_DIR / "logs"
ORCH = BOTS_DIR / "orchestrator.py"
SAFE_UPDATE = BOTS_DIR / "safe_update.sh"

CONTROL_TOKEN = os.environ.get("CONTROL_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", os.environ.get("CONTROL_PORT", "8081")))
MAX_LOG_LINES = 2_000
MAX_REQUEST_BYTES = 65_536
REQUEST_TIMEOUT_SECONDS = 15

# Bot name validation pattern: alphanumeric, hyphens, underscores only
BOT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

# Telemetry token from environment; empty means reject all telemetry requests
TELEMETRY_TOKEN = os.environ.get("CODEBOT_TELEMETRY_TOKEN", "").strip()

# Remediation hints for 401 responses — tells operators how to recover
CONTROL_UNAUTHORIZED_HINT = (
    "Set the CONTROL_TOKEN environment variable on the server and pass it as "
    "an 'Authorization: Bearer <token>' header. Example: "
    "export CONTROL_TOKEN=your-secret-token && fly secrets set CONTROL_TOKEN=$CONTROL_TOKEN"
)
TELEMETRY_UNAUTHORIZED_HINT = (
    "Set the CODEBOT_TELEMETRY_TOKEN environment variable "
    "on the server and pass it as an 'Authorization: Bearer <token>' header. "
    "Example: export CODEBOT_TELEMETRY_TOKEN=your-telemetry-token"
)
TELEMETRY_NOT_CONFIGURED_HINT = (
    "CODEBOT_TELEMETRY_TOKEN is not configured on the server. "
    "Set it via environment variable, e.g. "
    "export CODEBOT_TELEMETRY_TOKEN=your-telemetry-token"
)

# Import bot registry and model profiles safely from process_manager.
# This avoids dynamic code execution (exec_module) of orchestrator.py,
# preventing arbitrary code execution vulnerabilities (CB-495709-D068).
from codebot.process_manager import BOT_REGISTRY, MODEL_PROFILES, BotConfig, ModelProfile


def _resolve_control_state_dir() -> Path:
    """Resolve the state dir for PID files, honoring test patches.

    Production path is the module-level ``STATE_DIR`` (``codebot/state``).
    Tests patch ``codebot.control_server.STATE_DIR`` to a tmp dir; the
    helpers below must honor that patch so PID-file behavior is testable.
    """
    try:
        mod = sys.modules.get(__name__)
        sd = getattr(mod, "STATE_DIR", None) if mod is not None else None
        if sd is not None:
            return Path(sd)
    except Exception:
        pass
    return STATE_DIR


def eff_timeout(cfg: BotConfig) -> int:
    """Calculate effective heartbeat timeout for a bot config."""
    prof = MODEL_PROFILES.get(cfg.model)
    if prof:
        return max(int(cfg.interval_seconds * prof.heartbeat_multiplier), cfg.heartbeat_timeout)
    return cfg.heartbeat_timeout


def validate_bot_name(name: str) -> bool:
    """Validate bot name against safe pattern to prevent command injection.

    Args:
        name: The bot name to validate

    Returns:
        True if the name matches ^[a-zA-Z0-9_-]+$, False otherwise
    """
    if not isinstance(name, str):
        return False
    return bool(BOT_NAME_PATTERN.match(name))


def _read_pid_file(name: str) -> int | None:
    """Read PID from state/<name>.pid file.

    Returns None when the file is missing, unreadable, or holds a
    non-numeric payload (treated as corrupt/stale, not an error).
    """
    pid_path = _resolve_control_state_dir() / f"{name}.pid"
    try:
        if pid_path.exists():
            txt = pid_path.read_text(encoding="utf-8").strip()
            if txt.isdigit():
                return int(txt)
    except Exception as e:
        logger.warning("_read_pid_file: error reading PID file for '%s': %s", name, e)
    return None


def _verify_cmdline(pid: int, name: str) -> bool:
    """Verify that PID belongs to a bot api_runner process with the given bot name.

    The real launcher (process_manager._build_popen_args) spawns::

        sys.executable -m codebot.api_runner <bot_name> ...

    whose ``/proc/<pid>/cmdline`` argv decodes to e.g.
    ``['/usr/bin/python3', '-m', 'codebot.api_runner', '<bot>', ...]``.
    Legacy spawn forms used ``['<python>', 'api_runner.py', '<bot>']``.

    Verification therefore accepts BOTH forms, requiring an EXACT
    (not substring) match on the bot name:

    1. Module form: consecutive argv pair ``('codebot.api_runner', name)``
       or a ``'-m'`` flag followed (one slot later) by
       ``'codebot.api_runner'`` with ``name`` as the NEXT argv after the
       module token.  The interpreter check additionally requires argv
       joined text to contain ``'python'`` or the module token itself.
    2. Script form (legacy/tests): consecutive argv pair
       ``('api_runner.py', name)`` whose path basename match is exact,
       plus a ``'python'`` token somewhere in argv.

    A non-python process with a matching cmdline suffix, an extra
    argument between the runner token and the name, or a substring-only
    match (``'my-bot-extra'`` vs ``'my-bot'``) is REJECTED to avoid
    denial-of-service on unrelated processes.
    """
    try:
        cmdline_path = f"/proc/{pid}/cmdline"
        with open(cmdline_path, "rb") as f:
            cmdline_bytes = f.read()
        argv = [arg.decode("utf-8", errors="replace") for arg in cmdline_bytes.split(b"\x00") if arg]
        if not argv:
            return False
        cmdline_lower = " ".join(argv).lower()
        is_python = "python" in cmdline_lower or "codebot.api_runner" in cmdline_lower
        if not is_python:
            return False
        # Form 1: module invocation ``-m codebot.api_runner <name> ...``.
        for i, arg in enumerate(argv):
            base = arg.rsplit("/", 1)[-1] if "/" in arg else arg
            if arg == "codebot.api_runner" or base == "api_runner.py":
                if i + 1 < len(argv) and argv[i + 1] == name:
                    return True
        return False
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _atomic_signal_pid(pid: int, sig: int) -> bool:
    """Send signal to PID atomically using pidfd if available, else os.kill.

    Using ``pidfd_open`` + ``pidfd_send_signal`` (Linux 5.1+, Python 3.9+
    exposes ``os.pidfd_open``; 3.12+ also ``os.pidfd_send_signal``) pins
    the signal to the *process object* the verified ``/proc/<pid>/cmdline``
    belonged to, instead of re-resolving the numeric PID.  When pidfds
    are available the classic verify-then-``os.kill`` TOCTOU window
    (PID exits and its number is recycled between ``_verify_cmdline``
    and signal delivery) is closed: ``pidfd_send_signal`` fails with
    ``ESRCH`` if the original process is gone rather than signalling the
    PID reuser.

    When pidfds are unavailable the code falls back to ``os.kill`` (the
    same semantics as before); PID-file targeting plus cmdline
    verification still prevents regex-based over-matching.
    """
    try:
        # Try using pidfd_open for atomic signaling (Python 3.9+ on Linux 5.1+).
        if hasattr(os, "pidfd_open"):
            try:
                pidfd = os.pidfd_open(pid, 0)
                try:
                    if hasattr(os, "pidfd_send_signal"):
                        os.pidfd_send_signal(pidfd, sig, None, None, 0)
                        return True
                    else:
                        # Python <3.12: pidfd pins the process but we can only
                        # signal via os.kill(pid); the fd at least lets us
                        # detect PID reuse cheaply (fd becomes invalid/ESRCH
                        # on poll) — but kill-by-number remains best-effort.
                        os.kill(pid, sig)
                        return True
                finally:
                    os.close(pidfd)
            except OSError:
                # pidfd_open failed (e.g., process exited, not supported), fall back
                pass

        # Standard fallback
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        raise
    except Exception:
        raise


def _safe_kill_bot_process(name: str, timeout: int = 5) -> tuple[bool, list[int]]:
    """Safely kill bot process using PID file and verified signaling.

    Uses PID file written at startup for exact targeting, eliminating
    TOCTOU races from pgrep/pkill. Verifies cmdline before signaling
    to prevent killing recycled PIDs. Uses pidfd for atomic signaling
    if available.

    Args:
        name: Validated bot name (must pass validate_bot_name first)
        timeout: Timeout for operations (unused in PID-file path, kept for signature compat)

    Returns:
        Tuple of (success, killed_pids) where success indicates no errors occurred
    """
    if not validate_bot_name(name):
        logger.error("_safe_kill_bot_process: invalid bot name rejected: %s", repr(name))
        return False, []

    killed_pids: list[int] = []
    import signal
    
    # Primary path: Read PID from file
    pid = _read_pid_file(name)
    
    if pid is not None:
        # Verify cmdline matches expected bot before killing
        if _verify_cmdline(pid, name):
            try:
                _atomic_signal_pid(pid, signal.SIGTERM)
                logger.info("_safe_kill_bot_process: sent SIGTERM to PID %d from PID file for bot '%s'", pid, name)
                killed_pids.append(pid)
                # Clean up PID file after successful signal
                try:
                    pid_file = STATE_DIR / f"{name}.pid"
                    if pid_file.exists():
                        pid_file.unlink()
                except OSError:
                    pass
            except ProcessLookupError:
                logger.info("_safe_kill_bot_process: PID %d from PID file already exited for bot '%s'", pid, name)
                # Clean up stale PID file
                try:
                    pid_file = STATE_DIR / f"{name}.pid"
                    if pid_file.exists():
                        pid_file.unlink()
                except OSError:
                    pass
            except PermissionError:
                logger.warning("_safe_kill_bot_process: permission denied killing PID %d for bot '%s'", pid, name)
            except Exception as e:
                logger.warning("_safe_kill_bot_process: error killing PID %d for bot '%s': %s", pid, name, e)
        else:
            logger.warning("_safe_kill_bot_process: PID %d from PID file failed cmdline verification for bot '%s'", pid, name)
            # Stale PID file pointing to wrong process? Clean it up to be safe.
            try:
                pid_file = STATE_DIR / f"{name}.pid"
                if pid_file.exists():
                    pid_file.unlink()
            except OSError:
                pass
    else:
        # No PID file found. Do NOT fall back to pgrep to avoid broad matching.
        logger.warning("_safe_kill_bot_process: No PID file found for bot '%s'. Skipping kill.", name)

    return True, killed_pids


def _read_orchestrator_pid_file() -> int | None:
    """Read orchestrator PID from state/.orchestrator.pid file."""
    pid_path = STATE_DIR / ".orchestrator.pid"
    try:
        if pid_path.exists():
            txt = pid_path.read_text(encoding="utf-8").strip()
            if txt.isdigit():
                return int(txt)
    except Exception as e:
        logger.warning("_read_orchestrator_pid_file: error reading PID file: %s", e)
    return None


def _verify_orchestrator_cmdline(pid: int) -> bool:
    """Verify that PID belongs to orchestrator.py.

    Accepts both the module form (``-m codebot...`` running orchestrator
    code is NOT expected; orchestrator runs as a script) and the script
    form where some argv element's basename is exactly
    ``'orchestrator.py'``, plus a python/module token in the cmdline.
    Path normalization uses basename comparison so ``/a/b/orchestrator.py``
    matches but ``fake_orchestrator.py`` or ``orchestrator.py.bak`` do not.
    """
    try:
        cmdline_path = f"/proc/{pid}/cmdline"
        with open(cmdline_path, "rb") as f:
            cmdline_bytes = f.read()
        argv = [arg.decode("utf-8", errors="replace") for arg in cmdline_bytes.split(b"\x00") if arg]
        if not argv:
            return False

        # Check if any argv element's basename is exactly 'orchestrator.py'
        for arg in argv:
            basename = arg.rsplit("/", 1)[-1] if "/" in arg else arg
            if basename == "orchestrator.py":
                # Additional safety: ensure it's a python process
                cmdline_str = " ".join(argv).lower()
                if "python" in cmdline_str:
                    return True
                else:
                    logger.warning(
                        "_verify_orchestrator_cmdline: PID %d has orchestrator.py but not python process", pid
                    )
                    return False
        return False
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _safe_kill_orchestrator(timeout: int = 5) -> tuple[bool, list[int]]:
    """Safely kill orchestrator process using PID file and verified signaling.

    Uses PID file written at startup for exact targeting, eliminating
    TOCTOU races from pgrep. Verifies cmdline before signaling.

    Args:
        timeout: Timeout for operations (unused in PID-file path, kept for signature compat)

    Returns:
        Tuple of (success, killed_pids) where success indicates no errors occurred
    """
    killed_pids: list[int] = []
    import signal

    pid = _read_orchestrator_pid_file()
    
    if pid is not None:
        if _verify_orchestrator_cmdline(pid):
            try:
                _atomic_signal_pid(pid, signal.SIGTERM)
                logger.info("_safe_kill_orchestrator: sent SIGTERM to verified PID %d from PID file", pid)
                killed_pids.append(pid)
                # Clean up PID file
                try:
                    pid_file = STATE_DIR / ".orchestrator.pid"
                    if pid_file.exists():
                        pid_file.unlink()
                except OSError:
                    pass
            except ProcessLookupError:
                logger.info("_safe_kill_orchestrator: PID %d from PID file already exited", pid)
                try:
                    pid_file = STATE_DIR / ".orchestrator.pid"
                    if pid_file.exists():
                        pid_file.unlink()
                except OSError:
                    pass
            except PermissionError:
                logger.warning("_safe_kill_orchestrator: permission denied killing PID %d", pid)
            except Exception as e:
                logger.warning("_safe_kill_orchestrator: error killing PID %d: %s", pid, e)
        else:
            logger.warning("_safe_kill_orchestrator: PID %d from PID file failed cmdline verification", pid)
            # Stale PID file? Clean it up.
            try:
                pid_file = STATE_DIR / ".orchestrator.pid"
                if pid_file.exists():
                    pid_file.unlink()
            except OSError:
                pass
    else:
        logger.warning("_safe_kill_orchestrator: No PID file found for orchestrator. Skipping kill.")

    return True, killed_pids


def _safe_kill_process(pid: int, expected_cmdline: str, grace_period: float = 5.0) -> tuple[bool, int | None]:
    """Safely kill a process by PID with /proc/cmdline verification and SIGTERM/SIGKILL escalation.

    Verifies that the target PID's cmdline matches expected_cmdline before sending signals.
    Sends SIGTERM first, waits up to grace_period seconds, then escalates to SIGKILL if needed.
    Re-verifies cmdline before each signal to prevent killing recycled PIDs.

    Args:
        pid: Process ID to terminate (must be positive integer)
        expected_cmdline: Expected substring/suffix of /proc/<pid>/cmdline for verification
        grace_period: Seconds to wait between SIGTERM and SIGKILL

    Returns:
        Tuple of (success, killed_pid). success=True only if process is confirmed gone.
        killed_pid is the targeted PID or None if aborted before signaling.
    """
    import signal as _signal

    # Validate inputs
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        logger.warning("_safe_kill_process: invalid pid rejected: %r", pid)
        return False, None
    if not isinstance(expected_cmdline, str) or not expected_cmdline:
        logger.warning("_safe_kill_process: invalid expected_cmdline rejected: %r", expected_cmdline)
        return False, None

    def _read_cmdline(p: int) -> str | None:
        try:
            with open(f"/proc/{p}/cmdline", "rb") as f:
                data = f.read(4096)
            return data.decode("utf-8", errors="replace").replace("\x00", " ").strip()
        except FileNotFoundError:
            return None
        except PermissionError:
            logger.warning("_safe_kill_process: permission denied reading /proc/%d/cmdline", p)
            return None
        except Exception as e:
            logger.warning("_safe_kill_process: error reading /proc/%d/cmdline: %s", p, e)
            return None

    def _is_alive(p: int) -> bool:
        try:
            os.kill(p, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we lack permission; treat as alive
            return True
        except Exception:
            return False

    # Initial cmdline verification
    cmdline = _read_cmdline(pid)
    if cmdline is None:
        logger.info("_safe_kill_process: PID %d does not exist or unreadable, nothing to kill", pid)
        return False, None
    if expected_cmdline not in cmdline:
        logger.warning(
            "_safe_kill_process: PID %d cmdline mismatch (expected %r in %r), aborting",
            pid, expected_cmdline[:200], cmdline[:200]
        )
        return False, None

    # Send SIGTERM
    try:
        os.kill(pid, _signal.SIGTERM)
        logger.info("_safe_kill_process: sent SIGTERM to PID %d", pid)
    except ProcessLookupError:
        logger.info("_safe_kill_process: PID %d already exited before SIGTERM", pid)
        return True, pid
    except PermissionError:
        logger.warning("_safe_kill_process: permission denied sending SIGTERM to PID %d", pid)
        return False, pid
    except Exception as e:
        logger.warning("_safe_kill_process: error sending SIGTERM to PID %d: %s", pid, e)
        return False, pid

    # Grace period polling
    elapsed = 0.0
    poll_interval = min(0.2, grace_period / 10.0) if grace_period > 0 else 0.2
    while elapsed < grace_period:
        time.sleep(poll_interval)
        elapsed += poll_interval
        if not _is_alive(pid):
            logger.info("_safe_kill_process: PID %d exited after SIGTERM", pid)
            return True, pid

    # Re-verify cmdline before SIGKILL to avoid killing recycled PID
    cmdline_after = _read_cmdline(pid)
    if cmdline_after is None:
        logger.info("_safe_kill_process: PID %d exited during grace period", pid)
        return True, pid
    if expected_cmdline not in cmdline_after:
        logger.warning(
            "_safe_kill_process: PID %d cmdline changed during grace period (expected %r in %r), aborting SIGKILL",
            pid, expected_cmdline[:200], cmdline_after[:200]
        )
        return False, pid

    # Escalate to SIGKILL
    try:
        os.kill(pid, _signal.SIGKILL)
        logger.warning("_safe_kill_process: escalated to SIGKILL for PID %d", pid)
    except ProcessLookupError:
        logger.info("_safe_kill_process: PID %d exited before SIGKILL", pid)
        return True, pid
    except PermissionError:
        logger.warning("_safe_kill_process: permission denied sending SIGKILL to PID %d", pid)
        return False, pid
    except Exception as e:
        logger.warning("_safe_kill_process: error sending SIGKILL to PID %d: %s", pid, e)
        return False, pid

    # Brief wait after SIGKILL
    time.sleep(min(0.5, grace_period * 0.1))
    if not _is_alive(pid):
        logger.info("_safe_kill_process: PID %d terminated after SIGKILL", pid)
        return True, pid

    logger.warning("_safe_kill_process: PID %d still alive after SIGKILL (possible zombie)", pid)
    return False, pid


def heartbeat_age(name: str) -> float | None:
    # Defense-in-depth (CB-3E4571EFBE42): validate bot name before using it
    # in filesystem path construction to block path traversal.
    if not validate_bot_name(name):
        logger.warning("heartbeat_age: rejected invalid bot name: %s", repr(name)[:100])
        return None
    p = STATE_DIR / f"{name}.heartbeat"
    try:
        v = float(p.read_text().strip().split()[0])
        return time.time() - v
    except Exception:
        return None


def log_tail(name: str, lines: int = 200) -> str:
    # Defense-in-depth: validate name parameter before path construction
    # Reject names containing '/' or '..' and validate against safe pattern
    if not isinstance(name, str):
        return ""
    if '/' in name or '..' in name:
        logger.warning("log_tail: rejected path traversal attempt: %s", repr(name))
        return ""
    if not BOT_NAME_PATTERN.match(name):
        logger.warning("log_tail: rejected invalid bot name: %s", repr(name))
        return ""
    # Additional safeguard: use os.path.basename() to strip any directory components
    safe_name = os.path.basename(name)
    if not safe_name or safe_name != name:
        logger.warning("log_tail: rejected name with path components: %s", repr(name))
        return ""
    p = LOGS_DIR / f"{safe_name}.log"
    if not p.exists():
        return ""
    try:
        out = subprocess.run(["tail", "-n", str(lines), str(p)], capture_output=True, text=True, timeout=5)
        return out.stdout if out.returncode == 0 else p.read_text()[-8000:]
    except Exception:
        try:
            return p.read_text()[-8000:]
        except Exception:
            return ""


def bot_status(name: str) -> dict:
    # Defense-in-depth (CB-3E4571EFBE42): bot names interpolated into
    # pgrep -f (regex) patterns must be allowlist-validated first.
    # Invalid names are neutralized to a safe sentinel (still re.escape()d
    # below) so no crafted regex/metacharacters can ever reach subprocess.
    if not validate_bot_name(name):
        logger.warning("bot_status: rejected invalid bot name: %s", repr(name)[:100])
        name = "invalid-bot-name-rejected"
    cfg = next((c for c in BOT_REGISTRY if c.name == name), None)
    hb = heartbeat_age(name)
    state_file = STATE_DIR / f"{name}.state.json"
    state = {}
    try:
        if state_file.exists():
            state = json.loads(state_file.read_text())
    except Exception:
        pass
    # process check via pgrep
    running = False
    pid = None
    try:
        # Defense-in-depth (CB-3E4571EFBE42): escape bot name to prevent
        # regex injection in pgrep pattern; name was allowlist-validated above.
        escaped_name = re.escape(name)
        # Anchor with $ so "bot" cannot match "bot-extra" via regex prefix.
        ps = subprocess.run(["pgrep", "-f", f"api_runner\\.py {escaped_name}$"], capture_output=True, text=True, timeout=3)
        if ps.stdout.strip():
            running = True
            pid = ps.stdout.strip().split()[0]
    except Exception:
        pass
    # orchestrator process check
    orch_running = False
    try:
        ps2 = subprocess.run(["pgrep", "-f", "orchestrator.py"], capture_output=True, text=True, timeout=3)
        orch_running = bool(ps2.stdout.strip())
    except Exception:
        pass
    eff = eff_timeout(cfg) if cfg else None
    prof = MODEL_PROFILES.get(cfg.model) if cfg else None
    nxt = None
    try:
        nxt = float(state.get("next_run_at", 0)) - time.time() if state.get("next_run_at") else None
        if nxt is not None and nxt < 0:
            nxt = 0
    except Exception:
        nxt = None
    status_data = {
        "name": name,
        "model": cfg.model if cfg else None,
        "interval_seconds": cfg.interval_seconds if cfg else None,
        "effective_timeout": eff,
        "risk": prof.lockup_risk if prof else None,
        "heartbeat_age_seconds": round(hb, 1) if hb is not None else None,
        "running": running,
        "pid": pid,
        "state": state.get("status"),
        "next_run_in_seconds": round(nxt, 1) if nxt is not None else None,
        "restart_count": state.get("restart_count"),
        "orchestrator_running": orch_running,
    }
    computed_at = time.time()
    # Stable ETag: hash of serialized status data for conditional requests
    try:
        etag_source = json.dumps(status_data, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        etag_source = name
    etag = hashlib.sha256(etag_source.encode()).hexdigest()[:32]
    status_data["computed_at"] = computed_at
    status_data["etag"] = etag
    return status_data


def scheduler_status() -> dict:
    budget_state = "budget-unknown"
    budget_total = None
    budget_day = None
    per_model: dict[str, dict[str, int]] = {}
    try:
        try:
            from codebot.token_budget import current_day_utc, day_total, get_budget_state
        except ImportError:
            from bots.token_budget import current_day_utc, day_total, get_budget_state
        day = current_day_utc()
        budget_day = day
        ledger_path = STATE_DIR / "token_ledger.json"
        total = day_total(day, path=ledger_path)
        budget_total = int(total) if isinstance(total, (int, float)) else 0
        budget_state = get_budget_state(total)
        try:
            import json as _json

            ledger = _json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
            if isinstance(ledger, dict) and ledger.get("day_utc") == day:
                by_model = ledger.get("by_model")
                if isinstance(by_model, dict):
                    for name, row in list(by_model.items())[:32]:
                        if not isinstance(name, str) or not isinstance(row, dict):
                            continue
                        try:
                            per_model[name] = {
                                "prompt_actual": int(row.get("prompt_actual", 0) or 0),
                                "completion_actual": int(row.get("completion_actual", 0) or 0),
                            }
                        except (TypeError, ValueError):
                            continue
                    bounded_model: dict[str, dict[str, int]] = {}
                    for m_name, m_vals in list(per_model.items())[:32]:
                        if not isinstance(m_name, str) or not isinstance(m_vals, dict):
                            continue
                        if len(m_name) > 64:
                            m_name = m_name[:64]
                        if any(s in m_name.lower() for s in ("token", "secret", "password", "api_key")):
                            continue
                        try:
                            bounded_model[m_name] = {
                                "prompt_actual": max(0, min(int(m_vals.get("prompt_actual", 0) or 0), 10_000_000_000)),
                                "completion_actual": max(0, min(int(m_vals.get("completion_actual", 0) or 0), 10_000_000_000)),
                            }
                        except (TypeError, ValueError):
                            continue
                    per_model = bounded_model
        except (OSError, ValueError):
            per_model = {}
    except (ImportError, OSError, ValueError):
        pass
    dead_letter_count = 0
    dead_letter_ids: list[str] = []
    queue_count = 0
    lease_count = 0
    try:
        try:
            from codebot.lease_state import dead_letters
        except ImportError:
            from bots.lease_state import dead_letters
        letters = dead_letters(STATE_DIR)
        if isinstance(letters, list):
            dead_letter_count = len(letters)
            for letter in letters[:20]:
                if isinstance(letter, dict) and isinstance(letter.get("id"), str):
                    dead_letter_ids.append(letter["id"][:64])
    except (ImportError, OSError, ValueError):
        pass
    try:
        leases_path = STATE_DIR / "leases.json"
        if leases_path.exists():
            try:
                import json as _json2

                raw = _json2.loads(leases_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    leases = raw.get("leases")
                    attempts = raw.get("attempts")
                    if isinstance(leases, dict):
                        lease_count = len(leases)
                    if isinstance(attempts, dict):
                        queue_count = len(attempts)
            except (OSError, ValueError):
                pass
    except (OSError, ValueError):
        pass
    paused_count = 0
    paused_bots: list[str] = []
    disabled_count = 0
    disabled_bots: list[str] = []
    disabled_details: list[dict] = []
    starved: list[dict] = []
    try:
        import json as _json3

        oldest_hb: float | None = None
        per_bot_age: list[tuple[str, float]] = []
        for cfg in BOT_REGISTRY:
            name = cfg.name
            if (STATE_DIR / f"{name}.paused").exists():
                paused_count += 1
                if len(paused_bots) < 20:
                    paused_bots.append(name)
            hb_age = heartbeat_age(name)
            if hb_age is not None:
                per_bot_age.append((name, hb_age))
                if oldest_hb is None or hb_age > oldest_hb:
                    oldest_hb = hb_age
            state_file = STATE_DIR / f"{name}.state.json"
            try:
                if state_file.exists():
                    st = _json3.loads(state_file.read_text(encoding="utf-8"))
                    if isinstance(st, dict) and st.get("status") in ("disabled", "error-disabled"):
                        disabled_count += 1
                        if len(disabled_bots) < 20:
                            disabled_bots.append(name)
                        # Why disabled reason as bounded string: expose operator-visible
                        # status/reason without raw prompts or secrets; truncation enforces caps
                        if len(disabled_details) < 20:
                            raw_status = str(st.get("status", "disabled"))[:64]
                            raw_reason = st.get("reason") or st.get("error") or st.get("disabled_reason") or ""
                            reason_txt = str(raw_reason)[:200] if raw_reason else raw_status
                            try:
                                try:
                                    from codebot.event_log import sanitize_data as _san
                                except ImportError:
                                    from bots.event_log import sanitize_data as _san
                                clean = _san({"reason": reason_txt})
                                reason_txt = str(clean.get("reason", reason_txt))[:256]
                            except Exception:
                                reason_txt = reason_txt[:256]
                            disabled_details.append({"bot": name[:64], "reason": reason_txt})
            except (OSError, ValueError):
                pass
        per_bot_age.sort(key=lambda kv: kv[1], reverse=True)
        for name, age in per_bot_age[:5]:
            try:
                starved.append({"bot": name, "starvation_age_seconds": round(float(age), 1)})
            except (TypeError, ValueError):
                continue
    except (OSError, ValueError):
        pass
    batch_utilization: dict[str, int] = {"max_per_batch": 5, "max_batches": 2, "stagger_s": 20}
    return {
        "version": 1,
        "budget_state": budget_state,
        "budget_day": budget_day,
        "budget_total_actual": budget_total,
        "per_model_actual": per_model,
        "drain": (STATE_DIR / ".drain").exists(),
        "dead_letter_count": dead_letter_count,
        "dead_letter_ids": dead_letter_ids,
        "queue_tracked": queue_count,
        "lease_active": lease_count,
        "paused_count": paused_count,
        "paused_bots": paused_bots,
        "disabled_count": disabled_count,
        "disabled_bots": disabled_bots,
        "disabled_details": disabled_details,
        "starved_oldest": starved[0] if starved else None,
        "starved_top": starved,
        "batch_utilization": batch_utilization,
    }


def _check_budget_alerts(budget_pct: float, budget_state: str) -> list[dict]:
    """Check budget thresholds and return alert list.

    Alerts trigger at 80% (warn) and 90% (critical) of daily budget cap.
    Returns list of alert dicts with threshold, level, message, and timestamp.
    """
    alerts: list[dict] = []
    now = time.time()
    if budget_pct >= 90:
        alerts.append({
            "threshold": 90,
            "level": "critical",
            "message": f"Daily budget at {budget_pct:.1f}% — shedding tier-3 work",
            "ts": now,
        })
    elif budget_pct >= 80:
        alerts.append({
            "threshold": 80,
            "level": "warn",
            "message": f"Daily budget at {budget_pct:.1f}% — approaching limit",
            "ts": now,
        })
    # Also check budget_state for additional context
    if budget_state == "stop":
        alerts.append({
            "threshold": 100,
            "level": "critical",
            "message": "Daily budget exhausted — all work stopped",
            "ts": now,
        })
    return alerts


def economics_budget_status() -> dict:
    """Return current daily budget status from token_budget module.

    Returns dict with: budget_cap, budget_used, budget_remaining,
    budget_pct, budget_state, day_utc, per_model_actual.
    Follows same try/except ImportError pattern as scheduler_status().
    """
    budget_cap = 4_000_000_000  # Default CAP
    budget_used = 0
    budget_state = "budget-unknown"
    day_utc = None
    per_model: dict[str, dict[str, int]] = {}

    try:
        try:
            from codebot.token_budget import current_day_utc, day_total, get_budget_state, CAP
        except ImportError:
            from bots.token_budget import current_day_utc, day_total, get_budget_state, CAP

        budget_cap = CAP
        day_utc = current_day_utc()
        ledger_path = STATE_DIR / "token_ledger.json"
        total = day_total(day_utc, path=ledger_path)
        budget_used = int(total) if isinstance(total, (int, float)) else 0
        budget_state = get_budget_state(budget_used, budget_cap)

        # Read per-model actuals from ledger
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
            if isinstance(ledger, dict) and ledger.get("day_utc") == day_utc:
                by_model = ledger.get("by_model")
                if isinstance(by_model, dict):
                    for name, row in list(by_model.items())[:32]:
                        if not isinstance(name, str) or not isinstance(row, dict):
                            continue
                        try:
                            per_model[name] = {
                                "prompt_actual": int(row.get("prompt_actual", 0) or 0),
                                "completion_actual": int(row.get("completion_actual", 0) or 0),
                            }
                        except (TypeError, ValueError):
                            continue
        except (OSError, ValueError):
            pass
    except (ImportError, OSError, ValueError):
        pass

    budget_remaining = max(0, budget_cap - budget_used)
    budget_pct = (budget_used / budget_cap * 100) if budget_cap > 0 else 0.0

    return {
        "budget_cap": budget_cap,
        "budget_used": budget_used,
        "budget_remaining": budget_remaining,
        "budget_pct": round(budget_pct, 2),
        "budget_state": budget_state,
        "day_utc": day_utc,
        "per_model_actual": per_model,
    }


def economics_summary() -> dict:
    """Return combined economics summary: budget status + fleet cost data.

    Combines token_budget data with CostTracker.build_summary() and
    pricing_table.calculate_cost_usd() for USD conversion.
    Includes budget alerts at 80%/90% thresholds.
    """
    # Get budget status
    budget_data = economics_budget_status()
    budget_pct = budget_data.get("budget_pct", 0.0)
    budget_state = budget_data.get("budget_state", "budget-unknown")

    # Check alerts
    alerts = _check_budget_alerts(budget_pct, budget_state)

    # Log alerts if any triggered
    for alert in alerts:
        level = alert.get("level", "warn")
        msg = alert.get("message", "")
        if level == "critical":
            logger.warning("ECONOMICS ALERT [CRITICAL]: %s", msg)
        else:
            logger.info("ECONOMICS ALERT [%s]: %s", level.upper(), msg)

    # Get fleet cost data from CostTracker
    fleet_totals = {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "ticket_count": 0, "total_usd": 0.0}
    by_model: dict[str, dict] = {}
    by_day: dict[str, int] = {}

    try:
        try:
            from codebot.cost_tracker import CostTracker
        except ImportError:
            from bots.cost_tracker import CostTracker

        tracker = CostTracker(STATE_DIR)
        summary = tracker.build_summary()

        ft = summary.get("fleet_totals", {})
        fleet_totals["total_tokens"] = int(ft.get("total_tokens", 0))
        fleet_totals["prompt_tokens"] = int(ft.get("prompt_tokens", 0))
        fleet_totals["completion_tokens"] = int(ft.get("completion_tokens", 0))
        fleet_totals["ticket_count"] = int(ft.get("ticket_count", 0))

        # Calculate USD costs per model using pricing_table
        try:
            try:
                from codebot.pricing_table import calculate_cost_usd
            except ImportError:
                from bots.pricing_table import calculate_cost_usd

            tickets = summary.get("tickets", {})
            model_tokens: dict[str, dict[str, int]] = {}
            for tid, tdata in tickets.items():
                models = tdata.get("models", [])
                prompt = int(tdata.get("prompt_tokens", 0))
                completion = int(tdata.get("completion_tokens", 0))
                # Attribute tokens to first model listed (simplified)
                if models:
                    model_name = models[0] if isinstance(models, list) and models else "unknown"
                else:
                    model_name = "unknown"
                if model_name not in model_tokens:
                    model_tokens[model_name] = {"prompt": 0, "completion": 0}
                model_tokens[model_name]["prompt"] += prompt
                model_tokens[model_name]["completion"] += completion

            total_usd = 0.0
            for model_name, tokens in model_tokens.items():
                usd = calculate_cost_usd(model_name, tokens["prompt"], tokens["completion"])
                by_model[model_name] = {
                    "tokens": tokens["prompt"] + tokens["completion"],
                    "usd": round(usd, 6),
                }
                total_usd += usd
            fleet_totals["total_usd"] = round(total_usd, 6)
        except (ImportError, OSError, ValueError):
            pass

    except (ImportError, OSError, ValueError):
        pass

    return {
        "version": 1,
        "generated_at": time.time(),
        "budget": {
            "cap": budget_data.get("budget_cap"),
            "used": budget_data.get("budget_used"),
            "remaining": budget_data.get("budget_remaining"),
            "pct": budget_data.get("budget_pct"),
            "state": budget_data.get("budget_state"),
            "day": budget_data.get("day_utc"),
        },
        "fleet": fleet_totals,
        "by_model": by_model,
        "by_day": by_day,
        "alerts": alerts,
    }


def retry_dead_letter(item_id: str) -> dict:
    try:
        try:
            from codebot.lease_state import retry_dead_letter as retry
        except ImportError:
            from bots.lease_state import retry_dead_letter as retry
        result = retry(STATE_DIR, item_id)
        if result["status"] == "retried":
            try:
                try:
                    from codebot.event_log import append_event
                except ImportError:
                    from bots.event_log import append_event
                append_event(STATE_DIR, "dead-letter-retry", {"id": item_id})
            except (ImportError, OSError, ValueError):
                pass
        return result
    except (ImportError, OSError, ValueError):
        return {"status": "unavailable", "id": item_id}


class ControlHandler(BaseHTTPRequestHandler):
    def _is_loopback_client(self) -> bool:
        return bool(self.client_address and self.client_address[0] in {"127.0.0.1", "::1"})

    def _auth(self) -> bool | None:
        """Validate Bearer token via Authorization header with rate limiting.

        Fail-closed: when CONTROL_TOKEN is unset/empty, reject all requests
        (Constitution §2: no implicit trust at auth boundaries).
        Rate-limited: excessive failed attempts trigger 429 responses.

        Returns:
            True  - authenticated successfully
            False - authentication failed (caller must send 401)
            None  - rate limited (429 already sent, caller must return immediately)
        """
        # Get client IP for rate limiting
        client_ip = self.client_address[0] if self.client_address else "unknown"

        # Check rate limit before processing auth
        allowed, reason = _rate_limiter.is_allowed(client_ip)
        if not allowed:
            logger.warning("Rate limit exceeded for %s: %s", client_ip, reason)
            retry_after = str(RATE_LIMIT_COOLDOWN_SECONDS)
            self._json(429, {"error": "too many requests", "reason": reason}, extra_headers={"Retry-After": retry_after})
            return None

        if not CONTROL_TOKEN:
            logger.critical(
                "SECURITY: CONTROL_TOKEN is not set — rejecting all authenticated requests. "
                "Set CONTROL_TOKEN env var to enable API access. "
                "This is a fail-closed security measure."
            )
            return False
        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {CONTROL_TOKEN}"
        result = hmac.compare_digest(auth.strip(), expected)
        if not result:
            # Record failed auth attempt for rate limiting
            _rate_limiter.record_failure(client_ip)
            logger.warning("Authentication failed for %s", client_ip)
        return result

    def _json_304(self, etag: str) -> None:
        """Send 304 Not Modified response with ETag."""
        self.send_response(304)
        self.send_header("ETag", f'"{etag}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def _json(self, code: int, obj: dict | list, extra_headers: dict | None = None) -> None:
        """Send JSON response with security headers (Constitution §2)."""
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # Security headers per Constitution §2
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json_with_cache_headers(self, code: int, obj: dict | list, etag: str, last_modified_ts: float) -> None:
        """Send JSON response with ETag/Last-Modified cache headers."""
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", f'"{etag}"')
        self.send_header("Last-Modified", email.utils.formatdate(timeval=last_modified_ts, localtime=False, usegmt=True))
        self.send_header("Cache-Control", "no-cache")
        # Security headers per Constitution §2
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> tuple[dict | None, int | None, str | None]:
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            return {}, None, None
        try:
            length = int(raw_length)
        except ValueError:
            return None, 400, "Content-Length must be an integer"
        if length < 0:
            return None, 400, "Content-Length must not be negative"
        if length > MAX_REQUEST_BYTES:
            return None, 413, "request body too large"
        try:
            self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)
            raw = self.rfile.read(length)
        except (OSError, TimeoutError, socket.timeout):
            return None, 408, "request body read timed out"
        try:
            body = json.loads(raw.decode()) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, 400, "request body must be valid JSON"
        if not isinstance(body, dict):
            return None, 400, "request body must be a JSON object"
        return body, None, None

    def _get_destructive_preview(self, path: str, body: dict) -> dict:
        """Return a preview of what a destructive action would do without executing it.

        Args:
            path: The endpoint path being called
            body: The request body containing parameters like 'bots'

        Returns:
            A dict describing what would happen
        """
        preview = {"action": path, "would_execute": True}

        if path in ("/bots/stop", "/api/bots/stop", "/control/stop"):
            bots = body.get("bots")
            if bots:
                preview["description"] = f"Would stop specified bots: {bots}"
                preview["affected_bots"] = bots
            else:
                preview["description"] = "Would stop ALL bots and orchestrator"
                preview["affected_bots"] = "all"
                preview["warning"] = "This will halt the entire fleet"

        elif path in ("/control/drain", "/api/control/drain"):
            preview["description"] = "Would create .drain file, causing all bots to gracefully shut down after current task"
            preview["recovery"] = "Run clear-drain endpoint or delete state/.drain file to resume operations"
            preview["undo_command"] = "python3 control_client.py clear-drain"

        elif path in ("/control/update", "/api/control/update"):
            preview["description"] = "Would run safe_update.sh --force to update the codebase"
            preview["warning"] = "This may restart services and interrupt running tasks"
            preview["recovery"] = "Git revert can restore previous version if needed"

        return preview

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # /health is public — Fly's http_service.checks GETs /health without Bearer.
        # Keep it cheap: no auth, no bot scan, just ok + timestamp.
        # But enforce rate limiting to prevent abuse (Constitution §2).
        if path in ("/health", "/api/health"):
            client_ip = self.client_address[0] if self.client_address else "unknown"
            # Use separate rate limiter to isolate health checks from auth brute-force
            allowed, reason = _health_rate_limiter.is_allowed(client_ip)
            if not allowed:
                logger.warning("Rate limit exceeded for /health from %s: %s", client_ip, reason)
                retry_after = str(RATE_LIMIT_COOLDOWN_SECONDS)
                self._json(429, {"error": "too many requests", "reason": reason}, extra_headers={"Retry-After": retry_after})
                return
            self._json(200, {"status": "ok", "time": time.time()})
            return

        if path == "/dashboard":
            from codebot.dashboard import dashboard_page
            self._html(200, dashboard_page())
            return

        if path == "/tickets":
            from codebot.dashboard import ticket_explorer_page
            self._html(200, ticket_explorer_page())
            return

        if path in ("/dashboard/snapshot", "/api/dashboard/snapshot") and self._is_loopback_client():
            from codebot.dashboard import live_snapshot
            self._json(200, dict(live_snapshot(BOTS_DIR.parent)))
            return

        if path in ("/tickets/snapshot", "/api/tickets/snapshot") and self._is_loopback_client():
            from codebot.dashboard import ticket_explorer_snapshot
            self._json(200, dict(ticket_explorer_snapshot(
                BOTS_DIR.parent,
                state=qs.get("state", [""])[0],
                query=qs.get("query", [""])[0],
                offset=qs.get("offset", [0])[0],
                limit=qs.get("limit", [100])[0],
            )))
            return

        if path in ("/tickets/detail", "/api/tickets/detail") and self._is_loopback_client():
            from codebot.dashboard import ticket_explorer_detail
            detail = ticket_explorer_detail(BOTS_DIR.parent, qs.get("id", [""])[0])
            self._json(200 if detail else 404, detail or {"error": "ticket not found"})
            return

        auth_result = self._auth()
        if auth_result is None:
            return  # Rate limited, response already sent
        if not auth_result:
            self._json(401, {"error": "unauthorized", "hint": CONTROL_UNAUTHORIZED_HINT})
            return

        if path in ("/bots", "/api/bots"):
            self._json(200, [bot_status(c.name) for c in BOT_REGISTRY])
            return

        if path in ("/dashboard/snapshot", "/api/dashboard/snapshot"):
            from codebot.dashboard import live_snapshot
            self._json(200, dict(live_snapshot(BOTS_DIR.parent)))
            return

        if path in ("/tickets/snapshot", "/api/tickets/snapshot"):
            from codebot.dashboard import ticket_explorer_snapshot
            self._json(200, dict(ticket_explorer_snapshot(
                BOTS_DIR.parent,
                state=qs.get("state", [""])[0],
                query=qs.get("query", [""])[0],
                offset=qs.get("offset", [0])[0],
                limit=qs.get("limit", [100])[0],
            )))
            return

        if path in ("/tickets/detail", "/api/tickets/detail"):
            from codebot.dashboard import ticket_explorer_detail
            detail = ticket_explorer_detail(BOTS_DIR.parent, qs.get("id", [""])[0])
            self._json(200 if detail else 404, detail or {"error": "ticket not found"})
            return

        if path in ("/scheduler/status", "/api/scheduler/status"):
            self._json(200, scheduler_status())
            return

        if path in ("/scheduler/events", "/api/scheduler/events"):
            try:
                limit_raw = qs.get("limit", ["100"])[0]
            except (IndexError, AttributeError):
                limit_raw = "100"
            try:
                limit = int(limit_raw)
            except (TypeError, ValueError):
                self._json(400, {"error": "limit must be an integer"})
                return
            try:
                event_type = qs.get("type", [None])[0]
            except (IndexError, AttributeError):
                event_type = None
            if event_type is not None and not isinstance(event_type, str):
                event_type = str(event_type)
            if event_type is not None and len(event_type) > 64:
                event_type = event_type[:64]
            try:
                try:
                    from codebot.event_log import MAX_EVENTS as _MAX_EVENTS, read_events, sanitize_data
                except ImportError:
                    from bots.event_log import MAX_EVENTS as _MAX_EVENTS, read_events, sanitize_data
                bounded = max(1, min(limit, _MAX_EVENTS))
                if event_type:
                    records = read_events(STATE_DIR, limit=_MAX_EVENTS)
                    filtered = [r for r in records if isinstance(r, dict) and r.get("type") == event_type]
                    records = filtered[-bounded:] if len(filtered) > bounded else filtered
                else:
                    records = read_events(STATE_DIR, limit=bounded)[:bounded]
                safe_events = []
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    data = record.get("data")
                    safe_events.append(
                        {
                            "version": 1,
                            "type": record.get("type"),
                            "ts": record.get("ts"),
                            "data": sanitize_data(data if isinstance(data, dict) else {}),
                        }
                    )
                payload = {"version": 1, "limit": bounded, "events": safe_events}
                if event_type:
                    payload["type"] = event_type[:64]
                self._json(200, payload)
            except (ImportError, OSError, ValueError):
                self._json(200, {"version": 1, "limit": 0, "events": []})
            return

        if path in ("/scheduler/dead-letters", "/api/scheduler/dead-letters"):
            try:
                try:
                    from codebot.lease_state import dead_letters
                except ImportError:
                    from bots.lease_state import dead_letters
                letters = dead_letters(STATE_DIR)
                safe = [
                    {"id": str(item.get("id", ""))[:64], "reason": str(item.get("reason", ""))[:256]}
                    for item in letters[:20]
                    if isinstance(item, dict)
                ]
                self._json(200, {"version": 1, "count": len(letters) if isinstance(letters, list) else 0, "dead_letters": safe})
            except (ImportError, OSError, ValueError):
                self._json(200, {"version": 1, "count": 0, "dead_letters": []})
            return

        m = re.match(r"^/(?:api/)?bots/([^/]+)/logs$", path)
        if m:
            name = m.group(1)
            try:
                lines = int(qs.get("lines", ["200"])[0])
            except ValueError:
                self._json(400, {"error": "lines must be an integer"})
                return
            lines = max(1, min(lines, MAX_LOG_LINES))
            if not any(c.name == name for c in BOT_REGISTRY):
                self._json(404, {"error": "unknown bot"})
                return
            self._json(200, {"name": name, "lines": lines, "tail": log_tail(name, lines)})
            return

        m = re.match(r"^/(?:api/)?bots/([^/]+)$", path)
        if m:
            name = m.group(1)
            if not any(c.name == name for c in BOT_REGISTRY):
                self._json(404, {"error": "unknown bot"})
                return
            status = bot_status(name)
            etag = status.get("etag", "")
            computed_at = status.get("computed_at", time.time())

            # Conditional request: If-None-Match (ETag comparison)
            if_none_match = self.headers.get("If-None-Match", "")
            if if_none_match:
                # Strip quotes from ETag header value for comparison
                client_etag = if_none_match.strip().strip('"')
                if client_etag == etag:
                    self._json_304(etag)
                    return

            # Conditional request: If-Modified-Since
            if_modified_since = self.headers.get("If-Modified-Since", "")
            if if_modified_since and not if_none_match:
                try:
                    client_time = email.utils.parsedate_to_datetime(if_modified_since)
                    server_time = email.utils.formatdate(timeval=computed_at, localtime=False, usegmt=True)
                    server_dt = email.utils.parsedate_to_datetime(server_time)
                    if client_time >= server_dt:
                        self._json_304(etag)
                        return
                except (TypeError, ValueError, IndexError):
                    pass

            # Strip internal metadata from response to clients
            response = {k: v for k, v in status.items() if k not in ("computed_at", "etag")}
            self._json_with_cache_headers(200, response, etag, computed_at)
            return

        # Delegate telemetry health check to TelemetryHandler logic
        if path in ("/telemetry/health", "/api/telemetry/health"):
            self._json(200, {"status": "ok", "time": time.time()})
            return

        # Gate metrics — read-only observability endpoint
        if path in ("/gates/metrics", "/api/gates/metrics"):
            try:
                try:
                    from codebot.quality_gate import get_gate_metrics
                except ImportError:
                    from bots.quality_gate import get_gate_metrics
                payload = get_gate_metrics(STATE_DIR)
                self._json(200, payload)
            except Exception:
                self._json(200, {
                    "version": 1,
                    "metrics": [],
                    "alerts": [],
                    "generated_at": time.time(),
                })
            return

        # Economics endpoints — read-only observability for budget/cost data
        if path in ("/economics/budget-status", "/api/economics/budget-status"):
            self._json(200, economics_budget_status())
            return

        if path in ("/economics/summary", "/api/economics/summary"):
            self._json(200, economics_summary())
            return

        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        auth_result = self._auth()
        if auth_result is None:
            return  # Rate limited, response already sent
        if not auth_result:
            self._json(401, {"error": "unauthorized", "hint": CONTROL_UNAUTHORIZED_HINT})
            return
        parsed = urlparse(self.path)
        path = parsed.path
        body, error_code, error = self._read_json_body()
        if error_code is not None:
            self._json(error_code, {"error": error})
            return
        # Ensure body is always a dict (robust against edge cases)
        if body is None:
            body = {}

        # Destructive endpoint validation: require explicit confirmation or dry-run
        DESTRUCTIVE_PATHS = {
            "/bots/stop", "/api/bots/stop", "/control/stop",
            "/control/drain", "/api/control/drain",
            "/control/update", "/api/control/update",
        }
        if path in DESTRUCTIVE_PATHS:
            dry_run = body.get("dry_run")
            if dry_run is True:
                preview = self._get_destructive_preview(path, body)
                self._json(200, {"ok": True, "dry_run": True, "preview": preview})
                return
            force = body.get("force")
            confirm = body.get("confirm")
            if not (force is True or confirm is True):
                self._json(400, {"error": "destructive action requires 'force': true or 'confirm': true in request body; use --force flag or interactive confirmation"})
                return

        # POST /bots/{name}/restart
        m = re.match(r"^/(?:api/)?bots/([^/]+)/restart$", path)
        if m:
            name = m.group(1)
            # Validate bot name format before any subprocess calls (Constitution §2)
            if not validate_bot_name(name):
                self._json(400, {"error": "invalid bot name format"})
                return
            if not any(c.name == name for c in BOT_REGISTRY):
                self._json(404, {"error": "unknown bot"})
                return
            # Per-bot destructive action: require force/confirm or dry-run
            is_dry_run = body.get("dry_run") is True
            if is_dry_run:
                self._json(200, {"ok": True, "dry_run": True, "preview": {
                    "action": "restart", "bot": name,
                    "description": f"Would restart bot '{name}' by killing current process and starting via orchestrator",
                    "undo": "Bot will auto-respawn on next orchestrator health check if paused accidentally",
                }})
                return
            force = body.get("force")
            confirm = body.get("confirm")
            if not (force is True or confirm is True):
                self._json(400, {"error": "destructive action requires 'force': true or 'confirm': true in request body; use --force flag or interactive confirmation"})
                return
            # Safely kill existing process using PID verification, then restart
            try:
                # Use safe kill with PID verification instead of pkill regex (CB-8668967-A113)
                success, killed_pids = _safe_kill_bot_process(name, timeout=5)
                if not success:
                    logger.warning("restart: safe kill reported errors for bot '%s', proceeding with restart anyway", name)
                time.sleep(1)
                # orchestrator will respawn on next health check if waiting; force start via orchestrator CLI
                subprocess.Popen(["python3", str(ORCH), "--start", name], cwd=str(BOTS_DIR))
                self._json(200, {"ok": True, "action": "restart", "bot": name,
                                 "killed_pids": killed_pids,
                                 "undo": "Bot will auto-respawn on next orchestrator health check"})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        m = re.match(r"^/(?:api/)?bots/([^/]+)/pause$", path)
        if m:
            name = m.group(1)
            # Validate bot name format before any subprocess calls (Constitution §2)
            if not validate_bot_name(name):
                self._json(400, {"error": "invalid bot name format"})
                return
            if not any(c.name == name for c in BOT_REGISTRY):
                self._json(404, {"error": "unknown bot"})
                return
            # Per-bot destructive action: require force/confirm or dry-run
            is_dry_run = body.get("dry_run") is True
            if is_dry_run:
                self._json(200, {"ok": True, "dry_run": True, "preview": {
                    "action": "pause", "bot": name,
                    "description": f"Would pause bot '{name}' by creating state/{name}.paused and killing process",
                    "undo": f"POST /bots/{name}/resume to unpause",
                }})
                return
            force = body.get("force")
            confirm = body.get("confirm")
            if not (force is True or confirm is True):
                self._json(400, {"error": "destructive action requires 'force': true or 'confirm': true in request body; use --force flag or interactive confirmation"})
                return
            try:
                (STATE_DIR / f"{name}.paused").write_text(str(time.time()))
                # Use safe kill with PID verification instead of pkill regex (CB-8668967-A113)
                success, killed_pids = _safe_kill_bot_process(name, timeout=5)
                if not success:
                    logger.warning("pause: safe kill reported errors for bot '%s'", name)
                self._json(200, {"ok": True, "paused": name,
                                 "killed_pids": killed_pids,
                                 "undo": f"POST /bots/{name}/resume to unpause"})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        m = re.match(r"^/(?:api/)?bots/([^/]+)/resume$", path)
        if m:
            name = m.group(1)
            # Validate bot name format before any subprocess calls (Constitution §2)
            if not validate_bot_name(name):
                self._json(400, {"error": "invalid bot name format"})
                return
            if not any(c.name == name for c in BOT_REGISTRY):
                self._json(404, {"error": "unknown bot"})
                return
            try:
                p = STATE_DIR / f"{name}.paused"
                if p.exists():
                    p.unlink()
                # Safe: list-mode subprocess + validate_bot_name() regex check above
                # prevents injection. Do NOT use shlex.quote with list args.
                subprocess.Popen(["python3", str(ORCH), "--start", name], cwd=str(BOTS_DIR))
                self._json(200, {"ok": True, "resumed": name})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path in ("/bots/start", "/api/bots/start"):
            bots = body.get("bots")
            if bots is None:
                # No bots specified, start all registered bots
                bots = [c.name for c in BOT_REGISTRY]
            else:
                # Validate each bot name format before subprocess calls (Constitution §2)
                if not isinstance(bots, list):
                    self._json(400, {"error": "bots must be an array"})
                    return
                for n in bots:
                    if not isinstance(n, str):
                        self._json(400, {"error": "bot names must be strings"})
                        return
                    if not validate_bot_name(n):
                        self._json(400, {"error": "invalid bot name format"})
                        return
                # Validate each bot name against BOT_REGISTRY (Constitution §2)
                # Return 400 for unknown bots to prevent enumeration and argument injection
                valid_bot_names = {c.name for c in BOT_REGISTRY}
                for n in bots:
                    if n not in valid_bot_names:
                        self._json(400, {"error": f"unknown bot: {n}"})
                        return
            try:
                # Safe: list-mode subprocess + validate_bot_name() regex check above
                # prevents injection. Do NOT use shlex.quote with list args.
                subprocess.Popen(["python3", str(ORCH), "--start", *bots], cwd=str(BOTS_DIR))
                self._json(200, {"ok": True, "started": bots})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path in ("/bots/stop", "/api/bots/stop", "/control/stop"):
            bots = body.get("bots")
            try:
                if bots:
                    if not isinstance(bots, list):
                        self._json(400, {"error": "bots must be an array"})
                        return
                    # Validate each bot name format before subprocess calls (Constitution §2)
                    for n in bots:
                        if not isinstance(n, str):
                            self._json(400, {"error": "bot names must be strings"})
                            return
                        if not validate_bot_name(n):
                            self._json(400, {"error": "invalid bot name format"})
                            return
                    # Validate each bot name against BOT_REGISTRY (Constitution §2)
                    # Reject unknown bots with 400 to prevent arbitrary process targeting
                    valid_bot_names = {c.name for c in BOT_REGISTRY}
                    for n in bots:
                        if n not in valid_bot_names:
                            self._json(400, {"error": f"unknown bot: {n}"})
                            return
                    # Use safe kill with PID verification instead of pkill regex (CB-8668967-A113)
                    all_killed_pids: list[int] = []
                    for n in bots:
                        success, killed_pids = _safe_kill_bot_process(n, timeout=5)
                        if not success:
                            logger.warning("stop: safe kill reported errors for bot '%s'", n)
                        all_killed_pids.extend(killed_pids)
                else:
                    # Stop-all: use PID-verified killing for all registered bots
                    # instead of raw pkill -f which can match arbitrary processes
                    all_killed_pids = []
                    for cfg in BOT_REGISTRY:
                        success, killed_pids = _safe_kill_bot_process(cfg.name, timeout=5)
                        if not success:
                            logger.warning("stop-all: safe kill reported errors for bot '%s'", cfg.name)
                        all_killed_pids.extend(killed_pids)
                    # Also kill orchestrator using safe verification helper
                    orch_success, orch_killed_pids = _safe_kill_orchestrator(timeout=5)
                    if not orch_success:
                        logger.warning("stop-all: safe kill reported errors for orchestrator")
                    all_killed_pids.extend(orch_killed_pids)
                response_data = {"ok": True, "stopped": bots or "all",
                                 "killed_pids": all_killed_pids,
                                 "undo": "Run 'start' or 'restart <bot>' to resume bots; orchestrator will auto-respawn if still running"}
                self._json(200, response_data)
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path in ("/control/drain", "/api/control/drain"):
            try:
                (STATE_DIR / ".drain").write_text(str(time.time()))
                self._json(200, {"ok": True, "drain": True,
                                 "undo": "POST /control/clear-drain to resume operations"})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path in ("/control/clear-drain", "/api/control/clear-drain"):
            try:
                p = STATE_DIR / ".drain"
                if p.exists():
                    p.unlink()
                self._json(200, {"ok": True, "drain": False})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path in ("/control/update", "/api/control/update"):
            try:
                proc = subprocess.run([str(SAFE_UPDATE), "--force"], cwd=str(BOTS_DIR), capture_output=True, text=True, timeout=120)
                self._json(200, {"ok": proc.returncode == 0, "returncode": proc.returncode, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:],
                                 "undo": "Git revert can restore previous version if needed"})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        # POST /telemetry — production telemetry ingestion
        if path in ("/telemetry", "/api/telemetry"):
            self._handle_telemetry(body)
            return

        m = re.match(r"^/(?:api/)?scheduler/dead-letters/(Q-\d+)/retry$", path)
        if m:
            result = retry_dead_letter(m.group(1))
            self._json(200, result)
            return

        self._json(404, {"error": "not found"})

    def _handle_telemetry(self, body: dict) -> None:
        """Handle POST /telemetry — ingest production telemetry signals.

        Validates the signal, creates a ticket candidate in DISCOVERED state,
        stores the event for trend analysis, and triggers anomaly detection.
        """
        # Get client IP for rate limiting
        client_ip = self.client_address[0] if self.client_address else "unknown"

        # Check rate limit before processing auth
        allowed, reason = _rate_limiter.is_allowed(client_ip)
        if not allowed:
            logger.warning("Rate limit exceeded for telemetry from %s: %s", client_ip, reason)
            retry_after = str(RATE_LIMIT_COOLDOWN_SECONDS)
            self._json(429, {"error": "too many requests", "reason": reason}, extra_headers={"Retry-After": retry_after})
            return

        # Fail-closed guard: Reject telemetry when CONTROL_TOKEN is empty (CB-B4086).
        # No opt-in flag may re-enable unauthenticated access (Constitution §2).
        if not CONTROL_TOKEN:
            logger.warning(
                "SECURITY: Telemetry request rejected because CONTROL_TOKEN is empty."
            )
            self._json(401, {"error": "unauthorized", "hint": CONTROL_UNAUTHORIZED_HINT})
            return

        # Check telemetry-specific auth using constant-time comparison (Constitution §2)
        if TELEMETRY_TOKEN:
            auth = self.headers.get("Authorization", "")
            expected = f"Bearer {TELEMETRY_TOKEN}"
            if not hmac.compare_digest(auth.strip(), expected):
                _rate_limiter.record_failure(client_ip)
                logger.warning("Telemetry authentication failed for %s", client_ip)
                self._json(401, {"error": "unauthorized", "hint": TELEMETRY_UNAUTHORIZED_HINT})
                return
        elif not CONTROL_TOKEN:
            # If neither token is set, reject telemetry (safer default)
            self._json(401, {"error": "telemetry token not configured", "hint": TELEMETRY_NOT_CONFIGURED_HINT})
            return

        # Import telemetry validation and ticket creation
        try:
            from codebot.telemetry import (
                _validate_signal,
                _create_ticket_from_signal,
                detect_anomalies,
            )
            from codebot.event_log import append_event
        except ImportError as e:
            logger.error("Failed to import telemetry modules: %s", e)
            self._json(500, {"error": "telemetry subsystem unavailable"})
            return

        # Validate input at trust boundary
        is_valid, error_msg = _validate_signal(body)
        if not is_valid:
            self._json(400, {"error": error_msg})
            return

        # Store telemetry event for trend analysis
        try:
            append_event(STATE_DIR, "telemetry", body)
        except Exception as e:
            logger.warning("Failed to append telemetry event: %s", e)

        # Create ticket candidate
        result = _create_ticket_from_signal(body, STATE_DIR)

        if result.get("success"):
            response = {
                "ok": True,
                "ticket_id": result.get("ticket_id"),
                "message": "signal accepted, ticket candidate created (requires human triage)",
            }
            if result.get("duplicate"):
                response["message"] = "signal already tracked (duplicate)"

            # Anomaly detection: read recent telemetry events and check for spikes
            try:
                from codebot.event_log import read_events
                recent_events = read_events(STATE_DIR, limit=50)
                telemetry_signals = [
                    e.get("data", {}) for e in recent_events
                    if e.get("type") == "telemetry"
                ]
                # Baseline: assume 2 errors per window is normal
                baseline_rate = 2.0
                anomalies = detect_anomalies(telemetry_signals, baseline_rate)
                if anomalies:
                    for anomaly in anomalies:
                        append_event(STATE_DIR, "discovery-trigger", anomaly)
                    response["anomalies_detected"] = len(anomalies)
                    logger.info(
                        "Telemetry anomaly detected: %d anomalies, discovery triggered",
                        len(anomalies),
                    )
            except Exception as e:
                logger.warning("Anomaly detection failed: %s", e)

            self._json(201, response)
        else:
            self._json(500, {"error": result.get("error", "internal error")})

    def log_message(self, format, *args):  # noqa: A002
        # quiet except errors; fly logs capture stdout
        pass


def main() -> None:
    # Bind to localhost only when CONTROL_TOKEN is unset (fail-closed per Constitution §2).
    # This prevents unauthenticated network access in containerized/shared environments.
    if not CONTROL_TOKEN:
        bind_host = "127.0.0.1"
        # Ensure logging has at least one handler so logger.critical is visible in
        # container stdout/stderr even when no orchestrator has configured logging.
        if not logging.getLogger().handlers and not logger.handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
                stream=sys.stderr,
            )
        _sec_msg = (
            "SECURITY: CONTROL_TOKEN is not set — binding to 127.0.0.1 only (fail-closed). "
            "All authenticated endpoints will reject requests. "
            "Set CONTROL_TOKEN env var to enable remote API access."
        )
        logger.critical(_sec_msg)
        # Duplicate via print to stderr so Fly.io log streams (which may not
        # capture logging output) always surface this critical warning.
        print(f"CRITICAL: {_sec_msg}", file=sys.stderr, flush=True)
    else:
        bind_host = "0.0.0.0"

    addr = (bind_host, PORT)
    startup_msg = f"control_server listening on {addr[0]}:{addr[1]}  bots={len(BOT_REGISTRY)}  drain={(STATE_DIR / '.drain').exists()}"
    print(startup_msg, flush=True)
    logger.info(startup_msg)
    httpd = ThreadingHTTPServer(addr, ControlHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
