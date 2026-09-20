"""Tests for bounded I/O in control_client module.

Verifies Constitutional §4 invariant: All HTTP response reads use a size-limited
read consistent with the Bounded I/O invariant.

Covers:
- resp.read(MAX_RESPONSE_BYTES) is used instead of unbounded resp.read()
- HTTPError body reads are also bounded
- Normal-sized responses work correctly
- Oversized responses are truncated without memory exhaustion
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from codebot.control_client import req, MAX_RESPONSE_BYTES


class TestControlClientBoundedIO(unittest.TestCase):
    """Test that all HTTP response reads in control_client are bounded."""

    def test_max_response_bytes_constant_exists(self):
        """Verify MAX_RESPONSE_BYTES constant is defined and reasonable."""
        self.assertIsInstance(MAX_RESPONSE_BYTES, int)
        self.assertGreater(MAX_RESPONSE_BYTES, 0)
        # Should be at least 1KB and at most 10MB
        self.assertGreaterEqual(MAX_RESPONSE_BYTES, 1024)
        self.assertLessEqual(MAX_RESPONSE_BYTES, 10 * 1024 * 1024)

    @patch("codebot.control_client.urllib.request.urlopen")
    def test_normal_response_works(self, mock_urlopen):
        """Test that normal-sized responses are read correctly."""
        normal_data = json.dumps({"status": "ok", "data": "test"}).encode()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = normal_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=None)
        mock_urlopen.return_value = mock_resp

        code, data = req("GET", "/health")
        self.assertEqual(code, 200)
        self.assertEqual(data, {"status": "ok", "data": "test"})
        # Verify read was called with MAX_RESPONSE_BYTES
        mock_resp.read.assert_called_once_with(MAX_RESPONSE_BYTES)

    @patch("codebot.control_client.urllib.request.urlopen")
    def test_large_response_is_truncated(self, mock_urlopen):
        """Test that oversized responses are truncated at MAX_RESPONSE_BYTES."""
        # Create data larger than MAX_RESPONSE_BYTES
        large_data = b"x" * (MAX_RESPONSE_BYTES + 1000)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = large_data[:MAX_RESPONSE_BYTES]  # Simulate truncation
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=None)
        mock_urlopen.return_value = mock_resp

        code, data = req("GET", "/health")
        self.assertEqual(code, 200)
        # Verify read was called with the size limit
        mock_resp.read.assert_called_once_with(MAX_RESPONSE_BYTES)

    @patch("codebot.control_client.urllib.request.urlopen")
    def test_http_error_body_read_is_bounded(self, mock_urlopen):
        """Test that HTTPError body reads are also bounded."""
        import urllib.error

        mock_fp = MagicMock()
        mock_fp.read.return_value = b'{"error": "not found"}'

        http_error = urllib.error.HTTPError(
            url="http://example.com/test",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=mock_fp,
        )
        mock_urlopen.side_effect = http_error

        code, data = req("GET", "/nonexistent")
        self.assertEqual(code, 404)
        # Verify read was called with MAX_RESPONSE_BYTES
        mock_fp.read.assert_called_once_with(MAX_RESPONSE_BYTES)

    @patch("codebot.control_client.urllib.request.urlopen")
    def test_http_error_large_body_is_truncated(self, mock_urlopen):
        """Test that large HTTPError bodies are truncated."""
        import urllib.error

        large_body = b"{" + b'"error": "' + b"x" * (MAX_RESPONSE_BYTES + 500) + b'"}'
        mock_fp = MagicMock()
        mock_fp.read.return_value = large_body[:MAX_RESPONSE_BYTES]

        http_error = urllib.error.HTTPError(
            url="http://example.com/test",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=mock_fp,
        )
        mock_urlopen.side_effect = http_error

        code, data = req("GET", "/error")
        self.assertEqual(code, 500)
        # Verify read was called with the size limit
        mock_fp.read.assert_called_once_with(MAX_RESPONSE_BYTES)

    @patch("codebot.control_client.urllib.request.urlopen")
    def test_empty_response_handled(self, mock_urlopen):
        """Test that empty responses are handled correctly."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b""
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=None)
        mock_urlopen.return_value = mock_resp

        code, data = req("GET", "/health")
        self.assertEqual(code, 200)
        self.assertEqual(data, {})

    @patch("codebot.control_client.urllib.request.urlopen")
    def test_no_unbounded_read_calls_in_code(self):
        """Verify no unbounded .read() calls exist in control_client source."""
        import inspect
        import codebot.control_client as cc

        source = inspect.getsource(cc)
        # Check that all resp.read() calls include an argument
        import re
        # Match resp.read() without arguments
        unbounded_reads = re.findall(r'resp\.read\(\)', source)
        self.assertEqual(len(unbounded_reads), 0, 
            f"Found unbounded resp.read() calls: {unbounded_reads}")


if __name__ == "__main__":
    unittest.main()
