"""Tests for orchestrator SRP refactoring."""
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def test_orchestrator_loc_under_500():
    """Orchestrator should be under 500 lines of code after refactoring."""
    orchestrator_path = project_root / "codebot" / "orchestrator.py"
    assert orchestrator_path.exists(), "orchestrator.py not found"
    
    with open(orchestrator_path, 'r') as f:
        lines = f.readlines()
    
    # Count non-empty, non-comment lines for a rough LOC metric
    # Or just total lines as per ticket acceptance criteria
    total_lines = len(lines)
    print(f"orchestrator.py has {total_lines} lines")
    assert total_lines < 500, f"orchestrator.py has {total_lines} lines, expected < 500"


def test_config_reloader_module_exists():
    """Config reloader module should exist and be importable."""
    try:
        from codebot import config_reloader
        assert hasattr(config_reloader, 'check_prompt_changes')
        assert hasattr(config_reloader, 'check_code_changes')
        assert hasattr(config_reloader, 'check_config_changes')
    except ImportError:
        assert False, "config_reloader module not found or missing required functions"


def test_scheduler_module_exists():
    """Scheduler module should exist and be importable."""
    try:
        from codebot import scheduler
        assert hasattr(scheduler, 'dispatch_tickets_to_implementers')
        assert hasattr(scheduler, 'dispatch_tickets_to_reviewers')
    except ImportError:
        assert False, "scheduler module not found or missing required functions"
