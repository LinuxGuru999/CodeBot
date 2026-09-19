"""Tests for botop CLI contract: bare invocation and piped live behavior.

Ticket: CB-1417925-0B8C
Acceptance criteria:
- Piped `botop live` without --once terminates after one snapshot
- Bare `botop` exits 0 with a useful snapshot or clean hint, never exit code 2
"""

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOTOP = [sys.executable, "-m", "codebot.botop"]


def test_bare_botop_exits_zero():
    """Bare `botop` (no subcommand) must exit 0, not 2."""
    result = subprocess.run(
        BOTOP,
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    # Should NOT exit with error status 2
    assert result.returncode == 0, (
        f"bare botop exited {result.returncode}, expected 0.\n"
        f"stderr: {result.stderr[:500]}"
    )
    # Should contain a hint or useful output
    combined = result.stdout + result.stderr
    assert len(combined) > 0, "bare botop produced no output"
    # Should mention at least one of the suggested commands
    assert any(
        keyword in combined.lower()
        for keyword in ["live", "term", "health", "status", "hint"]
    ), f"bare botop output lacks useful hint: {combined[:300]}"


def test_bare_botop_no_error_exit():
    """Bare `botop` must never exit with code 2 (error)."""
    result = subprocess.run(
        BOTOP,
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode != 2, (
        f"bare botop exited with error code 2. stderr: {result.stderr[:500]}"
    )


def test_piped_live_terminates():
    """Piped `botop live` (stdout not a TTY) must terminate after one snapshot."""
    # When stdout is piped, it's not a TTY
    result = subprocess.run(
        [*BOTOP, "live"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    # Should complete within timeout (not loop forever)
    assert result.returncode == 0, (
        f"piped botop live exited {result.returncode}. stderr: {result.stderr[:500]}"
    )
    # Should have produced at least one snapshot
    assert len(result.stdout) > 0, "piped botop live produced no output"
    # Should contain recognizable dashboard content
    assert "CodeBot" in result.stdout or "Agent" in result.stdout or "Ticket" in result.stdout, (
        f"piped botop live output doesn't look like a snapshot: {result.stdout[:200]}"
    )


def test_live_once_still_works():
    """`botop live --once` should still work and exit 0."""
    result = subprocess.run(
        [*BOTOP, "live", "--once"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0, (
        f"botop live --once exited {result.returncode}. stderr: {result.stderr[:500]}"
    )
    assert len(result.stdout) > 0, "botop live --once produced no output"


def test_live_view_agents_only():
    """`botop live --once --view agents` should show agents but not tickets."""
    result = subprocess.run(
        [*BOTOP, "live", "--once", "--view", "agents"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0, (
        f"botop live --view agents exited {result.returncode}. stderr: {result.stderr[:500]}"
    )
    assert "Agents" in result.stdout, "agents view should contain 'Agents'"
    assert "Tickets" not in result.stdout, "agents view should NOT contain 'Tickets'"


def test_live_view_tickets_only():
    """`botop live --once --view tickets` should show tickets but not agents."""
    result = subprocess.run(
        [*BOTOP, "live", "--once", "--view", "tickets"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0, (
        f"botop live --view tickets exited {result.returncode}. stderr: {result.stderr[:500]}"
    )
    assert "Tickets" in result.stdout, "tickets view should contain 'Tickets'"
    assert "Agents" not in result.stdout, "tickets view should NOT contain 'Agents'"


def test_live_view_both_default():
    """`botop live --once` default view should contain both Agents and Tickets."""
    result = subprocess.run(
        [*BOTOP, "live", "--once"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0, (
        f"botop live --once exited {result.returncode}. stderr: {result.stderr[:500]}"
    )
    assert "Agents" in result.stdout, "default view should contain 'Agents'"
    assert "Tickets" in result.stdout, "default view should contain 'Tickets'"


def test_live_view_footer_shows_toggle_hints():
    """Dashboard footer should mention 1/2/3 key toggle shortcuts."""
    result = subprocess.run(
        [*BOTOP, "live", "--once"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0
    combined = result.stdout + result.stderr
    assert "1 agents" in combined.lower() or "1=agents" in combined.lower() or "1 agents" in combined, (
        f"footer missing view toggle hints: {combined[-300:]}"
    )
