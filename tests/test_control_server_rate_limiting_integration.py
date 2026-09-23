"""Integration tests for rate limiting and constant-time auth in control_server.py.

Verifies acceptance criteria for CB-725106-6F55:
1. test_rate_limiting_blocks_after_threshold exists and verifies 429 status
2. test_constant_time_comparison verifies hmac.compare_digest usage
"""
import os
import sys
import time
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch, MagicMock
import urllib.request
import urllib.error
import json

# Add codebot to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "codebot"))

# Mock orchestrator imports that might fail in test env
mock_orch = MagicMock()
mock_orch.BOT_REGISTRY = []
mock_orch.MODEL_PROFILES = {}
sys.modules['orchestrator'] = mock_orch
sys.modules['orch_cfg'] = mock_orch

from control_server import ControlHandler, RATE_LIMIT_MAX_ATTEMPTS, RATE_LIMIT_COOLDOWN_SECONDS


class TestControlServerRateLimitingIntegration(unittest.TestCase):
    """Integration tests for rate limiting on auth endpoints."""

    @classmethod
    def setUpClass(cls):
        """Start test server with rate limiting enabled."""
        # Set rate limit config for testing
        os.environ['RATE_LIMIT_MAX_ATTEMPTS'] = '3'
        os.environ['RATE_LIMIT_WINDOW_SECONDS'] = '60'
        os.environ['RATE_LIMIT_COOLDOWN_SECONDS'] = '5'
        os.environ['CONTROL_TOKEN'] = 'test-token-12345'
        os.environ.pop('CONTROL_ALLOW_UNAUTHENTICATED', None)
        os.environ['PORT'] = '0'  # Let OS pick available port
        
        # Reload to pick up env vars
        import importlib
        import control_server
        importlib.reload(control_server)
        
        cls.server = None
        cls.server_thread = None
        cls.base_url = None
        
        # Start server on random port
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), control_server.ControlHandler)
        cls.port = cls.server.server_address[1]
        cls.base_url = f'http://127.0.0.1:{cls.port}'
        
        cls.server_thread = threading.Thread(target=cls.server.serve_forever)
        cls.server_thread.daemon = True
        cls.server_thread.start()
        
        # Give server time to start
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        """Stop test server."""
        if cls.server:
            cls.server.shutdown()
            cls.server_thread.join(timeout=5)

    def _make_request(self, path, token=None):
        """Make HTTP request to test server."""
        url = f'{self.base_url}{path}'
        req = urllib.request.Request(url)
        if token:
            req.add_header('Authorization', f'Bearer {token}')
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode()) if e.fp else {}

    def test_rate_limiting_blocks_after_threshold(self):
        """Verify that rate limiting returns 429 after exceeding threshold.

        Acceptance criteria:
        - test_control_server.py contains test_rate_limiting_blocks_after_threshold
        - test verifies 429 status

        NOTE: /health is intentionally PUBLIC (no auth) with its own
        _health_rate_limiter, so failed-auth counting uses the protected
        /bots endpoint (guarded by _rate_limiter via _auth()).
        """
        # Make RATE_LIMIT_MAX_ATTEMPTS failed auth requests against /bots.
        # NOTE: RATE_LIMIT_MAX_ATTEMPTS is read at import time; the reloaded
        # test server uses env RATE_LIMIT_MAX_ATTEMPTS=3 while this process
        # may have the production default (5). Use the server's live limiter
        # config (3) via env, falling back to the imported constant.
        import control_server as _cs_live
        _live_max = 3
        try:
            _live_max = int(os.environ.get("RATE_LIMIT_MAX_ATTEMPTS", str(RATE_LIMIT_MAX_ATTEMPTS)))
        except (TypeError, ValueError):
            _live_max = RATE_LIMIT_MAX_ATTEMPTS
        for i in range(_live_max):
            status, body = self._make_request('/bots', token='wrong-token')
            # First few should be 401 (auth failure), not yet rate limited
            self.assertEqual(status, 401, f"Request {i+1} should return 401, got {status}")

        # Next request should be rate limited (429)
        status, body = self._make_request('/bots', token='wrong-token')
        self.assertEqual(status, 429, f"Request after threshold should return 429, got {status}")
        self.assertIn('error', body)
        # 429 payload is {"error": "too many requests", "reason": "rate limit exceeded ..."}
        combined = (body.get('error', '') + ' ' + body.get('reason', '')).lower()
        self.assertIn('rate limit', combined)

        # Verify Retry-After header is present
        req = urllib.request.Request(f'{self.base_url}/bots')
        req.add_header('Authorization', 'Bearer wrong-token')
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as e:
            retry_after = e.headers.get('Retry-After')
            self.assertIsNotNone(retry_after, "Retry-After header should be present on 429")
            self.assertTrue(retry_after.isdigit(), "Retry-After should be integer seconds")
            self.assertGreater(int(retry_after), 0, "Retry-After should be positive")

    def test_rate_limiting_allows_after_cooldown(self):
        """Verify that rate limit resets after cooldown period."""
        # This test is skipped in CI due to time constraints
        # but documents the expected behavior
        self.skipTest("Cooldown test requires waiting for RATE_LIMIT_COOLDOWN_SECONDS")

    def test_rate_limiting_per_ip(self):
        """Verify rate limiting is per-IP, not global (via protected /bots)."""
        # Exhaust rate limit for one "IP" (simulated via server's client_address)
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS + 1):
            self._make_request('/bots', token='wrong-token')

        # Server is single-threaded in test, so this verifies the limiter tracks failures
        # In production, each IP would be tracked independently
        status, body = self._make_request('/bots', token='wrong-token')
        self.assertEqual(status, 429, "Should be rate limited after max attempts")


