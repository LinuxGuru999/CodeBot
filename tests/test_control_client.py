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
import sys
import unittest
from io import StringIO
from unittest.mock import patch, MagicMock

# Import the module under test
sys.path.insert(0, '/home/kozuka/Work/CodeBot')
from codebot.control_client import req, cmd_status, cmd_dead_letter_retry, cmd_scheduler_status, cmd_dead_letters


class TestReqFunction(unittest.TestCase):
    """Test the req() HTTP client function."""

    @patch('codebot.control_client.urllib.request.urlopen')
    @patch('codebot.control_client.URL', 'http://test-server:8081')
    @patch('codebot.control_client.TOKEN', '')
    def test_req_get_success(self, mock_urlopen):
        """req() sends correct GET request and parses JSON response."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"status": "ok", "count": 5}).encode()
        mock_response.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_response

        status, body = req("GET", "/bots")

        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "count": 5})
        # Verify request was constructed correctly
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        self.assertEqual(request_obj.method, "GET")
        self.assertEqual(request_obj.full_url, "http://test-server:8081/bots")
        self.assertIsNone(request_obj.data)

    @patch('codebot.control_client.urllib.request.urlopen')
    @patch('codebot.control_client.URL', 'http://test-server:8081')
    @patch('codebot.control_client.TOKEN', 'secret-token-123')
    def test_req_with_auth_header(self, mock_urlopen):
        """req() includes Authorization header when TOKEN is set."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"result": "success"}).encode()
        mock_response.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_response

        req("POST", "/control/drain", {"action": "stop"})

        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        # Headers are case-insensitive; urllib uses 'Content-type' (lowercase 't')
        self.assertEqual(request_obj.headers.get("Authorization"), "Bearer secret-token-123")
        # Check Content-Type with correct casing used by urllib.request
        self.assertIn("Content-type", request_obj.headers)
        self.assertEqual(request_obj.headers["Content-type"], "application/json")

    @patch('codebot.control_client.urllib.request.urlopen')
    @patch('codebot.control_client.URL', 'http://test-server:8081')
    @patch('codebot.control_client.TOKEN', '')
    def test_req_post_with_body(self, mock_urlopen):
        """req() sends POST request with JSON body."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"updated": True}).encode()
        mock_response.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_response

        body = {"item_id": "abc-123", "force": True}
        status, result = req("POST", "/scheduler/dead-letters/abc-123/retry", body)

        self.assertEqual(status, 200)
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        self.assertEqual(request_obj.method, "POST")
        self.assertEqual(request_obj.full_url, "http://test-server:8081/scheduler/dead-letters/abc-123/retry")
        sent_data = json.loads(request_obj.data.decode())
        self.assertEqual(sent_data, body)

    @patch('codebot.control_client.urllib.request.urlopen')
    @patch('codebot.control_client.URL', 'http://test-server:8081')
    def test_req_http_error_404(self, mock_urlopen):
        """req() handles HTTP 404 error gracefully."""
        from urllib.error import HTTPError
        mock_fp = MagicMock()
        mock_fp.read.return_value = json.dumps({"error": "not found"}).encode()
        http_error = HTTPError(
            url="http://test-server:8081/missing",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=mock_fp
        )
        mock_urlopen.side_effect = http_error

        status, body = req("GET", "/missing")

        self.assertEqual(status, 404)
        self.assertIsInstance(body, dict)
        self.assertIn("error", body)

    @patch('codebot.control_client.urllib.request.urlopen')
    @patch('codebot.control_client.URL', 'http://test-server:8081')
    def test_req_connection_error(self, mock_urlopen):
        """req() handles connection errors gracefully without raising."""
        mock_urlopen.side_effect = Exception("Connection refused")

        status, body = req("GET", "/bots")

        self.assertEqual(status, 0)
        self.assertIsInstance(body, dict)
        self.assertIn("error", body)
        self.assertIn("Connection refused", body["error"])

    @patch('codebot.control_client.urllib.request.urlopen')
    @patch('codebot.control_client.URL', 'http://test-server:8081')
    def test_req_empty_response(self, mock_urlopen):
        """req() handles empty response body."""
        mock_response = MagicMock()
        mock_response.read.return_value = b""
        mock_response.status = 204
        mock_urlopen.return_value.__enter__.return_value = mock_response

        status, body = req("DELETE", "/cache")

        self.assertEqual(status, 204)
        self.assertEqual(body, {})

    @patch('codebot.control_client.urllib.request.urlopen')
    @patch('codebot.control_client.URL', 'http://test-server:8081')
    def test_req_non_json_response(self, mock_urlopen):
        """req() returns raw string when response is not valid JSON."""
        mock_response = MagicMock()
        mock_response.read.return_value = b"Plain text response"
        mock_response.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_response

        status, body = req("GET", "/plain")

        self.assertEqual(status, 200)
        self.assertEqual(body, "Plain text response")


class TestCmdStatus(unittest.TestCase):
    """Test cmd_status() output parsing."""

    @patch('codebot.control_client.req')
    def test_cmd_status_success(self, mock_req):
        """cmd_status() prints formatted bot status on success."""
        mock_req.return_value = (200, [
            {"name": "bug_hunter", "running": True, "heartbeat_age_seconds": 15, "effective_timeout": 300, "risk": "low", "model": "qwen-3.5-plus", "next_run_in_seconds": 45},
            {"name": "security_auditor", "running": False, "heartbeat_age_seconds": None, "effective_timeout": 600, "risk": "high", "model": "claude-sonnet-4", "next_run_in_seconds": None}
        ])

        captured_output = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured_output

        try:
            cmd_status()
        finally:
            sys.stdout = old_stdout

        output = captured_output.getvalue()
        self.assertIn("bug_hunter", output)
        self.assertIn("RUN", output)
        self.assertIn("security_auditor", output)
        self.assertIn("WAIT", output)
        self.assertIn("hb=15s", output)

    @patch('codebot.control_client.req')
    def test_cmd_status_failure(self, mock_req):
        """cmd_status() prints error and exits on non-200 response."""
        mock_req.return_value = (500, {"error": "Internal server error"})

        captured_output = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured_output

        try:
            with self.assertRaises(SystemExit) as cm:
                cmd_status()
            self.assertEqual(cm.exception.code, 1)
        finally:
            sys.stdout = old_stdout

        output = captured_output.getvalue()
        self.assertIn("error", output.lower())


class TestCmdDeadLetterRetry(unittest.TestCase):
    """Test cmd_dead_letter_retry() request construction."""

    @patch('codebot.control_client.req')
    def test_cmd_dead_letter_retry_success(self, mock_req):
        """cmd_dead_letter_retry() sends POST to correct endpoint."""
        mock_req.return_value = (200, {"retried": True, "item_id": "cb-xyz-789"})

        captured_output = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured_output

        try:
            cmd_dead_letter_retry("cb-xyz-789")
        finally:
            sys.stdout = old_stdout

        # Verify req was called with correct arguments
        mock_req.assert_called_once_with(
            "POST",
            "/scheduler/dead-letters/cb-xyz-789/retry",
            {}
        )

        output = captured_output.getvalue()
        self.assertIn("retried", output)

    @patch('codebot.control_client.req')
    def test_cmd_dead_letter_retry_failure(self, mock_req):
        """cmd_dead_letter_retry() exits with code 1 on failure."""
        mock_req.return_value = (400, {"error": "Invalid item ID"})

        captured_output = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured_output

        try:
            with self.assertRaises(SystemExit) as cm:
                cmd_dead_letter_retry("invalid-id")
            self.assertEqual(cm.exception.code, 1)
        finally:
            sys.stdout = old_stdout


class TestCmdSchedulerStatus(unittest.TestCase):
    """Test cmd_scheduler_status() bounded output."""

    @patch('codebot.control_client.req')
    def test_cmd_scheduler_status_success(self, mock_req):
        """cmd_scheduler_status() prints bounded JSON on success."""
        mock_payload = {
            "version": "1.2.3",
            "budget_state": "healthy",
            "budget_day": 1000,
            "budget_total_actual": 5000,
            "drain": False,
            "dead_letter_count": 3,
            "dead_letter_ids": ["dl-1", "dl-2", "dl-3", "dl-4"],  # Should be truncated to 20
            "queue_tracked": True,
            "lease_active": True,
            "paused_count": 2,
            "paused_bots": ["bot-a", "bot-b"],
            "disabled_count": 1,
            "disabled_bots": ["bot-c"],
            "disabled_details": [{"bot": "bot-c", "reason": "manual"}],
            "starved_oldest": "bot-d",
            "starved_top": ["bot-d", "bot-e"],
            "batch_utilization": 0.85,
            "per_model_actual": {"qwen": 100, "claude": 200}
        }
        mock_req.return_value = (200, mock_payload)

        captured_output = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured_output

        try:
            cmd_scheduler_status()
        finally:
            sys.stdout = old_stdout

        output = captured_output.getvalue()
        parsed = json.loads(output)
        self.assertEqual(parsed["version"], "1.2.3")
        # dead_letter_ids is truncated to 20 max; original had 4 so all should be kept
        self.assertEqual(len(parsed["dead_letter_ids"]), 4)
        self.assertIn("budget_state", parsed)

    @patch('codebot.control_client.req')
    def test_cmd_scheduler_status_non_dict_response(self, mock_req):
        """cmd_scheduler_status() handles non-dict response gracefully."""
        mock_req.return_value = (200, "unexpected string response")

        captured_output = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured_output

        try:
            with self.assertRaises(SystemExit) as cm:
                cmd_scheduler_status()
            self.assertEqual(cm.exception.code, 1)
        finally:
            sys.stdout = old_stdout


class TestCmdDeadLetters(unittest.TestCase):
    """Test cmd_dead_letters() retrieval."""

    @patch('codebot.control_client.req')
    def test_cmd_dead_letters_success(self, mock_req):
        """cmd_dead_letters() fetches and prints dead letters."""
        mock_req.return_value = (200, [{"id": "dl-1", "error": "timeout"}, {"id": "dl-2", "error": "rate_limit"}])

        captured_output = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured_output

        try:
            cmd_dead_letters()
        finally:
            sys.stdout = old_stdout

        output = captured_output.getvalue()
        self.assertIn("dl-1", output)
        self.assertIn("dl-2", output)
        mock_req.assert_called_once_with("GET", "/scheduler/dead-letters")

    @patch('codebot.control_client.req')
    def test_cmd_dead_letters_failure(self, mock_req):
        """cmd_dead_letters() exits on error response."""
        mock_req.return_value = (503, {"error": "Service unavailable"})

        captured_output = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured_output

        try:
            with self.assertRaises(SystemExit) as cm:
                cmd_dead_letters()
            self.assertEqual(cm.exception.code, 1)
        finally:
            sys.stdout = old_stdout


if __name__ == "__main__":
    unittest.main()
