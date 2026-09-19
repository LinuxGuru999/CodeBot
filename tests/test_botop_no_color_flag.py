"""Tests for global --no-color flag propagation to all botop subcommands.

Ticket: CB-1417898-76DB
Acceptance criteria:
- `botop --no-color status/claims/tickets/throughput/metrics/budget/events/findings/
  leases/deadletters/gatekeeper/health` emits zero ANSI escape bytes
- `NO_COLOR=1` env also suppresses color in all commands
- `botop live/term` behavior unchanged (already handled)
"""

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOTOP = [sys.executable, "-m", "codebot.botop"]

# ANSI escape pattern: ESC[
ANSI_PATTERN = b"\x1b["


def _run_botop(args: list[str], env_override: dict | None = None) -> subprocess.CompletedProcess:
    """Run botop with optional environment overrides."""
    import os
    env = os.environ.copy()
    if env_override:
        env.update(env_override)
    return subprocess.run(
        [*BOTOP, *args],
        capture_output=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
        env=env,
    )


def _assert_no_ansi(result: subprocess.CompletedProcess, cmd_desc: str) -> None:
    """Assert that stdout contains no ANSI escape sequences."""
    assert ANSI_PATTERN not in result.stdout, (
        f"botop {cmd_desc} with --no-color still contains ANSI escapes in stdout: "
        f"{result.stdout[:200]!r}"
    )


# --- Subcommands that should honor --no-color ---

SUBCOMMANDS = [
    "status",
    "status --verbose",
    "claims",
    "tickets",
    "throughput",
    "metrics",
    "budget",
    "events",
    "findings",
    "leases",
    "deadletters",
    "gatekeeper",
    "health",
]


def test_no_color_status():
    r = _run_botop(["--no-color", "status"])
    _assert_no_ansi(r, "--no-color status")


def test_no_color_claims():
    r = _run_botop(["--no-color", "claims"])
    _assert_no_ansi(r, "--no-color claims")


def test_no_color_tickets():
    r = _run_botop(["--no-color", "tickets"])
    _assert_no_ansi(r, "--no-color tickets")


def test_no_color_throughput():
    r = _run_botop(["--no-color", "throughput"])
    _assert_no_ansi(r, "--no-color throughput")


def test_no_color_metrics():
    r = _run_botop(["--no-color", "metrics"])
    _assert_no_ansi(r, "--no-color metrics")


def test_no_color_budget():
    r = _run_botop(["--no-color", "budget"])
    _assert_no_ansi(r, "--no-color budget")


def test_no_color_events():
    r = _run_botop(["--no-color", "events"])
    _assert_no_ansi(r, "--no-color events")


def test_no_color_findings():
    r = _run_botop(["--no-color", "findings"])
    _assert_no_ansi(r, "--no-color findings")


def test_no_color_leases():
    r = _run_botop(["--no-color", "leases"])
    _assert_no_ansi(r, "--no-color leases")


def test_no_color_deadletters():
    r = _run_botop(["--no-color", "deadletters"])
    _assert_no_ansi(r, "--no-color deadletters")


def test_no_color_gatekeeper():
    r = _run_botop(["--no-color", "gatekeeper"])
    _assert_no_ansi(r, "--no-color gatekeeper")


def test_no_color_health():
    r = _run_botop(["--no-color", "health"])
    _assert_no_ansi(r, "--no-color health")


def test_no_color_live_once():
    """--no-color should still work for live --once."""
    r = _run_botop(["--no-color", "live", "--once"])
    _assert_no_ansi(r, "--no-color live --once")


def test_no_color_env_var_status():
    """NO_COLOR=1 env var should suppress color in status."""
    r = _run_botop(["status"], env_override={"NO_COLOR": "1"})
    _assert_no_ansi(r, "NO_COLOR=1 status")


def test_no_color_env_var_tickets():
    """NO_COLOR=1 env var should suppress color in tickets."""
    r = _run_botop(["tickets"], env_override={"NO_COLOR": "1"})
    _assert_no_ansi(r, "NO_COLOR=1 tickets")


def test_no_color_env_var_health():
    """NO_COLOR=1 env var should suppress color in health."""
    r = _run_botop(["health"], env_override={"NO_COLOR": "1"})
    _assert_no_ansi(r, "NO_COLOR=1 health")


def test_all_subcommands_accept_no_color_flag():
    """Verify --no-color doesn't crash any subcommand (smoke test)."""
    for subcmd in SUBCOMMANDS:
        r = _run_botop(["--no-color"] + subcmd.split())
        assert r.returncode == 0, (
            f"botop --no-color {subcmd} crashed with exit code {r.returncode}.\n"
            f"stdout: {r.stdout[:300]}\n"
            f"stderr: {r.stderr[:300]}"
        )