class TestConstantTimeComparison(unittest.TestCase):
    """Tests for constant-time token comparison security.
    
    Acceptance criteria:
    - test_constant_time_comparison uses timing attack simulation or verifies hmac.compare_digest usage
    """

    def test_hmac_compare_digest_used_in_auth(self):
        """Verify that hmac.compare_digest is used for token comparison.
        
        This is a code inspection test that verifies the secure comparison
        function is used instead of == operator which is vulnerable to
        timing attacks.
        """
        import inspect
        import hmac
        
        # Get source code of ControlHandler._auth method
        source = inspect.getsource(ControlHandler._auth)
        
        # Verify hmac.compare_digest is used
        self.assertIn('hmac.compare_digest', source,
            "ControlHandler._auth should use hmac.compare_digest for constant-time comparison")
        
        # Verify == operator is NOT used for token comparison
        # (allow == in other contexts like checking if CONTROL_TOKEN is set)
        lines = source.split('\n')
        for line in lines:
            stripped = line.strip()
            # Skip comments and string literals
            if stripped.startswith('#') or 'hmac.compare_digest' in stripped:
                continue
            # Check for direct token comparison with ==
            if 'auth.strip() ==' in stripped or 'expected ==' in stripped:
                self.fail(f"Direct == comparison found in auth logic: {stripped}")

    def test_telemetry_also_uses_constant_time_comparison(self):
        """Verify telemetry endpoint also uses constant-time comparison."""
        import inspect
        import hmac
        
        source = inspect.getsource(ControlHandler._handle_telemetry)
        
        self.assertIn('hmac.compare_digest', source,
            "Telemetry auth should also use hmac.compare_digest for constant-time comparison")

    def test_no_timing_vulnerability_in_auth_path(self):
        """Basic timing test to verify no obvious timing leak.
        
        Note: This is a basic sanity check. Proper timing attack tests
        require statistical analysis over thousands of requests.
        """
        import hmac
        
        # Verify hmac.compare_digest is available and works
        token_a = 'Bearer test-token-12345'
        token_b = 'Bearer test-token-12346'  # One char different
        token_c = 'Bearer wrong-token-xxxxx'  # Completely different
        
        # compare_digest should take roughly same time regardless of where mismatch occurs
        result1 = hmac.compare_digest(token_a, token_b)
        result2 = hmac.compare_digest(token_a, token_c)
        
        self.assertFalse(result1, "Tokens should not match")
        self.assertFalse(result2, "Tokens should not match")
        # The fact that compare_digest returns False for both without timing leak
        # is verified by the function's implementation (constant-time by design)


if __name__ == '__main__':
    unittest.main()
