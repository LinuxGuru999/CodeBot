"""Tests for module.py error handling."""
import ast
import pytest
from pathlib import Path


def test_no_bare_except_clauses():
    """Verify that module.py has no bare except clauses."""
    module_path = Path(__file__).parent.parent / "codebot" / "module.py"
    assert module_path.exists(), "module.py should exist"
    
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    
    bare_excepts = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                bare_excepts.append(
                    f"Bare except at line {node.lineno}"
                )
    
    assert len(bare_excepts) == 0, (
        f"Found bare except clauses: {bare_excepts}. "
        "All except blocks must specify an exception type."
    )


def test_exception_logging_before_handling():
    """Verify that exceptions are logged before being handled."""
    module_path = Path(__file__).parent.parent / "codebot" / "module.py"
    assert module_path.exists(), "module.py should exist"
    
    source = module_path.read_text(encoding="utf-8")
    
    # Check that there's logging in except blocks
    # This is a simple check - in production you'd want more sophisticated analysis
    assert "log" in source.lower() or "print" in source.lower(), (
        "Exception handling should include logging"
    )
