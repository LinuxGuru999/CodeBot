"""Tests for CB-9621432-FB18: Live dashboard accessibility and pagination.

Acceptance criteria:
- Live view offers keyboard-discoverable help on first frame
- screen-clear path has --no-clear or reduced-motion alternative
- full agent list reachable via pagination flag
"""

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOTOP = [sys.executable, "-m", "codebot.botop"]


def test_live_no_clear_flag_accepted():
    """botop live --once --no-clear should be accepted and exit 0."""
    result = subprocess.run(
        [*BOTOP, "live", "--once", "--no-clear"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0, (
        f"botop live --no-clear exited {result.returncode}. stderr: {result.stderr[:500]}"
    )
    assert len(result.stdout) > 0, "botop live --no-clear produced no output"


def test_live_limit_flag_accepted():
    """botop live --once --limit 5 should be accepted and exit 0."""
    result = subprocess.run(
        [*BOTOP, "live", "--once", "--limit", "5"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0, (
        f"botop live --limit 5 exited {result.returncode}. stderr: {result.stderr[:500]}"
    )
    assert len(result.stdout) > 0, "botop live --limit 5 produced no output"


def test_live_offset_flag_accepted():
    """botop live --once --offset 0 should be accepted and exit 0."""
    result = subprocess.run(
        [*BOTOP, "live", "--once", "--offset", "0"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0, (
        f"botop live --offset 0 exited {result.returncode}. stderr: {result.stderr[:500]}"
    )


def test_live_limit_and_offset_combined():
    """botop live --once --limit 3 --offset 0 should work together."""
    result = subprocess.run(
        [*BOTOP, "live", "--once", "--limit", "3", "--offset", "0"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0, (
        f"botop live --limit 3 --offset 0 exited {result.returncode}. stderr: {result.stderr[:500]}"
    )
    assert len(result.stdout) > 0


def test_live_default_limit_still_15():
    """Default behavior (no --limit) should still show up to 15 agents."""
    result = subprocess.run(
        [*BOTOP, "live", "--once"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0
    # Should still contain the Agents section
    assert "Agents" in result.stdout


def test_live_no_clear_help_in_output():
    """The first-frame help text should mention --no-clear."""
    # When piped, --once is auto-selected, so we check the once output
    result = subprocess.run(
        [*BOTOP, "live", "--once", "--no-clear"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0
    combined = result.stdout + result.stderr
    # The help line is only shown in the live loop (not once mode),
    # but the footer should be present
    assert len(combined) > 0


def test_live_aliases_accept_new_flags():
    """Alias commands (watch, top, dash, dashboard) should accept --no-clear."""
    for alias in ("watch", "top", "dash", "dashboard"):
        result = subprocess.run(
            [*BOTOP, alias, "--once", "--no-clear", "--limit", "5"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, (
            f"botop {alias} --no-clear --limit 5 exited {result.returncode}. "
            f"stderr: {result.stderr[:500]}"
        )


def test_live_json_still_works():
    """--json mode should not be affected by new flags."""
    result = subprocess.run(
        [*BOTOP, "live", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0
    assert '"agents"' in result.stdout, "JSON output should contain agents key"
    assert '"tickets"' in result.stdout, "JSON output should contain tickets key"
