"""TDD tests for migration_008 (CB-4080995-C643): remove hidden web deps.

WHY: ticket CB-4080995-C643 requires removing conditional imports of bs4/lxml
from web_tools.py to comply with stdlib-only policy. This migration proves
forward AND reverse paths, idempotency, atomicity, and integrity.
"""
from __future__ import annotations

import hashlib
import importlib
from pathlib import Path

import pytest

# Pre-migration content snippets to ensure tests are self-contained
_DIRTY_EXTRACT_FUNC = """
def extract_text_from_html(html: str) -> str:
    \"\"\"Extract readable text from HTML.

    Attempts to use beautifulsoup4 (with lxml parser) or lxml directly for robust parsing
    of malformed HTML. Falls back to stdlib regex-based parsing if neither is available.

    Args:
        html: Raw HTML string.

    Returns:
        Extracted plain text.
    \"\"\"
    # Try beautifulsoup4 first (preferred for robustness)
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        # Remove script and style elements
        for script in soup(["script", "style", "nav", "footer", "header", "aside"]):
            script.decompose()
        text = soup.get_text(separator="\\n")
        # Clean up whitespace similar to fallback
        text = re.sub(r'\\n{3,}', '\\n\\n', text)
        lines = [line.strip() for line in text.splitlines()]
        lines = [line for line in lines if line]
        return '\\n'.join(lines)
    except ImportError:
        pass

    # Try lxml directly if bs4 failed but lxml is present (less likely if bs4 isn't there, but possible)
    doc = None
    try:
        from lxml import etree, html as lh
        doc = lh.fromstring(html.encode('utf-8'))
        # Remove unwanted tags
        for tag in ["script", "style", "nav", "footer", "header", "aside"]:
            for element in doc.xpath(f".//{tag}"):
                parent = element.getparent()
                if parent is not None:
                    parent.remove(element)
        text = etree.tostring(doc, method='text', encoding='unicode')
        text = re.sub(r'\\n{3,}', '\\n\\n', text)
        lines = [line.strip() for line in text.splitlines()]
        lines = [line for line in lines if line]
        return '\\n'.join(lines)
    except ImportError:
        pass
    finally:
        # Explicitly free the document tree to prevent memory leaks on large inputs
        # or if an exception occurred during processing.
        if doc is not None:
            del doc

    # Fallback to stdlib regex-based parsing
    for tag in ("script", "style", "nav", "footer", "header", "aside"):
        html = re.sub(rf'<{tag}[^>]*>.*?</{tag}>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<br\\s*/?>', '\\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</p>', '\\n\\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</h[1-6]>', '\\n\\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</li>', '\\n', html, flags=re.IGNORECASE)
    html = re.sub(r'<[^>]+>', ' ', html)
    html = re.sub(r'&nbsp;', ' ', html)
    html = re.sub(r'&amp;', '&', html)
    html = re.sub(r'&lt;', '<', html)
    html = re.sub(r'&gt;', '>', html)
    html = re.sub(r'&#\\d+;', '', html)
    html = re.sub(r'[ \\t]+', ' ', html)
    lines = [line.strip() for line in html.splitlines()]
    lines = [line for line in lines if line]
    return '\\n'.join(lines)
"""

_CLEAN_EXTRACT_FUNC = """
def extract_text_from_html(html: str) -> str:
    \"\"\"Extract readable text from HTML using stdlib only.

    Uses regex-based parsing to strip tags and entities. No third-party
    dependencies are used.

    Args:
        html: Raw HTML string.

    Returns:
        Extracted plain text.
    \"\"\"
    # Stdlib-only regex-based parsing
    for tag in ("script", "style", "nav", "footer", "header", "aside"):
        html = re.sub(rf'<{tag}[^>]*>.*?</{tag}>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<br\\s*/?>', '\\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</p>', '\\n\\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</h[1-6]>', '\\n\\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</li>', '\\n', html, flags=re.IGNORECASE)
    html = re.sub(r'<[^>]+>', ' ', html)
    html = re.sub(r'&nbsp;', ' ', html)
    html = re.sub(r'&amp;', '&', html)
    html = re.sub(r'&lt;', '<', html)
    html = re.sub(r'&gt;', '>', html)
    html = re.sub(r'&#\\d+;', '', html)
    html = re.sub(r'[ \\t]+', ' ', html)
    lines = [line.strip() for line in html.splitlines()]
    lines = [line for line in lines if line]
    return '\\n'.join(lines)
"""

