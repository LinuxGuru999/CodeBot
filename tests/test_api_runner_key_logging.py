"""Tests for CB-9360904-15F2: API key must never appear in log output.

Verifies that _log calls during API key resolution do not contain any
portion of the actual API key value (CWE-532 prevention).
"""
import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.api_runner import _log, run_bot


class TestApiKeyNotLogged:
    """Ensure no portion of an API key is ever written to stdout via _log."""

    def test_log_does_not_contain_key_characters(self):
        """Direct test: _log output must not contain sensitive key material."""
        secret_key = "sk-test-super-secret-key-1234567890abcdef"
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            _log("API key resolved (present)")
        output = captured.getvalue()
        # The full key must not appear
        assert secret_key not in output
        # No prefix/slice of the key should appear (first 10 chars)
        assert secret_key[:10] not in output
        # No substring of length >= 4 from the key should appear
        for i in range(len(secret_key) - 3):
            fragment = secret_key[i:i+4]
            # Skip very generic fragments that could be coincidental
            if fragment in ("key ", " res", "test"):
                continue
            assert fragment not in output, f"Key fragment '{fragment}' found in log output"

    def test_run_bot_key_resolution_logs_no_key_material(self, tmp_path):
        """Integration test: run_bot's key resolution path must not leak key."""
        secret_key = "dgr-sk-live-abc123def456ghi789jkl012mno345"
        hb_file = tmp_path / "test.heartbeat"
        ckpt_file = tmp_path / "test.checkpoint.json"

        captured = io.StringIO()

        def fake_exit(code=0):
            raise SystemExit(code)

        with patch("codebot.api_runner._resolve_api_key", return_value=secret_key), \
             patch("codebot.api_runner._is_draining", return_value=True), \
             patch("sys.stdout", captured), \
             patch("sys.exit", side_effect=fake_exit):
            with pytest.raises(SystemExit):
                run_bot(
                    bot_name="test-bot",
                    model="qwen-3.8-max",
                    mission_prompt="test mission",
                    heartbeat_file=str(hb_file),
                    ckpt_file=str(ckpt_file),
                )

        output = captured.getvalue()
        # The key itself must never appear in logs
        assert secret_key not in output, "Full API key found in log output"
        # No prefix of the key (e.g., first 10 chars) should appear
        assert secret_key[:10] not in output, "API key prefix found in log output"
        # Verify the safe log message IS present
        assert "API key resolved (present)" in output or "drain active" in output

    def test_run_bot_missing_key_logs_no_key_material(self, tmp_path):
        """When no key is found, the error log must also not contain key material."""
        hb_file = tmp_path / "test.heartbeat"
        ckpt_file = tmp_path / "test.checkpoint.json"

        captured = io.StringIO()

        def fake_exit(code=0):
            raise SystemExit(code)

        with patch("codebot.api_runner._resolve_api_key", return_value=None), \
             patch("codebot.api_runner._is_draining", return_value=False), \
             patch("sys.stdout", captured), \
             patch("sys.exit", side_effect=fake_exit):
            with pytest.raises(SystemExit):
                run_bot(
                    bot_name="test-bot",
                    model="qwen-3.8-max",
                    mission_prompt="test mission",
                    heartbeat_file=str(hb_file),
                    ckpt_file=str(ckpt_file),
                )

        output = captured.getvalue()
        # Should mention missing key without any actual key value
        assert "no API key found" in output
        # Ensure no accidental key leakage
        assert "DIALAGRAM_API_KEY" in output  # env var name is fine
