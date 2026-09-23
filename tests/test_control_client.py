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
import importlib
from io import StringIO
from unittest.mock import patch, MagicMock

# Import the module under test
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from codebot import control_client


class TestReqFunction(unittest.TestCase):
    """Test the req() function which handles all HTTP communication."""

    def setUp(self):
        """Set up test fixtures."""
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"
        importlib.reload(control_client)

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
        importlib.reload(control_client)

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_get_success_json_response(self, mock_urlopen):
        """Test req() sends correct GET request and parses JSON response."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"status": "ok", "count": 5}'
        mock_response.status = 200
        mock_response.__enter__ = lambda self: self
        mock_response.__exit__ = lambda self, *args: None
        mock_urlopen.return_value = mock_response

        status, body = control_client.req("GET", "/bots")

        mock_urlopen.assert_called_once()
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]

        self.assertEqual(request_obj.method, "GET")
        self.assertEqual(request_obj.full_url, "http://127.0.0.1:8081/bots")
        # Check headers exist (case-insensitive check via str representation)
        headers_str = str(request_obj.headers)
        self.assertIn("application/json", headers_str)
        self.assertIn("Bearer", headers_str)
        self.assertIsNone(request_obj.data)

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

        self.assertEqual(request_obj.method, "POST")
        self.assertEqual(request_obj.full_url, "http://127.0.0.1:8081/scheduler/dead-letters/123/retry")
        self.assertIsNotNone(request_obj.data)
        sent_body = json.loads(request_obj.data.decode())
        self.assertEqual(sent_body, test_body)

        self.assertEqual(status, 200)
        self.assertEqual(body, {"result": "success"})

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_no_token_when_unset(self, mock_urlopen):
        """Test req() omits Authorization header when TOKEN is empty."""
        os.environ["CONTROL_TOKEN"] = ""
        importlib.reload(control_client)

        mock_response = MagicMock()
        mock_response.read.return_value = b'{}'
        mock_response.status = 200
        mock_response.__enter__ = lambda self: self
        mock_response.__exit__ = lambda self, *args: None
        mock_urlopen.return_value = mock_response

        control_client.req("GET", "/health")

        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]

        self.assertNotIn("Authorization", request_obj.headers)

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_http_error_404(self, mock_urlopen):
        """Test req() handles HTTP 404 error gracefully."""
        from urllib.error import HTTPError
        mock_fp = MagicMock()
        mock_fp.read.return_value = b'{"error": "not found"}'
        http_error = HTTPError(
            url="http://127.0.0.1:8081/notfound",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=mock_fp
        )
        mock_urlopen.side_effect = http_error

        status, body = control_client.req("GET", "/notfound")

        self.assertEqual(status, 404)
        self.assertIsInstance(body, dict)
        self.assertIn("error", body)

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_connection_error(self, mock_urlopen):
        """Test req() handles connection errors gracefully (fail open)."""
        mock_urlopen.side_effect = Exception("Connection refused")

        status, body = control_client.req("GET", "/bots")

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

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_http_error_with_raw_body(self, mock_urlopen):
        """Test req() handles HTTPError with non-JSON raw body gracefully."""
        from urllib.error import HTTPError
        mock_fp = MagicMock()
        mock_fp.read.return_value = b'Internal Server Error - plain text'
        http_error = HTTPError(
            url="http://127.0.0.1:8081/error",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=mock_fp
        )
        mock_urlopen.side_effect = http_error

        status, body = control_client.req("GET", "/error")

        self.assertEqual(status, 500)
        self.assertIsInstance(body, dict)
        self.assertIn("error", body)
        self.assertIn("Internal Server Error", body["error"])
        self.assertEqual(body.get("code"), 500)

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_connection_exception_returns_fail_open(self, mock_urlopen):
        """Test req() returns (0, error_dict) on connection exceptions like URLError."""
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        status, body = control_client.req("GET", "/bots")

        self.assertEqual(status, 0)
        self.assertIsInstance(body, dict)
        self.assertIn("error", body)
        self.assertIn("Connection refused", body["error"])

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_url_trailing_slash_normalization(self, mock_urlopen):
        """Test req() normalizes URL by stripping trailing slash from CONTROL_URL."""
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081/"
        os.environ["CONTROL_TOKEN"] = "test-token"
        importlib.reload(control_client)

        mock_response = MagicMock()
        mock_response.read.return_value = b'{"status": "ok"}'
        mock_response.status = 200
        mock_response.__enter__ = lambda self: self
        mock_response.__exit__ = lambda self, *args: None
        mock_urlopen.return_value = mock_response

        status, body = control_client.req("GET", "/health")

        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        # URL should not have double slashes
        self.assertEqual(request_obj.full_url, "http://127.0.0.1:8081/health")

        # Restore
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        importlib.reload(control_client)


class TestCmdStatus(unittest.TestCase):
    """Test cmd_status() output parsing."""

    def setUp(self):
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"
        importlib.reload(control_client)
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
        importlib.reload(control_client)

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
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"
        importlib.reload(control_client)
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
        importlib.reload(control_client)

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
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"
        importlib.reload(control_client)
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
        importlib.reload(control_client)

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

        self.assertLessEqual(len(result.get("dead_letter_ids", [])), 20)
        self.assertLessEqual(len(result.get("paused_bots", [])), 20)
        self.assertLessEqual(len(result.get("disabled_bots", [])), 20)
        self.assertLessEqual(len(result.get("disabled_details", [])), 20)
        self.assertLessEqual(len(result.get("starved_top", [])), 5)
        self.assertLessEqual(len(result.get("per_model_actual", {})), 32)

    @patch('codebot.control_client.req')
    def test_cmd_scheduler_status_non_dict_response(self, mock_req):
        """Test cmd_scheduler_status() handles non-dict response gracefully."""
        mock_req.return_value = (200, "not a dict")

        with self.assertRaises(SystemExit) as cm:
            control_client.cmd_scheduler_status()

        self.assertEqual(cm.exception.code, 1)
        output = self.held_output.getvalue()
        self.assertIn("not a dict", output)


class TestCmdSchedulerEvents(unittest.TestCase):
    """Test cmd_scheduler_events() parameter handling."""

    def setUp(self):
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"
        importlib.reload(control_client)
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
        importlib.reload(control_client)

    @patch('codebot.control_client.req')
    def test_cmd_scheduler_events_limit_bounds(self, mock_req):
        """Test cmd_scheduler_events() bounds limit between 1 and 100."""
        mock_req.return_value = (200, [])

        control_client.cmd_scheduler_events(limit="0")
        call_args = mock_req.call_args
        self.assertIn("limit=1", call_args[0][1])

        mock_req.reset_mock()

        control_client.cmd_scheduler_events(limit="999")
        call_args = mock_req.call_args
        self.assertIn("limit=100", call_args[0][1])

    @patch('codebot.control_client.sys.exit', side_effect=SystemExit(1))
    def test_cmd_scheduler_events_invalid_limit(self, mock_exit):
        """Test cmd_scheduler_events() handles invalid limit gracefully."""
        with self.assertRaises(SystemExit):
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
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        self.original_argv = sys.argv
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"
        importlib.reload(control_client)

    def tearDown(self):
        sys.argv = self.original_argv
        if self.original_url is None:
            os.environ.pop("CONTROL_URL", None)
        else:
            os.environ["CONTROL_URL"] = self.original_url
        if self.original_token is None:
            os.environ.pop("CONTROL_TOKEN", None)
        else:
            os.environ["CONTROL_TOKEN"] = self.original_token
        importlib.reload(control_client)

    @patch('codebot.control_client.cmd_status')
    def test_main_routes_to_status(self, mock_cmd_status):
        """Test main() routes 'status' command correctly."""
        sys.argv = ['control_client.py', 'status']
        # cmd_status doesn't exit on success (code 200), so no SystemExit expected
        control_client.main()
        mock_cmd_status.assert_called_once()

    @patch('codebot.control_client.cmd_dead_letter_retry')
    def test_main_routes_to_retry_dead_letter(self, mock_cmd_retry):
        """Test main() routes 'retry-dead-letter' command correctly."""
        sys.argv = ['control_client.py', 'retry-dead-letter', 'item-456']
        # cmd_dead_letter_retry doesn't exit on success (code 200), so no SystemExit expected
        control_client.main()
        mock_cmd_retry.assert_called_once_with("item-456")

    def test_main_shows_help_on_no_args(self):
        """Test main() shows help when no arguments provided."""
        sys.argv = ['control_client.py']
        with self.assertRaises(SystemExit) as cm:
            control_client.main()
        self.assertEqual(cm.exception.code, 1)

    def test_main_exits_on_unknown_command(self):
        """Test main() exits with code 2 on unknown command."""
        sys.argv = ['control_client.py', 'unknown-command']
        with self.assertRaises(SystemExit) as cm:
            control_client.main()
        self.assertEqual(cm.exception.code, 2)


class TestPlaintextTokenGuard(unittest.TestCase):
    """Test that req() refuses to send Bearer token over plaintext HTTP to non-loopback."""

    def setUp(self):
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")

    def tearDown(self):
        if self.original_url is None:
            os.environ.pop("CONTROL_URL", None)
        else:
            os.environ["CONTROL_URL"] = self.original_url
        if self.original_token is None:
            os.environ.pop("CONTROL_TOKEN", None)
        else:
            os.environ["CONTROL_TOKEN"] = self.original_token
        importlib.reload(control_client)

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_refuses_token_over_plaintext_non_loopback(self, mock_urlopen):
        """Test req() raises RuntimeError when TOKEN set and URL is http:// non-loopback."""
        os.environ["CONTROL_URL"] = "http://192.168.1.10:8081"
        os.environ["CONTROL_TOKEN"] = "secret-token"
        importlib.reload(control_client)

        with self.assertRaises(RuntimeError) as cm:
            control_client.req("GET", "/bots")

        self.assertIn("Refusing to send Bearer token over plaintext", str(cm.exception))
        self.assertIn("192.168.1.10", str(cm.exception))
        self.assertIn("https://", str(cm.exception))
        # Token must NOT appear in error message
        self.assertNotIn("secret-token", str(cm.exception))
        # urlopen must never have been called
        mock_urlopen.assert_not_called()

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_allows_token_over_https_remote(self, mock_urlopen):
        """Test req() allows token over https:// even to non-loopback host."""
        os.environ["CONTROL_URL"] = "https://example.com"
        os.environ["CONTROL_TOKEN"] = "secret-token"
        importlib.reload(control_client)

        mock_response = MagicMock()
        mock_response.read.return_value = b'{}'
        mock_response.status = 200
        mock_response.__enter__ = lambda self: self
        mock_response.__exit__ = lambda self, *args: None
        mock_urlopen.return_value = mock_response

        status, body = control_client.req("GET", "/bots")

        mock_urlopen.assert_called_once()
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        headers_str = str(request_obj.headers)
        self.assertIn("Bearer", headers_str)
        self.assertEqual(status, 200)

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_allows_token_on_loopback_variants(self, mock_urlopen):
        """Test req() allows token over http:// to loopback addresses."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{}'
        mock_response.status = 200
        mock_response.__enter__ = lambda self: self
        mock_response.__exit__ = lambda self, *args: None
        mock_urlopen.return_value = mock_response

        for loopback_url in ["http://127.0.0.1:8081", "http://localhost:8081", "http://[::1]:8081"]:
            mock_urlopen.reset_mock()
            os.environ["CONTROL_URL"] = loopback_url
            os.environ["CONTROL_TOKEN"] = "secret-token"
            importlib.reload(control_client)

            status, body = control_client.req("GET", "/health")

            mock_urlopen.assert_called_once()
            call_args = mock_urlopen.call_args
            request_obj = call_args[0][0]
            headers_str = str(request_obj.headers)
            self.assertIn("Bearer", headers_str, f"Token not sent for loopback URL: {loopback_url}")

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_allows_no_token_over_plaintext_non_loopback(self, mock_urlopen):
        """Test req() allows http:// non-loopback when no token is set (nothing to leak)."""
        os.environ["CONTROL_URL"] = "http://192.168.1.10:8081"
        os.environ["CONTROL_TOKEN"] = ""
        importlib.reload(control_client)

        mock_response = MagicMock()
        mock_response.read.return_value = b'{}'
        mock_response.status = 200
        mock_response.__enter__ = lambda self: self
        mock_response.__exit__ = lambda self, *args: None
        mock_urlopen.return_value = mock_response

        status, body = control_client.req("GET", "/health")

        mock_urlopen.assert_called_once()
        self.assertEqual(status, 200)


