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
from unittest.mock import patch, MagicMock
from io import StringIO

# Import the module under test
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from codebot import control_client


class TestReqFunction(unittest.TestCase):
    """Test the req() function which handles all HTTP communication."""

    def setUp(self):
        """Set up test fixtures."""
        # Reset to defaults for each test
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"

    def tearDown(self):
        """Tear down test fixtures."""
        if self.original_url is None:
            os.environ.pop("CONTROL_URL", None)
        else:
            os.environ["CONTROL_URL"] = self.original_url
        
        if self.original_token is None:
            os.environ.pop("CONTROL_TOKEN", None)
        else:
            os.environ["CONTROL_TOKEN"] = self.original_token

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_get_success_json_response(self, mock_urlopen):
        """Test req() sends correct GET request and parses JSON response."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"status": "ok", "count": 5}'
        mock_response.status = 200
        mock_response.__enter__ = lambda self: self
        mock_response.__exit__ = lambda self, *args: None
        mock_response.headers = {"Content-Type": "application/json"}
        mock_urlopen.return_value = mock_response

        status, body = control_client.req("GET", "/bots")

        # Verify request construction
        mock_urlopen.assert_called_once()
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        
        # Check method
        self.assertEqual(request_obj.method, "GET")
        # Check URL
        self.assertEqual(request_obj.full_url, "http://127.0.0.1:8081/bots")
        # Check headers - note: Request object stores headers differently
        self.assertIn("Content-Type", str(request_obj.headers))
        self.assertIn("Authorization", str(request_obj.headers))
        # Check no body for GET
        self.assertIsNone(request_obj.data)
        
        # Verify response parsing
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "count": 5})

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_post_with_body(self, mock_urlopen):
        """Test req() sends POST with correct body and headers."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"result": "success"}'
        mock_response.status = 200
        mock_response.__enter__ = lambda self: self
        mock_response.__exit__ = lambda self, *args: None
        mock_urlopen.return_value = mock_response

        test_body = {"action": "retry", "item_id": "123"}
        status, body = control_client.req("POST", "/scheduler/dead-letters/123/retry", test_body)

        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        
        # Check method
        self.assertEqual(request_obj.method, "POST")
        # Check URL
        self.assertEqual(request_obj.full_url, "http://127.0.0.1:8081/scheduler/dead-letters/123/retry")
        # Check body is JSON encoded
        self.assertIsNotNone(request_obj.data)
        sent_body = json.loads(request_obj.data.decode())
        self.assertEqual(sent_body, test_body)
        
        self.assertEqual(status, 200)
        self.assertEqual(body, {"result": "success"})

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_no_token_when_unset(self, mock_urlopen):
        """Test req() omits Authorization header when TOKEN is empty."""
        os.environ["CONTROL_TOKEN"] = ""
        
        mock_response = MagicMock()
        mock_response.read.return_value = b'{}'
        mock_response.status = 200
        mock_response.__enter__ = lambda self: self
        mock_response.__exit__ = lambda self, *args: None
        mock_urlopen.return_value = mock_response

        control_client.req("GET", "/health")

        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        
        # Should not have Authorization header
        self.assertNotIn("Authorization", request_obj.headers)

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_http_error_404(self, mock_urlopen):
        """Test req() handles HTTP 404 error gracefully."""
        from urllib.error import HTTPError
        mock_error = HTTPError(
            url="http://127.0.0.1:8081/notfound",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=MagicMock(read=lambda n: b'{"error": "not found"}')
        )
        mock_urlopen.side_effect = mock_error

        status, body = control_client.req("GET", "/notfound")

        self.assertEqual(status, 404)
        self.assertIsInstance(body, dict)
        self.assertIn("error", body)

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_connection_error(self, mock_urlopen):
        """Test req() handles connection errors gracefully (fail open)."""
        mock_urlopen.side_effect = Exception("Connection refused")

        status, body = control_client.req("GET", "/bots")

        # Should return 0 status and error dict, never raise
        self.assertEqual(status, 0)
        self.assertIsInstance(body, dict)
        self.assertIn("error", body)
        self.assertIn("Connection refused", body["error"])

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_raw_string_response(self, mock_urlopen):
        """Test req() handles non-JSON response gracefully."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'Plain text response'
        mock_response.status = 200
        mock_response.__enter__ = lambda self: self
        mock_response.__exit__ = lambda self, *args: None
        mock_urlopen.return_value = mock_response

        status, body = control_client.req("GET", "/plain")

        self.assertEqual(status, 200)
        self.assertEqual(body, "Plain text response")

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_empty_response(self, mock_urlopen):
        """Test req() handles empty response as empty dict."""
        mock_response = MagicMock()
        mock_response.read.return_value = b''
        mock_response.status = 204
        mock_response.__enter__ = lambda self: self
        mock_response.__exit__ = lambda self, *args: None
        mock_urlopen.return_value = mock_response

        status, body = control_client.req("DELETE", "/item/1")

        self.assertEqual(status, 204)
        self.assertEqual(body, {})


class TestCmdStatus(unittest.TestCase):
    """Test cmd_status() output parsing."""

    def setUp(self):
        """Set up test fixtures."""
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"
        self.held_output = StringIO()
        self.original_stdout = sys.stdout
        sys.stdout = self.held_output

    def tearDown(self):
        """Tear down test fixtures."""
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
    def test_cmd_status_success_format(self, mock_req):
        """Test cmd_status() parses and formats JSON response correctly."""
        mock_req.return_value = (200, [
            {"name": "bug_hunter", "running": True, "heartbeat_age_seconds": 30, 
             "effective_timeout": 300, "risk": "low", "model": "qwen-3.5-plus", "next_run_in_seconds": 60},
            {"name": "security_auditor", "running": False, "heartbeat_age_seconds": None,
             "effective_timeout": 300, "risk": "medium", "model": "claude-sonnet-4", "next_run_in_seconds": None}
        ])

        control_client.cmd_status()
        output = self.held_output.getvalue()

        lines = output.strip().split('\n')
        self.assertEqual(len(lines), 2)
        self.assertIn("bug_hunter", lines[0])
        self.assertIn("RUN", lines[0])
        self.assertIn("security_auditor", lines[1])
        self.assertIn("WAIT", lines[1])

    @patch('codebot.control_client.req')
    def test_cmd_status_error_exit(self, mock_req):
        """Test cmd_status() exits with code 1 on error response."""
        mock_req.return_value = (500, {"error": "Internal server error"})

        with self.assertRaises(SystemExit) as cm:
            control_client.cmd_status()
        
        self.assertEqual(cm.exception.code, 1)
        output = self.held_output.getvalue()
        self.assertIn("error", output.lower())


class TestCmdDeadLetterRetry(unittest.TestCase):
    """Test cmd_dead_letter_retry() request construction."""

    def setUp(self):
        """Set up test fixtures."""
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"
        self.held_output = StringIO()
        self.original_stdout = sys.stdout
        sys.stdout = self.held_output

    def tearDown(self):
        """Tear down test fixtures."""
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
    def test_cmd_dead_letter_retry_correct_endpoint(self, mock_req):
        """Test cmd_dead_letter_retry() sends POST to correct endpoint with empty body."""
        mock_req.return_value = (200, {"status": "queued"})

        control_client.cmd_dead_letter_retry("item-123")

        mock_req.assert_called_once_with(
            "POST",
            "/scheduler/dead-letters/item-123/retry",
            {}
        )

    @patch('codebot.control_client.req')
    def test_cmd_dead_letter_retry_error_exit(self, mock_req):
        """Test cmd_dead_letter_retry() exits with code 1 on error."""
        mock_req.return_value = (404, {"error": "Item not found"})

        with self.assertRaises(SystemExit) as cm:
            control_client.cmd_dead_letter_retry("nonexistent-item")
        
        self.assertEqual(cm.exception.code, 1)


class TestCmdSchedulerStatus(unittest.TestCase):
    """Test cmd_scheduler_status() bounded output."""

    def setUp(self):
        """Set up test fixtures."""
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"
        self.held_output = StringIO()
        self.original_stdout = sys.stdout
        sys.stdout = self.held_output

    def tearDown(self):
        """Tear down test fixtures."""
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
    def test_cmd_scheduler_status_bounded_arrays(self, mock_req):
        """Test cmd_scheduler_status() bounds array outputs to prevent huge responses."""
        large_list = [{"id": i} for i in range(100)]
        mock_req.return_value = (200, {
            "version": "1.0",
            "dead_letter_ids": large_list,
            "paused_bots": large_list,
            "disabled_bots": large_list,
            "disabled_details": large_list,
            "starved_top": large_list,
            "per_model_actual": {f"model-{i}": i for i in range(100)}
        })

        control_client.cmd_scheduler_status()
        output = self.held_output.getvalue()
        result = json.loads(output)

        # Verify bounding
        self.assertLessEqual(len(result.get("dead_letter_ids", [])), 20)
        self.assertLessEqual(len(result.get("paused_bots", [])), 20)
        self.assertLessEqual(len(result.get("disabled_bots", [])), 20)
        self.assertLessEqual(len(result.get("disabled_details", [])), 20)
        self.assertLessEqual(len(result.get("starved_top", [])), 5)
        self.assertLessEqual(len(result.get("per_model_actual", {})), 32)

    @patch('codebot.control_client.req')
    def test_cmd_scheduler_status_non_dict_response(self, mock_req):
        """Test cmd_scheduler_status() handles non-dict response gracefully."""
        mock_req.return_value = (200, {"version": "1.0", "dead_letter_ids": []})

        control_client.cmd_scheduler_status()
        output = self.held_output.getvalue()
        
        # Should produce bounded JSON output
        result = json.loads(output)
        self.assertEqual(result["version"], "1.0")


class TestCmdSchedulerEvents(unittest.TestCase):
    """Test cmd_scheduler_events() parameter handling."""

    def setUp(self):
        """Set up test fixtures."""
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"
        self.held_output = StringIO()
        self.original_stdout = sys.stdout
        sys.stdout = self.held_output

    def tearDown(self):
        """Tear down test fixtures."""
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
    def test_cmd_scheduler_events_limit_bounds(self, mock_req):
        """Test cmd_scheduler_events() bounds limit between 1 and 100."""
        mock_req.return_value = (200, [])

        # Test below minimum
        control_client.cmd_scheduler_events(limit="0")
        call_args = mock_req.call_args
        self.assertIn("limit=1", call_args[0][1])

        mock_req.reset_mock()
        
        # Test above maximum
        control_client.cmd_scheduler_events(limit="999")
        call_args = mock_req.call_args
        self.assertIn("limit=100", call_args[0][1])

    @patch('codebot.control_client.sys.exit')
    @patch('codebot.control_client.req')
    def test_cmd_scheduler_events_invalid_limit(self, mock_req, mock_exit):
        """Test cmd_scheduler_events() handles invalid limit gracefully."""
        control_client.cmd_scheduler_events(limit="invalid")
        output = self.held_output.getvalue()
        
        self.assertIn("must be an integer", output)
        mock_exit.assert_called_once_with(1)

    @patch('codebot.control_client.req')
    def test_cmd_scheduler_events_with_type_filter(self, mock_req):
        """Test cmd_scheduler_events() includes type filter in URL."""
        mock_req.return_value = (200, [])

        control_client.cmd_scheduler_events(limit="10", event_type="timeout")
        
        call_args = mock_req.call_args
        path = call_args[0][1]
        self.assertIn("limit=10", path)
        self.assertIn("type=", path)


class TestMainCLI(unittest.TestCase):
    """Test main() CLI command routing."""

    def setUp(self):
        """Set up test fixtures."""
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"

    def tearDown(self):
        """Tear down test fixtures."""
        if self.original_url is None:
            os.environ.pop("CONTROL_URL", None)
        else:
            os.environ["CONTROL_URL"] = self.original_url
        if self.original_token is None:
            os.environ.pop("CONTROL_TOKEN", None)
        else:
            os.environ["CONTROL_TOKEN"] = self.original_token

    @patch('codebot.control_client.cmd_status')
    @patch('codebot.control_client.sys.argv', ['control_client.py', 'status'])
    def test_main_routes_to_status(self, mock_cmd_status):
        """Test main() routes 'status' command correctly."""
        with self.assertRaises(SystemExit):
            control_client.main()
        mock_cmd_status.assert_called_once()

    @patch('codebot.control_client.cmd_dead_letter_retry')
    @patch('codebot.control_client.sys.argv', ['control_client.py', 'retry-dead-letter', 'item-456'])
    def test_main_routes_to_retry_dead_letter(self, mock_cmd_retry):
        """Test main() routes 'retry-dead-letter' command correctly."""
        with self.assertRaises(SystemExit):
            control_client.main()
        mock_cmd_retry.assert_called_once_with("item-456")

    @patch('codebot.control_client.sys.argv', ['control_client.py'])
    def test_main_shows_help_on_no_args(self, mock_argv):
        """Test main() shows help when no arguments provided."""
        with self.assertRaises(SystemExit) as cm:
            control_client.main()
        self.assertEqual(cm.exception.code, 1)

    @patch('codebot.control_client.sys.argv', ['control_client.py', 'unknown-command'])
    def test_main_exits_on_unknown_command(self, mock_argv):
        """Test main() exits with code 2 on unknown command."""
        with self.assertRaises(SystemExit) as cm:
            control_client.main()
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
