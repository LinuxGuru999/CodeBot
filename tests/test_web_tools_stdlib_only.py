#!/usr/bin/env python3
"""Tests verifying stdlib-only policy enforcement in web_tools.

Ensures no hidden imports of bs4 or lxml remain, and that
extract_text_from_html works correctly using only stdlib.
"""
import ast
import sys
from pathlib import Path

from codebot.web_tools import extract_text_from_html

WEB_TOOLS_PATH = Path(__file__).resolve().parent.parent / "codebot" / "web_tools.py"


def test_no_bs4_import_in_source() -> None:
    """Verify that bs4/beautifulsoup4 is not imported anywhere in web_tools.py."""
    source = WEB_TOOLS_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "bs4" not in node.module, "Hidden bs4 import found"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "bs4" not in alias.name, "Hidden bs4 import found"


def test_no_lxml_import_in_source() -> None:
    """Verify that lxml is not imported anywhere in web_tools.py."""
    source = WEB_TOOLS_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "lxml" not in node.module, "Hidden lxml import found"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "lxml" not in alias.name, "Hidden lxml import found"


def test_extract_text_works_without_external_deps() -> None:
    """Ensure extract_text_from_html functions correctly with only stdlib."""
    # Temporarily block bs4 and lxml to simulate their absence
    bs4_mod = sys.modules.get("bs4")
    lxml_mod = sys.modules.get("lxml")
    try:
        sys.modules["bs4"] = None  # type: ignore[assignment]
        sys.modules["lxml"] = None  # type: ignore[assignment]

        html = "<html><body><p>Hello</p><script>var x=1;</script></body></html>"
        text = extract_text_from_html(html)
        assert "Hello" in text
        assert "var x=1;" not in text
    finally:
        if bs4_mod is not None:
            sys.modules["bs4"] = bs4_mod
        elif "bs4" in sys.modules:
            del sys.modules["bs4"]
        if lxml_mod is not None:
            sys.modules["lxml"] = lxml_mod
        elif "lxml" in sys.modules:
            del sys.modules["lxml"]


def test_extract_text_idempotent() -> None:
    """Running extraction twice on same input produces identical output."""
    html = "<div><p>Test</p><style>.x{}</style></div>"
    first = extract_text_from_html(html)
    second = extract_text_from_html(html)
    assert first == second
