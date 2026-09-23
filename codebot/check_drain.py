"""Check for drain or update lock signals.

Usage:
    python3 -m codebot.check_drain

Exits with 0 if .drain or .update_lock exists in the state directory,
otherwise exits with 1.
"""
import os
import sys
from pathlib import Path


def main() -> None:
    # Determine state directory relative to this file
    # codebot/check_drain.py -> project_root/.codebot/state
    project_root = Path(__file__).resolve().parent.parent
    state_dir = project_root / ".codebot" / "state"
    
    drain_file = state_dir / ".drain"
    update_lock = state_dir / ".update_lock"
    
    if drain_file.exists() or update_lock.exists():
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