_DIRTY_DOCSTRING = """- stdlib-only baseline (urllib.request, urllib.parse, html.parser, re); optionally
  uses beautifulsoup4 + lxml for robust HTML parsing when installed, otherwise falls
  back to regex-based extraction -- no required third-party runtime dependencies
  (see docs/adr/005-web-tools-html-parsing-strategy.md)"""

_CLEAN_DOCSTRING = """- stdlib-only (urllib.request, urllib.parse, html.parser, re, html.unescape)
- No third-party runtime dependencies (beautifulsoup4, lxml removed per CB-4080995-C643)"""


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Stage a dirty (pre-migration) version of web_tools.py for testing."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    import codebot.migrations.migration_008_remove_web_deps as mig
    importlib.reload(mig)
    
    work = tmp_path / "work"
    work.mkdir()
    wt = work / "web_tools.py"
    doc = work / "web_tools.md"
    
    # Create a minimal valid web_tools.py with dirty content
    # We use a simplified version that contains the key parts the migration looks for
    dirty_content = f"""\"\"\"Web tools module.

Invariants
----------
{_DIRTY_DOCSTRING}
\"\"\"
import re

{_DIRTY_EXTRACT_FUNC}
"""
    wt.write_text(dirty_content, encoding="utf-8")
    
    dirty_doc = f"""# web_tools.py

{_DIRTY_DOCSTRING}
"""
    doc.write_text(dirty_doc, encoding="utf-8")
        
    return mig, wt, doc


def test_migrate_forward_removes_bs4_imports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Forward migration removes bs4/lxml imports from web_tools.py."""
    mig, wt, doc = _stage(tmp_path, monkeypatch)
    result = mig.forward(wt, doc)
    assert result["status"] == "migrated"
    text = _read(wt)
    assert "from bs4 import BeautifulSoup" not in text
    assert "from lxml import" not in text
    # Validity gate: migrated copy must still compile
    compile(text, str(wt), "exec")


def test_migrate_rollback_restores_bs4_imports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rollback restores the pre-migration bs4/lxml imports byte-identically."""
    mig, wt, doc = _stage(tmp_path, monkeypatch)
    before_wt = _checksum(wt)
    assert mig.forward(wt, doc)["status"] == "migrated"
    assert _checksum(wt) != before_wt  # real transformation happened
    result = mig.rollback(wt, doc)
    assert result["status"] == "restored"
    assert _checksum(wt) == before_wt


def test_migrate_forward_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running forward twice equals running once (safe retry)."""
    mig, wt, doc = _stage(tmp_path, monkeypatch)
    assert mig.forward(wt, doc)["status"] == "migrated"
    sum_wt = _checksum(wt)
    second = mig.forward(wt, doc)
    assert second["status"] == "skipped"
    assert _checksum(wt) == sum_wt


def test_migrate_rollback_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running rollback twice is safe."""
    mig, wt, doc = _stage(tmp_path, monkeypatch)
    mig.forward(wt, doc)
    assert mig.rollback(wt, doc)["status"] == "restored"
    first_wt = _checksum(wt)
    result = mig.rollback(wt, doc)
    assert result["status"] == "skipped"
    assert _checksum(wt) == first_wt


def test_migrate_updates_docstring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Forward migration updates the module docstring to reflect stdlib-only."""
    mig, wt, doc = _stage(tmp_path, monkeypatch)
    mig.forward(wt, doc)
    text = _read(wt)
    assert "stdlib-only" in text
    assert "beautifulsoup4" not in text or "removed" in text.lower()


def test_migrate_backup_integrity_and_atomicity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Backups are created+verified; no stray tmp files remain after swap."""
    mig, wt, doc = _stage(tmp_path, monkeypatch)
    mig.forward(wt, doc)
    bak = Path(str(wt) + mig.BACKUP_SUFFIX)
    assert bak.is_file() and bak.stat().st_size > 0
    assert not Path(str(wt) + mig.TMP_SUFFIX).exists()
