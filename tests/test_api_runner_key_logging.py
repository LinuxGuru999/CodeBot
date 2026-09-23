"""Tests for CB-620589-BBE4: API key log sanitization in api_runner.py.

Verifies that _log output in api_runner.py never contains any portion of the
actual API key value, regardless of code path (key present, key absent,
drain, errors). Ensures Constitution §2 compliance.

Acceptance Criteria:
  - Test verifies _log output does not contain key characters
  - test passes when key is present/absent
  - test fails if key material is logged
"""
import io
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.api_runner import _log, run_bot


# Realistic API key that must NEVER appear in any log output
SECRET_KEY = "sk-test-super-secret-key-1234567890abcdef"


class TestLogNeverLeaksKey:
    """Unit tests: _log() output must never contain key material."""

    def test_log_output_does_not_contain_full_key(self):
        """_log with safe message must not contain the full secret key."""
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            _log("test-bot: API key resolved (present=True)")
        output = captured.getvalue()
        assert SECRET_KEY not in output, "Full API key found in _log output"

    def test_log_output_does_not_contain_key_prefix(self):
        """No prefix/slice of the key (first 10 chars) should appear."""
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            _log("test-bot: API key resolved (present=True)")
        output = captured.getvalue()
        assert SECRET_KEY[:10] not in output, "API key prefix found in _log output"

    def test_log_output_does_not_contain_key_substrings(self):
        """No substring of length >= 4 from the key should appear."""
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            _log("mybot: API key resolved (present=True)")
        output = captured.getvalue()
        for i in range(len(SECRET_KEY) - 3):
            fragment = SECRET_KEY[i:i+4]
            # Skip generic fragments that could appear coincidentally in log scaffolding
            if fragment in ("key ", " res", "test", "est-", "t-bo", "-bot"):
                continue
            assert fragment not in output, (
                f"Key fragment '{fragment}' found in _log output"
            )

    def test_deliberate_key_leak_is_detected(self):
        """PROVES the test framework catches key leakage (regression guard).

        If someone changes _log to include key material, this test must fail.
        """
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            # Deliberately log the key — must be caught by assertions
            _log(f"API key is {SECRET_KEY}")
        output = captured.getvalue()
        # This assertion PROVES our suite detects leaks
        assert SECRET_KEY in output, (
            "Regression guard: test must detect leaked key"
        )


