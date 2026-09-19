"""Tests for HTML parsing robustness in web_tools.

Focuses on malformed HTML handling and optional dependency fallback.
"""
import pytest
from codebot.web_tools import _extract_text_from_html


def test_malformed_unclosed_tags():
    """Test parsing HTML with unclosed tags."""
    html = "<p>Start <b>bold <i>italic</b> end</i>"
    # Current regex parser might fail to clean this correctly or produce weird output.
    # We expect clean text extraction regardless.
    text = _extract_text_from_html(html)
    assert "Start" in text
    assert "bold" in text
    assert "italic" in text
    assert "end" in text
    # Ensure no raw tags remain
    assert "<" not in text or "&lt;" in text  # Allow escaped if present, but ideally no raw tags


def test_malformed_nested_tags():
    """Test parsing HTML with improperly nested tags."""
    html = "<div><p>Text <span>more</div></span>"
    text = _extract_text_from_html(html)
    assert "Text" in text
    assert "more" in text


def test_script_and_style_removal():
    """Ensure script and style blocks are removed even if malformed."""
    html = "<html><head><style>body { color: red;</style></head><body>Hello</body>"
    text = _extract_text_from_html(html)
    assert "Hello" in text
    assert "color" not in text
    assert "red" not in text


def test_empty_input():
    """Test handling of empty string."""
    text = _extract_text_from_html("")
    assert text == ""


def test_only_tags():
    """Test HTML with no text content."""
    html = "<div><span></span></div>"
    text = _extract_text_from_html(html)
    assert text == ""
"}}]}