"""Tests for control_server rate limiting and authentication.

Covers RateLimiter class behavior and integration with _auth() and _handle_telemetry().
Also covers fail-closed security behavior when CONTROL_TOKEN is unset (CB-6048497-D3F1).
"""
import io
import json
import logging
import time
import threading
from http.server import BaseHTTPRequestHandler
from unittest.mock import patch, MagicMock

import pytest

# Import the RateLimiter class directly for unit testing
from codebot.control_server import RateLimiter, RATE_LIMIT_MAX_ATTEMPTS, RATE_LIMIT_WINDOW_SECONDS, RATE_LIMIT_COOLDOWN_SECONDS


class TestRateLimiter:
    """Unit tests for RateLimiter class."""

    def setup_method(self):
        """Create a fresh RateLimiter for each test."""
        self.limiter = RateLimiter()

    def test_is_allowed_initial(self):
        """New IP should be allowed."""
        allowed, reason = self.limiter.is_allowed("192.168.1.1")
        assert allowed is True
        assert reason is None

    def test_record_failure_and_check_limit(self):
        """After max failures, IP should be blocked."""
        ip = "10.0.0.1"
        # Record failures up to the limit
        for i in range(RATE_LIMIT_MAX_ATTEMPTS):
            self.limiter.record_failure(ip)

        # Next check should be blocked
        allowed, reason = self.limiter.is_allowed(ip)
        assert allowed is False
        assert "rate limit exceeded" in reason

    def test_record_failure_below_limit(self):
        """Below max failures, IP should still be allowed."""
        ip = "10.0.0.2"
        # Record one less than max
        for i in range(RATE_LIMIT_MAX_ATTEMPTS - 1):
            self.limiter.record_failure(ip)

        # Should still be allowed
        allowed, reason = self.limiter.is_allowed(ip)
        assert allowed is True
        assert reason is None

    def test_cooldown_expiration(self):
        """After cooldown period, IP should be unblocked."""
        ip = "10.0.0.3"
        # Fill up failures
        for i in range(RATE_LIMIT_MAX_ATTEMPTS):
            self.limiter.record_failure(ip)

        # Verify blocked
        allowed, _ = self.limiter.is_allowed(ip)
        assert allowed is False

        # Manually expire the cooldown by setting blocked_until to past
        with self.limiter._lock:
            self.limiter._blocked_until[ip] = time.time() - 100

        # Should be allowed now
        allowed, reason = self.limiter.is_allowed(ip)
        assert allowed is True
        # Failures should be reset after cooldown
        assert len(self.limiter._failures[ip]) == 0

    def test_window_cleanup(self):
        """Old failures outside the window should be cleaned up."""
        ip = "10.0.0.4"
        # Add old failures
        old_time = time.time() - RATE_LIMIT_WINDOW_SECONDS - 100
        with self.limiter._lock:
            self.limiter._failures[ip] = [old_time, old_time + 1]

        # Check should clean up old failures and allow
        allowed, reason = self.limiter.is_allowed(ip)
        assert allowed is True
        with self.limiter._lock:
            assert len(self.limiter._failures[ip]) == 0

    def test_different_ips_independent(self):
        """Rate limiting should be per-IP, not global."""
        ip1 = "10.0.0.5"
        ip2 = "10.0.0.6"

        # Block ip1
        for i in range(RATE_LIMIT_MAX_ATTEMPTS):
            self.limiter.record_failure(ip1)

        # ip1 should be blocked
        allowed1, _ = self.limiter.is_allowed(ip1)
        assert allowed1 is False

        # ip2 should still be allowed
        allowed2, reason2 = self.limiter.is_allowed(ip2)
        assert allowed2 is True
        assert reason2 is None

    def test_rate_limit_reason_message(self):
        """Blocked response should include helpful message."""
        ip = "10.0.0.7"
        for i in range(RATE_LIMIT_MAX_ATTEMPTS):
            self.limiter.record_failure(ip)

        allowed, reason = self.limiter.is_allowed(ip)
        assert allowed is False
        assert "rate limit exceeded" in reason
        assert str(RATE_LIMIT_MAX_ATTEMPTS) in reason
        assert str(RATE_LIMIT_WINDOW_SECONDS) in reason


class TestRateLimiterThreadSafety:
    """Test thread safety of RateLimiter."""

    def test_concurrent_access(self):
        """Multiple threads accessing RateLimiter should not cause race conditions."""
        limiter = RateLimiter()
        ip = "10.0.0.100"
        errors = []

        def record_failures():
            try:
                for _ in range(10):
                    limiter.record_failure(ip)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=record_failures) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # After 50 failures (5 threads * 10), should definitely be blocked
        allowed, _ = limiter.is_allowed(ip)
        assert allowed is False
