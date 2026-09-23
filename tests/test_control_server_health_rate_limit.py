"""Tests for /health endpoint rate limiter isolation (CB-351DD, CB-E2A92).

Verifies that:
1. /health uses a separate rate limiter from auth endpoints.
2. /health returns 200 even when auth rate limiter is blocked.
3. /health returns 429 when its own rate limiter is triggered by excessive requests.
4. The test patches cs._health_rate_limiter (not cs._rate_limiter).
"""
import io
import json
import time
import unittest
from unittest.mock import patch, MagicMock

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import codebot.control_server as cs
from codebot.control_server import RateLimiter, RATE_LIMIT_MAX_ATTEMPTS, RATE_LIMIT_COOLDOWN_SECONDS


class _FakeHeaders(dict):
    def get(self, k, default=None):
        return super().get(k, default)


def _make_handler(path: str, client_ip: str = "127.0.0.1"):
    """Create a minimal mock handler for testing do_GET."""
    h = MagicMock(spec=cs.ControlHandler)
    h.path = path
    h.command = "GET"
    h.client_address = (client_ip, 12345)
    h.headers = _FakeHeaders({"Content-Length": "0"})
    h.rfile = io.BytesIO(b"")
    h.wfile = io.BytesIO()
    h.connection = MagicMock()
    h.connection.settimeout = MagicMock()
    # Bind real methods
    for name in ["_json", "do_GET", "_is_loopback_client", "log_message"]:
        if hasattr(cs.ControlHandler, name):
            setattr(h, name, getattr(cs.ControlHandler, name).__get__(h, cs.ControlHandler))
    return h


def _capture_json(handler):
    """Replace _json with a capturing version."""
    calls = []
    def _cap(code, obj, extra_headers=None):
        calls.append((code, obj, extra_headers))
        body = json.dumps(obj).encode()
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                handler.send_header(k, v)
        handler.end_headers()
        handler.wfile.write(body)
    handler._json = _cap
    return calls


class TestHealthRateLimiterIsolation(unittest.TestCase):
    """Verify that /health uses a separate rate limiter from auth endpoints."""

    def setUp(self):
        """Reset both rate limiters before each test."""
        with cs._rate_limiter._lock:
            cs._rate_limiter._failures.clear()
            cs._rate_limiter._blocked_until.clear()
        with cs._health_rate_limiter._lock:
            cs._health_rate_limiter._failures.clear()
            cs._health_rate_limiter._blocked_until.clear()

    def test_health_returns_200_after_max_auth_failures_same_ip(self):
        """
        Acceptance Criteria: /health returns 200 even after
        RATE_LIMIT_MAX_ATTEMPTS failed auth requests from same IP.
        """
        client_ip = "192.168.1.100"

        # Simulate RATE_LIMIT_MAX_ATTEMPTS failed auth attempts
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
            cs._rate_limiter.record_failure(client_ip)

        # Verify auth limiter is now blocked
        allowed, reason = cs._rate_limiter.is_allowed(client_ip)
        self.assertFalse(allowed, "Auth limiter should be blocked after max failures")
        self.assertIn("rate limit exceeded", reason.lower())

        # Now hit /health — should still return 200 because health limiter is separate
        h = _make_handler("/health", client_ip)
        calls = _capture_json(h)
        h.do_GET()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 200, "/health must return 200 despite auth rate limit")
        self.assertEqual(calls[0][1]["status"], "ok")

    def test_health_returns_429_after_excessive_health_requests(self):
        """
        Acceptance Criteria: /health returns 429 after excessive /health requests
        from the same IP (proving _health_rate_limiter actually works).
        """
        client_ip = "10.0.0.50"

        # Make RATE_LIMIT_MAX_ATTEMPTS successful /health requests to trigger blocking
        for i in range(RATE_LIMIT_MAX_ATTEMPTS):
            h = _make_handler("/health", client_ip)
            calls = _capture_json(h)
            h.do_GET()
            self.assertEqual(calls[0][0], 200,
                             f"Request {i+1} should succeed before limit reached")

        # Next request should be blocked
        h = _make_handler("/health", client_ip)
        calls = _capture_json(h)
        h.do_GET()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 429,
                         "/health must return 429 after exceeding rate limit")
        self.assertIn("too many requests", calls[0][1].get("error", "").lower())

    def test_health_limiter_patches_correct_object(self):
        """
        Acceptance Criteria: test_do_get_health_rate_limited patches
        cs._health_rate_limiter, NOT cs._rate_limiter.

        This test verifies that patching _health_rate_limiter.is_allowed
        controls the /health response, proving the handler uses the correct limiter.
        """
        client_ip = "172.16.0.1"

        # Patch _health_rate_limiter to deny
        with patch.object(cs._health_rate_limiter, "is_allowed", return_value=(False, "blocked for test")):
            h = _make_handler("/health", client_ip)
            calls = _capture_json(h)
            h.do_GET()

        self.assertEqual(calls[0][0], 429,
                         "Patching _health_rate_limiter must cause 429 on /health")

        # Verify that patching _rate_limiter does NOT affect /health
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(False, "auth blocked")):
            h = _make_handler("/health", client_ip)
            calls = _capture_json(h)
            h.do_GET()

        self.assertEqual(calls[0][0], 200,
                         "Patching _rate_limiter must NOT affect /health response")

    def test_integration_health_200_after_auth_bruteforce_then_429_after_health_flood(self):
        """
        Integration test: /health returns 200 after auth brute-force but
        returns 429 after excessive /health requests from same IP.
        """
        client_ip = "203.0.113.42"

        # Step 1: Brute-force auth to block _rate_limiter
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
            cs._rate_limiter.record_failure(client_ip)

        auth_allowed, _ = cs._rate_limiter.is_allowed(client_ip)
        self.assertFalse(auth_allowed, "Auth limiter must be blocked")

        # Step 2: /health still works (isolated limiter)
        h = _make_handler("/health", client_ip)
        calls = _capture_json(h)
        h.do_GET()
        self.assertEqual(calls[0][0], 200, "/health must work despite auth block")

        # Step 3: Flood /health to trigger its own limiter
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS - 1):  # Already made 1 above
            h = _make_handler("/health", client_ip)
            calls = _capture_json(h)
            h.do_GET()
            self.assertEqual(calls[0][0], 200)

        # Step 4: Next /health request must be 429
        h = _make_handler("/health", client_ip)
        calls = _capture_json(h)
        h.do_GET()
        self.assertEqual(calls[0][0], 429,
                         "/health must return 429 after health-specific flood")


if __name__ == '__main__':
    unittest.main()