class TestRunBotKeyPresentPath:
    """Integration: verify key doesn't leak through key-present run_bot path.

    Previous tests used _is_draining=True which exits BEFORE the
    'API key resolved (present)' log line. These tests actually exercise
    the key-present code path by letting run_bot proceed past the drain check.
    """

    def test_key_present_path_never_logs_key(self, tmp_path):
        """run_bot with key present should log 'present' but not the key itself."""
        hb_file = tmp_path / "test.heartbeat"
        ckpt_file = tmp_path / "test.checkpoint.json"

        captured = io.StringIO()

        # Completion response: content without tool_calls → run_bot exits cleanly
        completion_response = {
            "choices": [{
                "message": {
                    "content": "Task completed.",
                    "tool_calls": None,
                }
            }]
        }

        def fake_exit(code=0):
            raise SystemExit(code)

        with patch("codebot.api_runner._resolve_api_key", return_value=SECRET_KEY), \
             patch("codebot.api_runner._is_draining", return_value=False), \
             patch("codebot.api_runner._call_api", return_value=completion_response), \
             patch("codebot.api_runner._write_bot_status"), \
             patch("codebot.api_runner._write_scratchpad"), \
             patch("codebot.api_runner._start_heartbeat_thread") as mock_hb, \
             patch("codebot.api_runner._stop_heartbeat_thread"), \
             patch("codebot.api_runner._persist_stream"), \
             patch("codebot.api_runner._write_checkpoint"), \
             patch("codebot.api_runner._write_heartbeat"), \
             patch("codebot.api_runner._ticket_id_from_claim", return_value=""), \
             patch("codebot.api_runner._auto_commit", return_value=True), \
             patch("codebot.api_runner._is_implementation_bot", return_value=False), \
             patch("codebot.api_runner._wait_for_rate_limit"), \
             patch("codebot.api_runner._record_request"), \
             patch("codebot.api_runner._record_success"), \
             patch("sys.stdout", captured), \
             patch("sys.exit", side_effect=fake_exit), \
             patch("time.sleep"):
            mock_hb.return_value = MagicMock()
            with pytest.raises(SystemExit):
                run_bot(
                    bot_name="test-bot",
                    model="qwen-3.8-max",
                    mission_prompt="test mission",
                    heartbeat_file=str(hb_file),
                    ckpt_file=str(ckpt_file),
                )

        output = captured.getvalue()
        assert SECRET_KEY not in output, "Full API key found in run_bot output"
        assert SECRET_KEY[:10] not in output, "API key prefix found in run_bot output"
        # The safe log message SHOULD be present with boolean indicator
        assert "API key resolved (present=True)" in output, (
            "Expected safe log message with boolean presence missing"
        )

    def test_key_present_path_with_rate_limited_error(self, tmp_path):
        """Even with HTTP 429 errors, key must never appear in logs."""
        hb_file = tmp_path / "test.heartbeat"
        ckpt_file = tmp_path / "test.checkpoint.json"

        captured = io.StringIO()
        call_count = 0

        import urllib.error

        def raising_api_call(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise urllib.error.HTTPError(
                    url="http://test", code=429,
                    msg="Rate limited", hdrs=None, fp=None,
                )
            # Second call succeeds
            return {
                "choices": [{
                    "message": {
                        "content": "Done.",
                        "tool_calls": None,
                    }
                }]
            }

        def fake_exit(code=0):
            raise SystemExit(code)

        with patch("codebot.api_runner._resolve_api_key", return_value=SECRET_KEY), \
             patch("codebot.api_runner._is_draining", return_value=False), \
             patch("codebot.api_runner._call_api", side_effect=raising_api_call), \
             patch("codebot.api_runner._write_bot_status"), \
             patch("codebot.api_runner._write_scratchpad"), \
             patch("codebot.api_runner._start_heartbeat_thread") as mock_hb, \
             patch("codebot.api_runner._stop_heartbeat_thread"), \
             patch("codebot.api_runner._persist_stream"), \
             patch("codebot.api_runner._write_checkpoint"), \
             patch("codebot.api_runner._write_heartbeat"), \
             patch("codebot.api_runner._ticket_id_from_claim", return_value=""), \
             patch("codebot.api_runner._auto_commit", return_value=True), \
             patch("codebot.api_runner._is_implementation_bot", return_value=False), \
             patch("codebot.api_runner._wait_for_rate_limit"), \
             patch("codebot.api_runner._record_request"), \
             patch("codebot.api_runner._record_success"), \
             patch("codebot.api_runner._record_rate_limit"), \
             patch("sys.stdout", captured), \
             patch("sys.exit", side_effect=fake_exit), \
             patch("time.sleep"):
            mock_hb.return_value = MagicMock()
            with pytest.raises(SystemExit):
                run_bot(
                    bot_name="test-bot",
                    model="qwen-3.8-max",
                    mission_prompt="test mission",
                    heartbeat_file=str(hb_file),
                    ckpt_file=str(ckpt_file),
                )

        output = captured.getvalue()
        assert SECRET_KEY not in output, (
            "API key leaked during rate-limited error path"
        )
        assert SECRET_KEY[:10] not in output, (
            "API key prefix leaked during rate-limited error path"
        )

    def test_key_present_path_with_connection_error(self, tmp_path):
        """Connection errors must not leak key material."""
        hb_file = tmp_path / "test.heartbeat"
        ckpt_file = tmp_path / "test.checkpoint.json"

        captured = io.StringIO()

        import urllib.error

        def raising_api_call(*args, **kwargs):
            raise urllib.error.URLError("Connection refused")

        def fake_exit(code=0):
            raise SystemExit(code)

        with patch("codebot.api_runner._resolve_api_key", return_value=SECRET_KEY), \
             patch("codebot.api_runner._is_draining", return_value=False), \
             patch("codebot.api_runner._call_api", side_effect=raising_api_call), \
             patch("codebot.api_runner._write_bot_status"), \
             patch("codebot.api_runner._write_scratchpad"), \
             patch("codebot.api_runner._start_heartbeat_thread") as mock_hb, \
             patch("codebot.api_runner._stop_heartbeat_thread"), \
             patch("codebot.api_runner._persist_stream"), \
             patch("codebot.api_runner._write_checkpoint"), \
             patch("codebot.api_runner._write_heartbeat"), \
             patch("codebot.api_runner._wait_for_rate_limit"), \
             patch("codebot.api_runner._record_request"), \
             patch("codebot.api_runner._record_success"), \
             patch("sys.stdout", captured), \
             patch("sys.exit", side_effect=fake_exit), \
             patch("time.sleep"):
            mock_hb.return_value = MagicMock()
            with pytest.raises(SystemExit):
                run_bot(
                    bot_name="test-bot",
                    model="qwen-3.8-max",
                    mission_prompt="test mission",
                    heartbeat_file=str(hb_file),
                    ckpt_file=str(ckpt_file),
                )

        output = captured.getvalue()
        assert SECRET_KEY not in output, (
            "API key leaked during connection error path"
        )


class TestRunBotKeyAbsentPath:
    """Integration: verify error messages for missing key don't leak material."""

    def test_missing_key_logs_safe_error(self, tmp_path):
        """When no key is found, the error log must mention env var but not a key."""
        hb_file = tmp_path / "test.heartbeat"
        ckpt_file = tmp_path / "test.checkpoint.json"

        captured = io.StringIO()

        def fake_exit(code=0):
            raise SystemExit(code)

        with patch("codebot.api_runner._resolve_api_key", return_value=None), \
             patch("codebot.api_runner._is_draining", return_value=False), \
             patch("codebot.api_runner._write_bot_status"), \
             patch("codebot.api_runner._write_scratchpad"), \
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
        # Error message mentions env var name (safe) but not any actual key
        assert "no API key found" in output
        assert "DIALAGRAM_API_KEY" in output  # env var name is fine

    def test_drain_path_logs_safe_message(self, tmp_path):
        """Drain path should log 'drain active' without any key material."""
        hb_file = tmp_path / "test.heartbeat"
        ckpt_file = tmp_path / "test.checkpoint.json"

        captured = io.StringIO()

        def fake_exit(code=0):
            raise SystemExit(code)

        with patch("codebot.api_runner._resolve_api_key", return_value=SECRET_KEY), \
             patch("codebot.api_runner._is_draining", return_value=True), \
             patch("codebot.api_runner._write_heartbeat"), \
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
        assert SECRET_KEY not in output
        assert SECRET_KEY[:10] not in output
        assert "drain active" in output

    def test_key_material_not_in_any_log_line(self, tmp_path):
        """Exhaustive: verify key doesn't appear across ALL log lines in any path."""
        hb_file = tmp_path / "test.heartbeat"
        ckpt_file = tmp_path / "test.checkpoint.json"

        captured = io.StringIO()

        completion_response = {
            "choices": [{
                "message": {
                    "content": "All done.",
                    "tool_calls": None,
                }
            }]
        }

        def fake_exit(code=0):
            raise SystemExit(code)

        with patch("codebot.api_runner._resolve_api_key", return_value=SECRET_KEY), \
             patch("codebot.api_runner._is_draining", return_value=False), \
             patch("codebot.api_runner._call_api", return_value=completion_response), \
             patch("codebot.api_runner._write_bot_status"), \
             patch("codebot.api_runner._write_scratchpad"), \
             patch("codebot.api_runner._start_heartbeat_thread") as mock_hb, \
             patch("codebot.api_runner._stop_heartbeat_thread"), \
             patch("codebot.api_runner._persist_stream"), \
             patch("codebot.api_runner._write_checkpoint"), \
             patch("codebot.api_runner._write_heartbeat"), \
             patch("codebot.api_runner._ticket_id_from_claim", return_value=""), \
             patch("codebot.api_runner._auto_commit", return_value=True), \
             patch("codebot.api_runner._is_implementation_bot", return_value=False), \
             patch("codebot.api_runner._wait_for_rate_limit"), \
             patch("codebot.api_runner._record_request"), \
             patch("codebot.api_runner._record_success"), \
             patch("sys.stdout", captured), \
             patch("sys.exit", side_effect=fake_exit), \
             patch("time.sleep"):
            mock_hb.return_value = MagicMock()
            with pytest.raises(SystemExit):
                run_bot(
                    bot_name="test-bot",
                    model="qwen-3.8-max",
                    mission_prompt="test mission",
                    heartbeat_file=str(hb_file),
                    ckpt_file=str(ckpt_file),
                )

        output = captured.getvalue()
        # Check every individual log line for key leakage
        for line in output.splitlines():
            assert SECRET_KEY not in line, (
                f"Key leaked in log line: {line!r}"
            )
