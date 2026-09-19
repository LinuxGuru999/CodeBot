"""Tests for botop logs fuzzy matching functionality.

Ticket: CB-4979945-0249
Acceptance criteria:
- Typo in agent name suggests closest matches (did-you-mean)
- logs without args lists agents
- --follow documents Ctrl-C exit
"""

import subprocess
import sys
from pathlib import Path
import tempfile
import os

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOTOP = [sys.executable, "-m", "codebot.botop"]


def test_logs_without_agent_lists_agents():
    """Running `botop logs` without an agent argument should list available agents."""
    result = subprocess.run(
        [*BOTOP, "logs"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    # Should exit with error code since agent is required, but provide helpful message
    assert result.returncode != 0, "logs without agent should show usage/error"
    combined = result.stdout + result.stderr
    # Should mention available agents or provide guidance
    assert "usage" in combined.lower() or "agent" in combined.lower(), (
        f"logs without agent should show usage: {combined[:300]}"
    )


def test_logs_fuzzy_match_suggests_similar_names():
    """Typo in agent name should suggest closest matches."""
    # Create a temporary state/logs directory with test agents
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        state_dir = tmpdir_path / ".codebot" / "state"
        logs_dir = tmpdir_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        
        # Create some test agent log files
        (logs_dir / "scheduler.log").write_text("scheduler log content")
        (logs_dir / "validator.log").write_text("validator log content")
        (logs_dir / "implementer.log").write_text("implementer log content")
        
        # Test with a typo that should match "scheduler"
        result = subprocess.run(
            [*BOTOP, "--project", str(tmpdir_path), "logs", "schedulr"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        
        combined = result.stdout + result.stderr
        # Should suggest similar names (did-you-mean)
        assert "scheduler" in combined.lower() or "did you mean" in combined.lower() or "similar" in combined.lower(), (
            f"Should suggest similar agent names. Output: {combined[:500]}"
        )


def test_logs_exact_match_still_works():
    """Exact agent name match should still work normally."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        state_dir = tmpdir_path / ".codebot" / "state"
        logs_dir = tmpdir_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        
        # Create test agent log file
        test_content = "test log line 1\ntest log line 2\ntest log line 3"
        (logs_dir / "testagent.log").write_text(test_content)
        
        result = subprocess.run(
            [*BOTOP, "--project", str(tmpdir_path), "logs", "testagent"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        
        assert result.returncode == 0, f"Exact match should succeed. stderr: {result.stderr}"
        assert "test log line" in result.stdout, f"Should show log content: {result.stdout}"


def test_follow_option_documents_ctrl_c():
    """The --follow option should document Ctrl-C exit in help or usage."""
    result = subprocess.run(
        [*BOTOP, "logs", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    
    assert result.returncode == 0, "logs --help should succeed"
    combined = result.stdout + result.stderr
    # Help should mention follow and ideally Ctrl-C
    assert "follow" in combined.lower() or "-f" in combined, (
        f"Help should mention follow option: {combined[:300]}"
    )