class TestLoadingFeedbackAndTimeout(unittest.TestCase):
    """Test loading feedback timer and timeout error messages."""

    def setUp(self):
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"
        importlib.reload(control_client)

    def tearDown(self):
        if self.original_url is None:
            os.environ.pop("CONTROL_URL", None)
        else:
            os.environ["CONTROL_URL"] = self.original_url
        if self.original_token is None:
            os.environ.pop("CONTROL_TOKEN", None)
        else:
            os.environ["CONTROL_TOKEN"] = self.original_token
        importlib.reload(control_client)

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_timeout_error_includes_retry_guidance(self, mock_urlopen):
        """Test that timeout errors include actionable retry guidance."""
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("timed out")

        status, body = control_client.req("GET", "/bots")

        self.assertEqual(status, 0)
        self.assertIsInstance(body, dict)
        err_msg = body.get("error", "")
        self.assertIn("timed out", err_msg.lower())
        self.assertIn("--timeout", err_msg)
        self.assertIn("CONTROL_URL", err_msg)
        self.assertIn("network", err_msg.lower())

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_loading_feedback_shown_on_slow_request(self, mock_urlopen):
        """Test that loading message appears on stderr when request takes >500ms."""
        import time

        def slow_response(*args, **kwargs):
            time.sleep(0.7)
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{}'
            mock_resp.status = 200
            mock_resp.__enter__ = lambda self: self
            mock_resp.__exit__ = lambda self, *a: None
            return mock_resp

        mock_urlopen.side_effect = slow_response

        held_stderr = StringIO()
        original_stderr = sys.stderr
        sys.stderr = held_stderr
        try:
            control_client.req("GET", "/health")
        finally:
            sys.stderr = original_stderr

        output = held_stderr.getvalue()
        self.assertIn("Request in progress", output)

    @patch('codebot.control_client.urllib.request.urlopen')
    def test_req_no_loading_feedback_on_fast_request(self, mock_urlopen):
        """Test that loading message does NOT appear when request completes quickly."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{}'
        mock_response.status = 200
        mock_response.__enter__ = lambda self: self
        mock_response.__exit__ = lambda self, *a: None
        mock_urlopen.return_value = mock_response

        held_stderr = StringIO()
        original_stderr = sys.stderr
        sys.stderr = held_stderr
        try:
            control_client.req("GET", "/health")
        finally:
            sys.stderr = original_stderr

        output = held_stderr.getvalue()
        self.assertNotIn("Request in progress", output)


class TestTimeoutFlag(unittest.TestCase):
    """Test --timeout CLI flag parsing."""

    def setUp(self):
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        self.original_argv = sys.argv
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"
        importlib.reload(control_client)

    def tearDown(self):
        sys.argv = self.original_argv
        if self.original_url is None:
            os.environ.pop("CONTROL_URL", None)
        else:
            os.environ["CONTROL_URL"] = self.original_url
        if self.original_token is None:
            os.environ.pop("CONTROL_TOKEN", None)
        else:
            os.environ["CONTROL_TOKEN"] = self.original_token
        importlib.reload(control_client)

    @patch('codebot.control_client.cmd_status')
    def test_timeout_flag_overrides_default(self, mock_cmd_status):
        """Test that --timeout flag changes REQUEST_TIMEOUT."""
        sys.argv = ['control_client.py', '--timeout', '30', 'status']
        control_client.main()
        self.assertEqual(control_client.REQUEST_TIMEOUT, 30)
        mock_cmd_status.assert_called_once()

    def test_timeout_flag_invalid_value_exits(self):
        """Test that --timeout with non-integer exits with code 1."""
        sys.argv = ['control_client.py', '--timeout', 'abc', 'status']
        with self.assertRaises(SystemExit) as cm:
            control_client.main()
        self.assertEqual(cm.exception.code, 1)


class TestPrintResult(unittest.TestCase):
    """Test _print_result helper function."""

    def setUp(self):
        self.held_output = StringIO()
        self.original_stdout = sys.stdout
        sys.stdout = self.held_output

    def tearDown(self):
        sys.stdout = self.original_stdout

    def test_print_result_dict_with_undo(self):
        """Test _print_result displays undo info when present."""
        response = {"status": "ok", "undo": "Run clear-drain to resume"}
        control_client._print_result(response)
        output = self.held_output.getvalue()
        self.assertIn("status", output)
        self.assertIn("Recovery", output)
        self.assertIn("clear-drain", output)

    def test_print_result_dict_with_dry_run_preview(self):
        """Test _print_result displays preview and undo for dry-run."""
        response = {
            "dry_run": True,
            "preview": {
                "description": "Would restart bug_hunter",
                "undo": "No action taken"
            }
        }
        control_client._print_result(response)
        output = self.held_output.getvalue()
        self.assertIn("Preview", output)
        self.assertIn("Would restart", output)
        self.assertIn("Recovery", output)

    def test_print_result_string(self):
        """Test _print_result handles string responses."""
        control_client._print_result("Plain text response")
        output = self.held_output.getvalue()
        self.assertEqual(output.strip(), "Plain text response")

    def test_print_result_dict_no_extra_info(self):
        """Test _print_result handles dict without undo or preview."""
        response = {"status": "ok"}
        control_client._print_result(response)
        output = self.held_output.getvalue()
        self.assertIn("status", output)
        self.assertNotIn("Recovery", output)
        self.assertNotIn("Preview", output)


class TestIsLoopbackHost(unittest.TestCase):
    """Test _is_loopback_host helper function."""

    def test_loopback_localhost(self):
        self.assertTrue(control_client._is_loopback_host("localhost"))

    def test_loopback_ipv4(self):
        self.assertTrue(control_client._is_loopback_host("127.0.0.1"))

    def test_loopback_ipv6(self):
        self.assertTrue(control_client._is_loopback_host("[::1]"))
        self.assertTrue(control_client._is_loopback_host("::1"))

    def test_non_loopback(self):
        self.assertFalse(control_client._is_loopback_host("192.168.1.1"))
        self.assertFalse(control_client._is_loopback_host("example.com"))

    def test_none_hostname(self):
        self.assertFalse(control_client._is_loopback_host(None))

    def test_case_insensitive(self):
        self.assertTrue(control_client._is_loopback_host("LOCALHOST"))
        self.assertTrue(control_client._is_loopback_host("LocalHost"))


class TestCmdStatusCoverage(unittest.TestCase):
    """Additional tests for cmd_status to improve coverage."""

    def setUp(self):
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"
        importlib.reload(control_client)
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
        importlib.reload(control_client)

    @patch('codebot.control_client.req')
    def test_cmd_status_with_missing_optional_fields(self, mock_req):
        """Test cmd_status handles bots with missing optional fields."""
        mock_req.return_value = (200, [
            {"name": "minimal_bot"}  # Missing running, heartbeat_age_seconds, etc.
        ])

        control_client.cmd_status()
        output = self.held_output.getvalue()
        self.assertIn("minimal_bot", output)


class TestCmdSchedulerStatusCoverage(unittest.TestCase):
    """Additional tests for cmd_scheduler_status to improve coverage."""

    def setUp(self):
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"
        importlib.reload(control_client)
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
        importlib.reload(control_client)

    @patch('codebot.control_client.req')
    def test_cmd_scheduler_status_error_exit(self, mock_req):
        """Test cmd_scheduler_status exits on non-200 response."""
        mock_req.return_value = (500, {"error": "Server error"})

        with self.assertRaises(SystemExit) as cm:
            control_client.cmd_scheduler_status()

        self.assertEqual(cm.exception.code, 1)
        output = self.held_output.getvalue()
        self.assertIn("error", output.lower())

    @patch('codebot.control_client.req')
    def test_cmd_scheduler_status_with_missing_fields(self, mock_req):
        """Test cmd_scheduler_status handles payload with missing optional fields."""
        mock_req.return_value = (200, {"version": "1.0"})  # Minimal payload

        control_client.cmd_scheduler_status()
        output = self.held_output.getvalue()
        result = json.loads(output)

        self.assertEqual(result["version"], "1.0")
        # All bounded fields should exist even if source was missing
        self.assertIn("budget_state", result)
        self.assertIn("dead_letter_ids", result)
        self.assertEqual(result["dead_letter_ids"], [])

    @patch('codebot.control_client.req')
    def test_cmd_scheduler_status_disabled_details_not_list(self, mock_req):
        """Test cmd_scheduler_status handles disabled_details that is not a list."""
        mock_req.return_value = (200, {"disabled_details": "not-a-list"})

        control_client.cmd_scheduler_status()
        output = self.held_output.getvalue()
        result = json.loads(output)

        self.assertEqual(result["disabled_details"], [])


class TestMainCLICoverage(unittest.TestCase):
    """Additional tests for main() CLI to improve coverage."""

    def setUp(self):
        self.original_url = os.environ.get("CONTROL_URL")
        self.original_token = os.environ.get("CONTROL_TOKEN")
        self.original_argv = sys.argv
        os.environ["CONTROL_URL"] = "http://127.0.0.1:8081"
        os.environ["CONTROL_TOKEN"] = "test-token"
        importlib.reload(control_client)
        self.held_output = StringIO()
        self.original_stdout = sys.stdout
        sys.stdout = self.held_output

    def tearDown(self):
        sys.argv = self.original_argv
        sys.stdout = self.original_stdout
        if self.original_url is None:
            os.environ.pop("CONTROL_URL", None)
        else:
            os.environ["CONTROL_URL"] = self.original_url
        if self.original_token is None:
            os.environ.pop("CONTROL_TOKEN", None)
        else:
            os.environ["CONTROL_TOKEN"] = self.original_token
        importlib.reload(control_client)

    @patch('codebot.control_client.req')
    def test_main_health_command(self, mock_req):
        """Test main() routes 'health' command."""
        mock_req.return_value = (200, {"status": "healthy"})
        sys.argv = ['control_client.py', 'health']

        control_client.main()

        mock_req.assert_called_once_with("GET", "/health")

    @patch('codebot.control_client.req')
    def test_main_logs_command(self, mock_req):
        """Test main() routes 'logs' command with --lines option."""
        mock_req.return_value = (200, {"tail": ["log line 1", "log line 2"]})
        sys.argv = ['control_client.py', 'logs', 'bug_hunter', '--lines', '50']

        control_client.main()

        mock_req.assert_called_once_with("GET", "/bots/bug_hunter/logs?lines=50")

    @patch('codebot.control_client.req')
    def test_main_logs_command_default_lines(self, mock_req):
        """Test main() routes 'logs' command with default lines."""
        mock_req.return_value = (200, {"tail": ["log line 1"]})
        sys.argv = ['control_client.py', 'logs', 'bug_hunter']

        control_client.main()

        mock_req.assert_called_once_with("GET", "/bots/bug_hunter/logs?lines=200")

    @patch('codebot.control_client.req')
    def test_main_resume_command(self, mock_req):
        """Test main() routes 'resume' command."""
        mock_req.return_value = (200, {"status": "resumed"})
        sys.argv = ['control_client.py', 'resume', 'bug_hunter']

        control_client.main()

        mock_req.assert_called_once_with("POST", "/bots/bug_hunter/resume", {})

    @patch('codebot.control_client.req')
    def test_main_clear_drain_command(self, mock_req):
        """Test main() routes 'clear-drain' command."""
        mock_req.return_value = (200, {"status": "drain cleared"})
        sys.argv = ['control_client.py', 'clear-drain']

        control_client.main()

        mock_req.assert_called_once_with("POST", "/control/clear-drain", {})

    @patch('codebot.control_client.req')
    def test_main_undrain_command(self, mock_req):
        """Test main() routes 'undrain' command (alias)."""
        mock_req.return_value = (200, {"status": "drain cleared"})
        sys.argv = ['control_client.py', 'undrain']

        control_client.main()

        mock_req.assert_called_once_with("POST", "/control/clear-drain", {})

    @patch('codebot.control_client.req')
    def test_main_state_command(self, mock_req):
        """Test main() routes 'state' command."""
        mock_req.return_value = (200, {"state": "active"})
        sys.argv = ['control_client.py', 'state']

        control_client.main()

        mock_req.assert_called_once_with("GET", "/state")

    @patch('codebot.control_client.input', return_value='yes')
    @patch('codebot.control_client.req')
    def test_main_restart_command_interactive(self, mock_req, mock_input):
        """Test main() routes 'restart' command with interactive confirmation."""
        mock_req.return_value = (200, {"status": "restarting"})
        sys.argv = ['control_client.py', 'restart', 'bug_hunter']

        control_client.main()

        mock_input.assert_called_once()
        mock_req.assert_called_once_with("POST", "/bots/bug_hunter/restart", {"force": True})

    @patch('codebot.control_client.req')
    def test_main_restart_command_force(self, mock_req):
        """Test main() routes 'restart' command with --force flag."""
        mock_req.return_value = (200, {"status": "restarting"})
        sys.argv = ['control_client.py', 'restart', 'bug_hunter', '--force']

        control_client.main()

        mock_req.assert_called_once_with("POST", "/bots/bug_hunter/restart", {"force": True})

    @patch('codebot.control_client.req')
    def test_main_restart_command_dry_run(self, mock_req):
        """Test main() routes 'restart' command with --dry-run flag."""
        mock_req.return_value = (200, {"dry_run": True, "preview": {}})
        sys.argv = ['control_client.py', 'restart', 'bug_hunter', '--dry-run']

        control_client.main()

        mock_req.assert_called_once_with("POST", "/bots/bug_hunter/restart", {"dry_run": True})

    @patch('codebot.control_client.validate_bot_name_simple', return_value=False)
    def test_main_restart_invalid_bot_name(self, mock_validate):
        """Test main() rejects invalid bot name for restart."""
        sys.argv = ['control_client.py', 'restart', 'invalid!name']

        with self.assertRaises(SystemExit) as cm:
            control_client.main()

        self.assertEqual(cm.exception.code, 1)

    @patch('codebot.control_client.input', return_value='yes')
    @patch('codebot.control_client.req')
    def test_main_pause_command_interactive(self, mock_req, mock_input):
        """Test main() routes 'pause' command with interactive confirmation."""
        mock_req.return_value = (200, {"status": "paused"})
        sys.argv = ['control_client.py', 'pause', 'bug_hunter']

        control_client.main()

        mock_input.assert_called_once()
        mock_req.assert_called_once_with("POST", "/bots/bug_hunter/pause", {"force": True})

    @patch('codebot.control_client.req')
    def test_main_pause_command_force(self, mock_req):
        """Test main() routes 'pause' command with --force flag."""
        mock_req.return_value = (200, {"status": "paused"})
        sys.argv = ['control_client.py', 'pause', 'bug_hunter', '--force']

        control_client.main()

        mock_req.assert_called_once_with("POST", "/bots/bug_hunter/pause", {"force": True})

    @patch('codebot.control_client.input', return_value='yes')
    @patch('codebot.control_client.req')
    def test_main_stop_command_with_bots_interactive(self, mock_req, mock_input):
        """Test main() routes 'stop' command with specific bots."""
        mock_req.return_value = (200, {"status": "stopped"})
        sys.argv = ['control_client.py', 'stop', 'bot1', 'bot2']

        control_client.main()

        mock_input.assert_called_once()
        call_args = mock_req.call_args
        self.assertEqual(call_args[0][1], "/bots/stop")
        self.assertIn("bots", call_args[0][2])

    @patch('codebot.control_client.req')
    def test_main_stop_command_force(self, mock_req):
        """Test main() routes 'stop' command with --force flag."""
        mock_req.return_value = (200, {"status": "stopped"})
        sys.argv = ['control_client.py', 'stop', '--force']

        control_client.main()

        mock_req.assert_called_once_with("POST", "/bots/stop", {"force": True})

    @patch('codebot.control_client.req')
    def test_main_stop_command_dry_run(self, mock_req):
        """Test main() routes 'stop' command with --dry-run flag."""
        mock_req.return_value = (200, {"dry_run": True})
        sys.argv = ['control_client.py', 'stop', '--dry-run']

        control_client.main()

        mock_req.assert_called_once_with("POST", "/bots/stop", {"dry_run": True})

    @patch('codebot.control_client.input', return_value='no')
    def test_main_stop_command_aborted(self, mock_input):
        """Test main() aborts stop command on negative confirmation."""
        sys.argv = ['control_client.py', 'stop', 'bot1']

        with self.assertRaises(SystemExit) as cm:
            control_client.main()

        self.assertEqual(cm.exception.code, 0)

    @patch('codebot.control_client.input', return_value='yes')
    @patch('codebot.control_client.req')
    def test_main_drain_command_interactive(self, mock_req, mock_input):
        """Test main() routes 'drain' command with interactive confirmation."""
        mock_req.return_value = (200, {"status": "draining"})
        sys.argv = ['control_client.py', 'drain']

        control_client.main()

        mock_input.assert_called_once()
        mock_req.assert_called_once_with("POST", "/control/drain", {"force": True})

    @patch('codebot.control_client.req')
    def test_main_drain_command_force(self, mock_req):
        """Test main() routes 'drain' command with --force flag."""
        mock_req.return_value = (200, {"status": "draining"})
        sys.argv = ['control_client.py', 'drain', '--force']

        control_client.main()

        mock_req.assert_called_once_with("POST", "/control/drain", {"force": True})

    @patch('codebot.control_client.req')
    def test_main_safe_stop_command(self, mock_req):
        """Test main() routes 'safe-stop' command (alias for drain)."""
        mock_req.return_value = (200, {"status": "draining"})
        sys.argv = ['control_client.py', 'safe-stop', '--force']

        control_client.main()

        mock_req.assert_called_once_with("POST", "/control/drain", {"force": True})

    @patch('codebot.control_client.input', return_value='yes')
    @patch('codebot.control_client.req')
    def test_main_update_command_interactive(self, mock_req, mock_input):
        """Test main() routes 'update' command with interactive confirmation."""
        mock_req.return_value = (200, {"status": "updating"})
        sys.argv = ['control_client.py', 'update']

        control_client.main()

        mock_input.assert_called_once()
        mock_req.assert_called_once_with("POST", "/control/update", {"force": True})

    @patch('codebot.control_client.req')
    def test_main_update_command_force(self, mock_req):
        """Test main() routes 'update' command with --force flag."""
        mock_req.return_value = (200, {"status": "updating"})
        sys.argv = ['control_client.py', 'update', '--force']

        control_client.main()

        mock_req.assert_called_once_with("POST", "/control/update", {"force": True})

    @patch('codebot.control_client.req')
    def test_main_update_command_dry_run(self, mock_req):
        """Test main() routes 'update' command with --dry-run flag."""
        mock_req.return_value = (200, {"dry_run": True})
        sys.argv = ['control_client.py', 'update', '--dry-run']

        control_client.main()

        mock_req.assert_called_once_with("POST", "/control/update", {"dry_run": True})

    @patch('codebot.control_client.cmd_scheduler_events')
    def test_main_scheduler_events_command(self, mock_cmd_events):
        """Test main() routes 'scheduler-events' command."""
        sys.argv = ['control_client.py', 'scheduler-events', '--limit', '50']

        control_client.main()

        mock_cmd_events.assert_called_once_with("50", None)

    @patch('codebot.control_client.cmd_dead_letters')
    def test_main_dead_letters_command(self, mock_cmd_dl):
        """Test main() routes 'dead-letters' command."""
        sys.argv = ['control_client.py', 'dead-letters']

        control_client.main()

        mock_cmd_dl.assert_called_once()

    def test_main_scheduler_events_invalid_limit(self):
        """Test main() handles invalid --limit for scheduler-events."""
        sys.argv = ['control_client.py', 'scheduler-events', '--limit', 'abc']

        with self.assertRaises(SystemExit) as cm:
            control_client.main()

        self.assertEqual(cm.exception.code, 1)

    def test_main_scheduler_events_unknown_option(self):
        """Test main() handles unknown option for scheduler-events."""
        sys.argv = ['control_client.py', 'scheduler-events', '--unknown']

        with self.assertRaises(SystemExit) as cm:
            control_client.main()

        self.assertEqual(cm.exception.code, 1)

    def test_main_scheduler_events_unknown_argument(self):
        """Test main() handles unknown argument for scheduler-events."""
        sys.argv = ['control_client.py', 'scheduler-events', 'extra-arg']

        with self.assertRaises(SystemExit) as cm:
            control_client.main()

        self.assertEqual(cm.exception.code, 1)

    def test_main_logs_invalid_lines(self):
        """Test main() handles invalid --lines value."""
        sys.argv = ['control_client.py', 'logs', 'bug_hunter', '--lines', 'abc']

        with self.assertRaises(SystemExit) as cm:
            control_client.main()

        self.assertEqual(cm.exception.code, 1)

    def test_main_logs_missing_lines_value(self):
        """Test main() handles missing --lines value."""
        sys.argv = ['control_client.py', 'logs', 'bug_hunter', '--lines']

        with self.assertRaises(SystemExit) as cm:
            control_client.main()

        self.assertEqual(cm.exception.code, 1)

    def test_main_retry_dead_letter_missing_id(self):
        """Test main() requires item ID for retry-dead-letter."""
        sys.argv = ['control_client.py', 'retry-dead-letter']

        with self.assertRaises(SystemExit) as cm:
            control_client.main()

        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
