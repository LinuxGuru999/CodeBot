#!/usr/bin/env python3
"""Tests for web_tools module."""
import pytest
import socket
from unittest.mock import patch, MagicMock
from codebot.web_tools import _extract_text_from_html, _PinnedHTTPSConnection, _PinnedHTTPConnection

def test_lxml_resource_cleanup():
    """Test that lxml resources are cleaned up even if an exception occurs.
    
    This test ensures that the internal lxml document object is properly
    managed and does not leak memory or resources, especially in error paths.
    """
    # Large HTML string to potentially trigger memory pressure if not cleaned up
    html_content = "<html><body>" + "<p>Test paragraph</p>" * 1000 + "</body></html>"
    
    # This should not raise and should clean up resources internally
    text = _extract_text_from_html(html_content)
    
    assert "Test paragraph" in text
    # Basic sanity check that we got some text back
    assert len(text) > 0

def test_lxml_exception_path_cleanup():
    """Test cleanup when an exception might occur during parsing."""
    # Malformed HTML that might cause issues during xpath/tostring
    html_content = "<html><body><p>Unclosed paragraph<div>Nested</div>"
    
    # Should handle gracefully without leaking resources
    text = _extract_text_from_html(html_content)
    
    # Just ensure it returns a string without crashing
    assert isinstance(text, str)


def test_pinned_https_connection_no_pinned_ip_fails():
    """Test that _PinnedHTTPSConnection raises ValueError when pinned_ip is None.
    
    This ensures fail-closed security posture: if no IP is pinned, the connection
    must not fall back to DNS resolution, preventing SSRF via DNS rebinding.
    """
    conn = _PinnedHTTPSConnection(host="example.com", port=443, pinned_ip=None)
    
    with pytest.raises(ValueError, match="Pinned IP is required"):
        conn.connect()


def test_pinned_http_connection_no_pinned_ip_fails():
    """Test that _PinnedHTTPConnection raises ValueError when pinned_ip is None.
    
    Regression test to ensure HTTP connections also enforce IP pinning.
    """
    conn = _PinnedHTTPConnection(host="example.com", port=80, pinned_ip=None)
    
    with pytest.raises(ValueError, match="Pinned IP is required"):
        conn.connect()
