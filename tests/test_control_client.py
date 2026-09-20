#!/usr/bin/env python3
"""Tests for control_client.py HTTP client and CLI commands.

Covers:
- req() with mocked HTTP responses (method, path, body, headers)
- cmd_status output parsing
- cmd_dead_letter_retry request construction
- Error handling for connection failures and HTTP errors
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from io import StringIO
from unittest.mock import patch, MagicMock

# Import the module under test
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from codebot import control_client


class TestReqFunction(unittest.TestCase):
    """Test the req() HTTP client function."""

    def setUp(self):
        """Reset environment variables before each test."""
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        # Default to localhost for tests
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"

    def tearDown(self):
        """Restore original environment variables."""
        if self.original_url is None:
            os.environ.pop("CONTROL_URL", None)
        else:
            os.environ["CONTROL_URL"] = self.original_url
        if self.original_token is None:
            os.environ.pop("CONTROL_TOKEN", None)
        else:
            os.environ["CONTROL_TOKEN"] = self.original_token

    @patch('urllib.request.Request')
    @patch('urllib.request.urlopen')
    def test_req_get_success_json(self, mock_urlopen, mock_request):
        """req() sends correct GET request and parses JSON response."""
        # Setup mock request object
        mock_req_instance = MagicMock()
        mock_req_instance.method = "GET"
        mock_req_instance.full_url = "http://127.0.0.1:8081/bots"
        mock_request.return_value = mock_req_instance
        
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"status": "ok", "count": 5}).encode()
        mock_response.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_response

        status, body = control_client.req("GET", "/bots")

        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "count": 5})
        # Verify Request was called with correct headers
        mock_request.assert_called_once()
        call_kwargs = mock_request.call_args[1]
        self.assertIn("Authorization", call_kwargs["headers"])
        self.assertEqual(call_kwargs["headers"]["Authorization"], "Bearer test-token")

    @patch('urllib.request.urlopen')
    def test_req_post_with_body(self, mock_urlopen):
        """req() sends POST with JSON body correctly."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"result": "created"}).encode()
        mock_response.status = 201
        mock_urlopen.return_value.__enter__.return_value = mock_response

        status, body = control_client.req("POST", "/control/drain", {"reason": "maintenance"})

        self.assertEqual(status, 201)
        self.assertEqual(body, {"result": "created"})
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        self.assertEqual(request.method, "POST")
        self.assertTrue(request.full_url.endswith("/control/drain"))
        # Verify body was sent
        self.assertIsNotNone(request.data)
        sent_body = json.loads(request.data.decode())
        self.assertEqual(sent_body, {"reason": "maintenance"})

    @patch('urllib.request.urlopen')
    def test_req_no_token_when_unset(self, mock_urlopen):
        """req() omits Authorization header when TOKEN is empty."""
        os.environ["CONTROL_TOKEN"] = ""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{}'
        mock_response.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_response

        status, body = control_client.req("GET", "/health")

        self.assertEqual(status, 200)
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        self.assertNotIn("Authorization", request.headers)

    @patch('urllib.request.urlopen')
    def test_req_http_error_404(self, mock_urlopen):
        """req() handles HTTP 404 error gracefully."""
        from urllib.error import HTTPError
        mock_fp = MagicMock()
        mock_fp.read.return_value = json.dumps({"error": "not found"}).encode()
        http_error = HTTPError("http://example.com", 404, "Not Found", {}, mock_fp)
        mock_urlopen.side_effect = http_error

        status, body = control_client.req("GET", "/nonexistent")

        self.assertEqual(status, 404)
        self.assertIsInstance(body, dict)
        self.assertIn("error", body)

    @patch('urllib.request.urlopen')
    def test_req_connection_error(self, mock_urlopen):
        """req() handles connection errors gracefully, returning code 0."""
        mock_urlopen.side_effect = Exception("Connection refused")

        status, body = control_client.req("GET", "/bots")

        self.assertEqual(status, 0)
        self.assertIsInstance(body, dict)
        self.assertIn("error", body)
        self.assertIn("Connection refused", body["error"])

    @patch('urllib.request.urlopen')
    def test_req_non_json_response(self, mock_urlopen):
        """req() returns raw string when response is not valid JSON."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'Plain text response'
        mock_response.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_response

        status, body = control_client.req("GET", "/plain")

        self.assertEqual(status, 200)
        self.assertEqual(body, "Plain text response")

    @patch('urllib.request.urlopen')
    def test_req_empty_response(self, mock_urlopen):
        """req() returns empty dict for empty successful response."""
        mock_response = MagicMock()
        mock_response.read.return_value = b''
        mock_response.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_response

        status, body = control_client.req("POST", "/empty")

        self.assertEqual(status, 200)
        self.assertEqual(body, {})


class TestCmdStatus(unittest.TestCase):
    """Test cmd_status() output parsing."""

    def setUp(self):
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = ""
        self.held_output = StringIO()
        self.original_stdout = sys.stdout
        sys.stdout = self.held_output

    def tearDown(self):
        sys.stdout = self.original_stdout
        if self.original_url is None:
            os.environ.pop("CONTROL_URL", None)
        else:
            os.environ["CONTROL_URL"] = self.original_url
        if self.original_token is None:
            os.environ.pop("CONTROL_TOKEN", None)
        else:
            os.environ["CONTROL_TOKEN"] = self.original_token

    @patch('codebot.control_client.req')
    def test_cmd_status_success(self, mock_req):
        """cmd_status() parses JSON response and prints formatted output."""
        mock_req.return_value = (200, [
            {"name": "bug_hunter", "running": True, "heartbeat_age_seconds": 30, "effective_timeout": 300, "risk": "low", "model": "qwen-3.5-plus", "next_run_in_seconds": 60},
            {"name": "security_auditor", "running": False, "heartbeat_age_seconds": None, "effective_timeout": 300, "risk": "high", "model": "claude-3.5", "next_run_in_seconds": None}
        ])

        control_client.cmd_status()

        output = self.held_output.getvalue()
        self.assertIn("bug_hunter", output)
        self.assertIn("RUN", output)
        self.assertIn("security_auditor", output)
        self.assertIn("WAIT", output)
        self.assertIn("hb=30s", output)

    @patch('codebot.control_client.req')
    def test_cmd_status_failure(self, mock_req):
        """cmd_status() prints error and exits on non-200 response."""
        mock_req.return_value = (500, {"error": "Internal server error"})

        with self.assertRaises(SystemExit) as cm:
            control_client.cmd_status()

        self.assertEqual(cm.exception.code, 1)
        output = self.held_output.getvalue()
        self.assertIn("error", output.lower())


class TestCmdDeadLetterRetry(unittest.TestCase):
    """Test cmd_dead_letter_retry() request construction."""

    def setUp(self):
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"
        self.held_output = StringIO()
        self.original_stdout = sys.stdout
        sys.stdout = self.held_output

    def tearDown(self):
        sys.stdout = self.original_stdout
        if self.original_url is None:
            os.environ.pop("CONTROL_URL", None)
        else:
            os.environ["CONTROL_URL"] = self.original_url
        if self.original_token is None:
            os.environ.pop("CONTROL_TOKEN", None)
        else:
            os.environ["CONTROL_TOKEN"] = self.original_token

    @patch('codebot.control_client.req')
    def test_cmd_dead_letter_retry_success(self, mock_req):
        """cmd_dead_letter_retry() sends POST to correct endpoint."""
        mock_req.return_value = (200, {"status": "retried", "item_id": "CB-123"})

        control_client.cmd_dead_letter_retry("CB-123")

        mock_req.assert_called_once_with("POST", "/scheduler/dead-letters/CB-123/retry", {})
        output = self.held_output.getvalue()
        self.assertIn("retried", output)

    @patch('codebot.control_client.req')
    def test_cmd_dead_letter_retry_failure(self, mock_req):
        """cmd_dead_letter_retry() exits with code 1 on failure."""
        mock_req.return_value = (404, {"error": "Item not found"})

        with self.assertRaises(SystemExit) as cm:
            control_client.cmd_dead_letter_retry("CB-999")

        self.assertEqual(cm.exception.code, 1)
        mock_req.assert_called_once_with("POST", "/scheduler/dead-letters/CB-999/retry", {})


class TestEdgeCases(unittest.TestCase):
    """Test edge cases and error paths."""

    def setUp(self):
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = ""

    def tearDown(self):
        if self.original_url is None:
            os.environ.pop("CONTROL_URL", None)
        else:
            os.environ["CONTROL_URL"] = self.original_url
        if self.original_token is None:
            os.environ.pop("CONTROL_TOKEN", None)
        else:
            os.environ["CONTROL_TOKEN"] = self.original_token

    @patch('urllib.request.urlopen')
    def test_req_url_trailing_slash_handling(self, mock_urlopen):
        """req() handles URL with or without trailing slash."""
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081/"
        mock_response = MagicMock()
        mock_response.read.return_value = b'{}'
        mock_response.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_response

        control_client.req("GET", "/bots")

        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        # Should not have double slashes
        self.assertNotIn("//bots", request.full_url.replace("://", ""))

    @patch('urllib.request.urlopen')
    def test_req_timeout_setting(self, mock_urlopen):
        """req() uses 15 second timeout."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{}'
        mock_response.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_response

        control_client.req("GET", "/health")

        # Verify timeout=15 was passed
        call_kwargs = mock_urlopen.call_args[1]
        self.assertEqual(call_kwargs.get("timeout"), 15)

    @patch('codebot.control_client.req')
    def test_cmd_scheduler_status_missing_fields(self, mock_req):
        """cmd_scheduler_status() handles missing fields gracefully."""
        mock_req.return_value = (200, {"version": "1.0"})  # Minimal response

        self.held_output = StringIO()
        self.original_stdout = sys.stdout
        sys.stdout = self.held_output

        try:
            control_client.cmd_scheduler_status()
            output = self.held_output.getvalue()
            self.assertIn("version", output)
        finally:
            sys.stdout = self.original_stdout


if __name__ == "__main__":
    unittest.main()
