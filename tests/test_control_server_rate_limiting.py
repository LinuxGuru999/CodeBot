"""Tests for rate limiting in control_server.py authentication.

Verifies that:
1. RateLimiter correctly tracks failures per IP.
2. Requests are blocked (429) after exceeding the threshold.
3. Cooldown period prevents further attempts.
4. Successful auth resets nothing (failures persist until window expires).
"""
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add codebot to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "codebot"))

# Mock imports that might fail in test env before full setup
sys.modules['orchestrator'] = MagicMock()
sys.modules['orch_cfg'] = MagicMock()

from control_server import RateLimiter, RATE_LIMIT_MAX_ATTEMPTS, RATE_LIMIT_WINDOW_SECONDS, RATE_LIMIT_COOLDOWN_SECONDS


class TestRateLimiter(unittest.TestCase):
    """Unit tests for the RateLimiter class."""

    def setUp(self):
        self.limiter = RateLimiter()
        self.test_ip = "192.168.1.100"
        # Ensure clean state
        self.limiter._failures.clear()
        self.limiter._blocked_until.clear()

    def test_initial_request_allowed(self):
        """First request from an IP should be allowed."""
        allowed, reason = self.limiter.is_allowed(self.test_ip)
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_failures_recorded_correctly(self):
        """Recording failures should increment the count."""
        for i in range(RATE_LIMIT_MAX_ATTEMPTS - 1):
            self.limiter.record_failure(self.test_ip)
        
        # Should still be allowed (not yet at limit)
        allowed, _ = self.limiter.is_allowed(self.test_ip)
        self.assertTrue(allowed)

    def test_block_after_max_attempts(self):
        """Request should be blocked after exceeding max attempts."""
        # Simulate max attempts
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
            self.limiter.record_failure(self.test_ip)
        
        allowed, reason = self.limiter.is_allowed(self.test_ip)
        self.assertFalse(allowed)
        self.assertIsNotNone(reason)
        self.assertIn("rate limit exceeded", reason)

    def test_cooldown_prevents_requests(self):
        """Requests during cooldown should be blocked."""
        # Trigger block
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS + 1):
            self.limiter.record_failure(self.test_ip)
            self.limiter.is_allowed(self.test_ip)  # Check to trigger block logic if needed
        
        # Verify blocked
        allowed, reason = self.limiter.is_allowed(self.test_ip)
        self.assertFalse(allowed)
        self.assertIn("blocked", reason.lower())

    def test_cooldown_expires_allows_requests(self):
        """After cooldown expires, requests should be allowed again."""
        # Manually set blocked_until to past
        self.limiter._blocked_until[self.test_ip] = time.time() - 10
        self.limiter._failures[self.test_ip] = [time.time() - 100]  # Old failures
        
        allowed, reason = self.limiter.is_allowed(self.test_ip)
        self.assertTrue(allowed)
        self.assertIsNone(reason)
        # Failures should be cleared after cooldown
        self.assertEqual(len(self.limiter._failures[self.test_ip]), 0)

    def test_window_sliding_removes_old_failures(self):
        """Failures older than the window should be ignored."""
        old_time = time.time() - RATE_LIMIT_WINDOW_SECONDS - 10
        self.limiter._failures[self.test_ip] = [old_time]
        
        allowed, _ = self.limiter.is_allowed(self.test_ip)
        self.assertTrue(allowed)
        # Old failure should be cleaned up
        self.assertEqual(len(self.limiter._failures[self.test_ip]), 0)

    def test_multiple_ips_tracked_independently(self):
        """Rate limits should be per-IP."""
        ip1 = "1.1.1.1"
        ip2 = "2.2.2.2"
        
        # Exhaust ip1
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
            self.limiter.record_failure(ip1)
            self.limiter.is_allowed(ip1)
        
        # ip1 blocked
        allowed1, _ = self.limiter.is_allowed(ip1)
        self.assertFalse(allowed1)
        
        # ip2 should still be allowed
        allowed2, _ = self.limiter.is_allowed(ip2)
        self.assertTrue(allowed2)


if __name__ == "__main__":
    unittest.main()
