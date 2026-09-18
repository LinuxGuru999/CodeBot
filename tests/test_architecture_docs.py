"""Tests to verify ARCHITECTURE.md accurately documents all codebot modules.

This test ensures documentation drift is caught automatically.
Ticket: CB-3301399-DC2F
"""
import re
from pathlib import Path


def _get_actual_modules() -> set[str]:
    """Return set of Python module names in codebot/ directory."""
    codebot_dir = Path(__file__).parent.parent / "codebot"
    modules = set()
    for py_file in codebot_dir.glob("*.py"):
        if py_file.name.startswith("__"):
            continue
        modules.add(py_file.stem)
    return modules


def _get_documented_modules() -> set[str]:
    """Extract module names documented in ARCHITECTURE.md tables."""
    arch_path = Path(__file__).parent.parent / "docs" / "ARCHITECTURE.md"
    if not arch_path.exists():
        return set()
    
    content = arch_path.read_text(encoding="utf-8")
    # Match table rows with backtick-wrapped module names like `module.py`
    pattern = r'\|\s*`(\w+)\.py`\s*\|'
    matches = re.findall(pattern, content)
    return set(matches)


def test_all_modules_are_documented():
    """Every Python module in codebot/ must be listed in ARCHITECTURE.md."""
    actual = _get_actual_modules()
    documented = _get_documented_modules()
    
    missing = actual - documented
    assert not missing, (
        f"The following modules exist in codebot/ but are not documented "
        f"in ARCHITECTURE.md: {sorted(missing)}"
    )


def test_no_ghost_modules_documented():
    """ARCHITECTURE.md should not list modules that don't exist."""
    actual = _get_actual_modules()
    documented = _get_documented_modules()
    
    ghosts = documented - actual
    assert not ghosts, (
        f"ARCHITECTURE.md documents modules that don't exist in codebot/: "
        f"{sorted(ghosts)}"
    )


def test_migrations_directory_documented():
    """The migrations/ directory should be mentioned in ARCHITECTURE.md."""
    arch_path = Path(__file__).parent.parent / "docs" / "ARCHITECTURE.md"
    content = arch_path.read_text(encoding="utf-8")
    
    assert "migrations" in content.lower(), (
        "ARCHITECTURE.md should document the migrations/ directory"
    )
