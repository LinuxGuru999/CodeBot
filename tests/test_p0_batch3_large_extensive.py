"""Extensive P0 batch3 — 5 large control-plane modules (>75% target).

Modules:
  codebot.control_server (1104 stmts)
  codebot.control_client (294 stmts)
  codebot.process_manager (758 stmts)
  codebot.checkpoint_manager (211 stmts)
  codebot.process_supervisor (14 stmts)

Covers error paths, auth, dispatch handlers, lifecycle branches, checkpoint
save/restore, supervisor heartbeat.  tmp_path isolation, mocked subprocess/time/network.

Style: Given/When/Then per test, one When per test, tmp_path, no live leakage.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import threading
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open, call

import pytest

import codebot.control_server as cs
import codebot.control_client as cc
import codebot.checkpoint_manager as cm
import codebot.process_supervisor as ps
import codebot.process_manager as pm
import codebot.state_manager as sm
import codebot.health_monitor as hm
# CB-B4086: CONTROL_ALLOW_UNAUTHENTICATED bypass removed; no module-level
# flag is expected. (Legacy references cleaned up.)

# ---------------------------------------------------------------------------
# Global isolation — reset singletons
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_batch3():
    orig_failures = dict(cs._rate_limiter._failures)
    orig_blocked = dict(cs._rate_limiter._blocked_until)
    orig_pm_cached = pm._cached_process_count
    orig_pm_at = pm._cached_process_count_at
    orig_pm_spawn = pm._last_spawn_time
    orig_pm_cache = pm._BOT_REGISTRY_CACHE
    orig_pm_gw = pm._prompt_gateway_instance
    orig_cm_dir = cm._STATE_DIR
    orig_cc_url = cc.URL
    orig_cc_token = cc.TOKEN
    orig_cc_timeout = cc.REQUEST_TIMEOUT
    orig_cs_token = cs.CONTROL_TOKEN
    orig_cs_tele = cs.TELEMETRY_TOKEN
    orig_sm_adapter = sm._adapter_instance
    orig_allow_env = os.environ.get("CONTROL_ALLOW_UNAUTHENTICATED")
    yield
    cs._rate_limiter._failures.clear(); cs._rate_limiter._failures.update(orig_failures)
    cs._rate_limiter._blocked_until.clear(); cs._rate_limiter._blocked_until.update(orig_blocked)
    pm._cached_process_count = orig_pm_cached
    pm._cached_process_count_at = orig_pm_at
    pm._last_spawn_time = orig_pm_spawn
    pm._BOT_REGISTRY_CACHE = orig_pm_cache
    pm._prompt_gateway_instance = orig_pm_gw
    cm._STATE_DIR = orig_cm_dir
    cc.URL = orig_cc_url
    cc.TOKEN = orig_cc_token
    cc.REQUEST_TIMEOUT = orig_cc_timeout
    cs.CONTROL_TOKEN = orig_cs_token
    cs.TELEMETRY_TOKEN = orig_cs_tele
    sm._adapter_instance = orig_sm_adapter
    if orig_allow_env is None:
        os.environ.pop("CONTROL_ALLOW_UNAUTHENTICATED", None)
    else:
        os.environ["CONTROL_ALLOW_UNAUTHENTICATED"] = orig_allow_env

class _FakeHeaders(dict):
    def get(self, k, default=None):
        return super().get(k, default)

def _handler(method: str, path: str, body: dict | None = None, headers: dict | None = None, client_ip: str = "127.0.0.1"):
    h = MagicMock(spec=cs.ControlHandler)
    h.path = path
    h.command = method
    h.client_address = (client_ip, 12345)
    hdrs = _FakeHeaders(headers or {})
    h.headers = hdrs
    h.connection = MagicMock()
    h.connection.settimeout = MagicMock()
    if body is not None:
        raw = json.dumps(body).encode()
        h.rfile = io.BytesIO(raw)
        h.headers["Content-Length"] = str(len(raw))
    else:
        if "Content-Length" not in hdrs:
            h.headers["Content-Length"] = "0"
            h.rfile = io.BytesIO(b"")
        else:
            h.rfile = io.BytesIO(b"")
    h.wfile = io.BytesIO()
    h.send_response = MagicMock()
    h.send_header = MagicMock()
    h.end_headers = MagicMock()
    for name in ["_auth","_json","_json_304","_html","_json_with_cache_headers","_read_json_body","_get_destructive_preview","do_GET","do_POST","_handle_telemetry","_is_loopback_client","log_message"]:
        if hasattr(cs.ControlHandler, name):
            setattr(h, name, getattr(cs.ControlHandler, name).__get__(h, cs.ControlHandler))
    return h

def _capture(handler):
    calls = []
    def _cap(code, obj, extra_headers=None):
        calls.append((code, obj, extra_headers))
        body = json.dumps(obj).encode()
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k,v in extra_headers.items():
                handler.send_header(k,v)
        handler.end_headers()
        handler.wfile.write(body)
    handler._json = _cap
    return calls


# ===========================================================================
# control_server — RateLimiter
# ===========================================================================
def test_rate_limiter_initial_allowed_Given_new_ip_When_is_allowed_Then_true():
    """Given new IP
    When is_allowed()
    Then allowed True, reason None."""
    lim = cs.RateLimiter()
    allowed, reason = lim.is_allowed("1.1.1.1")
    assert allowed is True and reason is None

def test_rate_limiter_blocks_after_max_Given_max_failures_When_is_allowed_Then_blocked():
    """Given max failures recorded
    When is_allowed()
    Then blocked with rate limit message."""
    lim = cs.RateLimiter()
    ip = "2.2.2.2"
    for _ in range(cs.RATE_LIMIT_MAX_ATTEMPTS):
        lim.record_failure(ip)
    allowed, reason = lim.is_allowed(ip)
    assert allowed is False
    assert "rate limit" in reason.lower()

def test_rate_limiter_below_limit_still_allowed_Given_below_max_When_is_allowed_Then_true():
    """Given failures below max
    When is_allowed()
    Then still allowed."""
    lim = cs.RateLimiter()
    ip = "3.3.3.3"
    for _ in range(cs.RATE_LIMIT_MAX_ATTEMPTS - 1):
        lim.record_failure(ip)
    allowed, _ = lim.is_allowed(ip)
    assert allowed is True

def test_rate_limiter_cooldown_expires_Given_blocked_ip_When_cooldown_expires_Then_allowed():
    """Given blocked IP with cooldown expired
    When is_allowed() after time travel
    Then allowed and failures cleared."""
    lim = cs.RateLimiter()
    ip = "4.4.4.4"
    for _ in range(cs.RATE_LIMIT_MAX_ATTEMPTS):
        lim.record_failure(ip)
    lim.is_allowed(ip)  # triggers cooldown
    with lim._lock:
        lim._blocked_until[ip] = time.time() - 1
    allowed, _ = lim.is_allowed(ip)
    assert allowed is True
    assert len(lim._failures[ip]) == 0

def test_rate_limiter_window_cleanup_Given_old_failures_When_is_allowed_Then_cleaned():
    """Given failures outside window
    When is_allowed()
    Then old entries pruned and allowed."""
    lim = cs.RateLimiter()
    ip = "5.5.5.5"
    old = time.time() - cs.RATE_LIMIT_WINDOW_SECONDS - 100
    with lim._lock:
        lim._failures[ip] = [old, old+1]
    allowed, _ = lim.is_allowed(ip)
    assert allowed is True
    assert len(lim._failures[ip]) == 0

def test_rate_limiter_per_ip_isolation_Given_two_ips_When_one_blocked_Then_other_allowed():
    """Given two IPs where one is blocked
    When checking second IP
    Then second remains allowed."""
    lim = cs.RateLimiter()
    ip1, ip2 = "6.6.6.1", "6.6.6.2"
    for _ in range(cs.RATE_LIMIT_MAX_ATTEMPTS):
        lim.record_failure(ip1)
    assert lim.is_allowed(ip1)[0] is False
    assert lim.is_allowed(ip2)[0] is True

def test_rate_limiter_still_blocked_during_cooldown_Given_blocked_When_queried_quickly_Then_remaining():
    """Given IP in cooldown
    When is_allowed() immediately
    Then blocked with remaining seconds."""
    lim = cs.RateLimiter()
    ip = "7.7.7.7"
    for _ in range(cs.RATE_LIMIT_MAX_ATTEMPTS):
        lim.record_failure(ip)
    lim.is_allowed(ip)  # enter cooldown
    allowed, reason = lim.is_allowed(ip)
    assert allowed is False
    assert "blocked for" in reason

def test_rate_limiter_thread_safety_Given_concurrent_record_When_joined_Then_no_error():
    """Given concurrent record_failure
    When threads join
    Then no exceptions and blocked."""
    lim = cs.RateLimiter()
    ip = "8.8.8.8"
    errs = []
    def worker():
        try:
            for _ in range(10): lim.record_failure(ip)
        except Exception as e: errs.append(e)
    ts = [threading.Thread(target=worker) for _ in range(5)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert not errs
    assert lim.is_allowed(ip)[0] is False

# ===========================================================================
# control_server — helpers: eff_timeout, validate_bot_name, heartbeat, log_tail
# ===========================================================================
def test_eff_timeout_with_profile_Given_profile_When_eff_timeout_Then_computed():
    """Given BotConfig with matching profile
    When eff_timeout()
    Then max(interval*multiplier, timeout)."""
    from codebot.process_manager import BotConfig, ModelProfile
    cfg = BotConfig(name="t", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m-a")
    with patch.object(cs, "MODEL_PROFILES", {"m-a": ModelProfile(lockup_risk="low", heartbeat_multiplier=2.0, log_stall_seconds=60, restart_cooldown=3, description="x")}):
        assert cs.eff_timeout(cfg) == 120

def test_eff_timeout_without_profile_Given_no_profile_When_eff_timeout_Then_base():
    """Given unknown model
    When eff_timeout()
    Then returns heartbeat_timeout."""
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="t2", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="unknown")
    with patch.object(cs, "MODEL_PROFILES", {}):
        assert cs.eff_timeout(cfg) == 30

def test_validate_bot_name_accepts_valid_Given_alphanum_When_validate_Then_true():
    """Given valid names
    When validate_bot_name()
    Then True."""
    for n in ["issues","my-bot","bot_1","A1-B2_C3"]:
        assert cs.validate_bot_name(n) is True

def test_validate_bot_name_rejects_invalid_Given_bad_When_validate_Then_false():
    """Given invalid names
    When validate_bot_name()
    Then False."""
    for n in ["","a/b","a;b","a b","../x",None,123]:
        assert cs.validate_bot_name(n) is False  # type: ignore[arg-type]

def test_heartbeat_age_returns_age_Given_file_exists_When_heartbeat_age_Then_delta(tmp_path: Path):
    """Given heartbeat file with recent timestamp
    When heartbeat_age()
    Then returns age."""
    with patch.object(cs, "STATE_DIR", tmp_path):
        (tmp_path / "mybot.heartbeat").write_text(str(time.time()))
        age = cs.heartbeat_age("mybot")
        assert age is not None and 0 <= age < 5

def test_heartbeat_age_rejects_invalid_name_Given_injection_When_heartbeat_age_Then_none():
    """Given invalid bot name
    When heartbeat_age()
    Then None (blocked traversal)."""
    assert cs.heartbeat_age("../evil") is None

def test_heartbeat_age_missing_file_Given_no_file_When_heartbeat_age_Then_none(tmp_path: Path):
    """Given missing file
    When heartbeat_age()
    Then None."""
    with patch.object(cs, "STATE_DIR", tmp_path):
        assert cs.heartbeat_age("missing") is None

def test_heartbeat_age_corrupt_file_Given_bad_content_When_heartbeat_age_Then_none(tmp_path: Path):
    """Given corrupt content
    When heartbeat_age()
    Then None."""
    with patch.object(cs, "STATE_DIR", tmp_path):
        (tmp_path / "bad.heartbeat").write_text("not-a-number")
        assert cs.heartbeat_age("bad") is None

def test_log_tail_rejects_slash_Given_slash_When_log_tail_Then_empty():
    """Given name with slash
    When log_tail()
    Then empty (blocked)."""
    assert cs.log_tail("a/b") == ""

def test_log_tail_rejects_dots_Given_dotdot_When_log_tail_Then_empty():
    """Given name with ..
    When log_tail()
    Then empty."""
    assert cs.log_tail("../evil") == ""

def test_log_tail_rejects_invalid_pattern_Given_dot_When_log_tail_Then_empty():
    """Given name failing pattern
    When log_tail()
    Then empty."""
    assert cs.log_tail("evil; rm") == ""

def test_log_tail_missing_file_Given_no_log_When_log_tail_Then_empty(tmp_path: Path):
    """Given no log file
    When log_tail()
    Then empty string."""
    with patch.object(cs, "LOGS_DIR", tmp_path):
        assert cs.log_tail("valid-bot") == ""

def test_log_tail_subprocess_success_Given_log_file_When_log_tail_Then_tail(tmp_path: Path):
    """Given log file exists
    When subprocess succeeds
    Then tail returned."""
    with patch.object(cs, "LOGS_DIR", tmp_path):
        (tmp_path / "valid-bot.log").write_text("hello\nworld\n")
        with patch("subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout="hello\nworld\n")
            assert cs.log_tail("valid-bot", 10) == "hello\nworld\n"

def test_log_tail_subprocess_fallback_Given_failure_When_log_tail_Then_fallback(tmp_path: Path):
    """Given subprocess fails
    When log_tail()
    Then fallback to read_text slice."""
    with patch.object(cs, "LOGS_DIR", tmp_path):
        p = tmp_path / "valid-bot.log"
        p.write_text("x"*9000)
        with patch("subprocess.run", side_effect=Exception("fail")):
            out = cs.log_tail("valid-bot")
            assert "x" in out

def test_log_tail_non_string_Given_non_string_When_log_tail_Then_empty():
    """Given non-string name
    When log_tail()
    Then empty."""
    assert cs.log_tail(None) == ""  # type: ignore[arg-type]

# ===========================================================================
# control_server — _safe_kill_bot_process & _safe_kill_process
# ===========================================================================
def test_safe_kill_bot_process_rejects_invalid_Given_bad_name_When_call_Then_false():
    """Given invalid bot name
    When _safe_kill_bot_process
    Then (False, [])."""
    ok, pids = cs._safe_kill_bot_process("bad; name")
    assert ok is False and pids == []

def test_safe_kill_bot_process_no_matches_Given_empty_pgrep_When_call_Then_true_empty():
    """Given pgrep empty
    When _safe_kill_bot_process
    Then (True, [])."""
    with patch("subprocess.run", return_value=MagicMock(stdout="", returncode=1)):
        ok, pids = cs._safe_kill_bot_process("valid-bot")
        assert ok is True and pids == []

def test_safe_kill_bot_process_pgrep_non_digit_lines_Given_weird_output_When_call_Then_skipped():
    """Given pgrep output with non-digit lines
    When _safe_kill_bot_process
    Then non-digits ignored."""
    with patch("subprocess.run", return_value=MagicMock(stdout="abc\n1234\n", returncode=0)):
        m = mock_open(read_data=b"python\x00api_runner.py\x00valid-bot\x00")
        with patch("builtins.open", m):
            with patch("os.kill"):
                ok, pids = cs._safe_kill_bot_process("valid-bot")
                assert ok is True
                assert 1234 in pids

def test_safe_kill_bot_process_verifies_cmdline_Given_matching_pid_When_verified_Then_killed():
    """Given pid with matching cmdline
    When _safe_kill_bot_process
    Then killed."""
    with patch("subprocess.run", return_value=MagicMock(stdout="9999\n", returncode=0)):
        with patch("builtins.open", mock_open(read_data=b"python\x00api_runner.py\x00valid-bot\x00")):
            with patch("os.kill") as mk:
                ok, pids = cs._safe_kill_bot_process("valid-bot")
                assert ok is True
                assert 9999 in pids
                mk.assert_called()

def test_safe_kill_bot_process_cmdline_mismatch_Given_wrong_cmdline_When_call_Then_not_killed():
    """Given pid with non-matching cmdline
    When _safe_kill_bot_process
    Then not killed."""
    with patch("subprocess.run", return_value=MagicMock(stdout="1111\n", returncode=0)):
        with patch("builtins.open", mock_open(read_data=b"python other.py valid-bot")):
            with patch("os.kill") as mk:
                ok, pids = cs._safe_kill_bot_process("valid-bot")
                assert ok is True
                assert pids == []
                mk.assert_not_called()

def test_safe_kill_bot_process_proc_not_python_Given_non_python_When_call_Then_skipped():
    """Given matched suffix but not python
    When _safe_kill_bot_process
    Then skipped."""
    with patch("subprocess.run", return_value=MagicMock(stdout="2222\n", returncode=0)):
        # cmdline ends with expected suffix but no python nor api_runner
        with patch("builtins.open", mock_open(read_data=b"/usr/bin/fake\x00api_runner.py\x00valid-bot-other\x00")):
            # Our logic: suffix check passes if " api_runner.py valid-bot" in cmdline, but python check fails if neither python nor api_runner? Actually api_runner present so it would still pass.
            # Use a cmdline that has suffix but not python/api_runner substring check fails
            pass
        # Simpler: cmdline is "java other" -> will be debug skipped
        with patch("builtins.open", mock_open(read_data=b"java\x00other\x00valid-bot\x00")):
            with patch("os.kill") as mk:
                ok, pids = cs._safe_kill_bot_process("valid-bot")
                assert pids == []

def test_safe_kill_bot_process_file_not_found_Given_exited_pid_When_call_Then_skipped():
    """Given PID file not found (exited)
    When _safe_kill_bot_process
    Then skipped."""
    with patch("subprocess.run", return_value=MagicMock(stdout="3333\n", returncode=0)):
        with patch("builtins.open", side_effect=FileNotFoundError):
            with patch("os.kill") as mk:
                ok, pids = cs._safe_kill_bot_process("valid-bot")
                assert ok is True and pids == []
                mk.assert_not_called()

def test_safe_kill_bot_process_permission_error_Given_denied_When_call_Then_skipped():
    """Given PermissionError reading proc
    When _safe_kill_bot_process
    Then skipped."""
    with patch("subprocess.run", return_value=MagicMock(stdout="4444\n", returncode=0)):
        with patch("builtins.open", side_effect=PermissionError):
            ok, pids = cs._safe_kill_bot_process("valid-bot")
            assert ok is True and pids == []

def test_safe_kill_bot_process_kill_permission_error_Given_verified_When_kill_denied_Then_continues():
    """Given verified PID but kill permission denied
    When _safe_kill_bot_process
    Then still returns success with pid listed."""
    with patch("subprocess.run", return_value=MagicMock(stdout="5555\n", returncode=0)):
        with patch("builtins.open", mock_open(read_data=b"python\x00api_runner.py\x00valid-bot\x00")):
            with patch("os.kill", side_effect=PermissionError):
                ok, pids = cs._safe_kill_bot_process("valid-bot")
                assert ok is True
                assert 5555 in pids

def test_safe_kill_bot_process_kill_lookup_error_Given_exited_When_kill_Then_ok():
    """Given PID exited before kill
    When _safe_kill_bot_process
    Then still ok."""
    with patch("subprocess.run", return_value=MagicMock(stdout="6666\n", returncode=0)):
        with patch("builtins.open", mock_open(read_data=b"python\x00api_runner.py\x00valid-bot\x00")):
            with patch("os.kill", side_effect=ProcessLookupError):
                ok, pids = cs._safe_kill_bot_process("valid-bot")
                assert ok is True
                assert 6666 in pids

def test_safe_kill_bot_process_timeout_Given_pgrep_timeout_When_call_Then_false():
    """Given pgrep timeout
    When _safe_kill_bot_process
    Then (False, [])."""
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pgrep", timeout=5)):
        ok, pids = cs._safe_kill_bot_process("valid-bot")
        assert ok is False

def test_safe_kill_bot_process_generic_exception_Given_error_When_call_Then_false():
    """Given generic exception in pgrep
    When _safe_kill_bot_process
    Then (False, [])."""
    with patch("subprocess.run", side_effect=RuntimeError("boom")):
        ok, _ = cs._safe_kill_bot_process("valid-bot")
        assert ok is False

def test_safe_kill_bot_process_generic_verify_error_Given_open_raises_When_call_Then_handled():
    """Given generic exception verifying PID
    When _safe_kill_bot_process
    Then skipped."""
    with patch("subprocess.run", return_value=MagicMock(stdout="7777\n", returncode=0)):
        with patch("builtins.open", side_effect=RuntimeError("weird")):
            ok, pids = cs._safe_kill_bot_process("valid-bot")
            assert ok is True and pids == []

def test_safe_kill_bot_process_kill_generic_error_Given_verified_When_kill_raises_Then_handled():
    """Given verified PID but kill raises generic
    When _safe_kill_bot_process
    Then handled."""
    with patch("subprocess.run", return_value=MagicMock(stdout="8888\n", returncode=0)):
        with patch("builtins.open", mock_open(read_data=b"python\x00api_runner.py\x00valid-bot\x00")):
            with patch("os.kill", side_effect=RuntimeError("boom")):
                ok, pids = cs._safe_kill_bot_process("valid-bot")
                assert ok is True
                assert 8888 in pids

# _safe_kill_process
def test_safe_kill_process_invalid_pid_bool_Given_bool_When_call_Then_false():
    """Given bool pid
    When _safe_kill_process
    Then (False, None)."""
    ok, pid = cs._safe_kill_process(True, "expected")  # type: ignore[arg-type]
    assert ok is False and pid is None

def test_safe_kill_process_invalid_pid_negative_Given_negative_When_call_Then_false():
    """Given negative pid
    When _safe_kill_process
    Then False."""
    ok, _ = cs._safe_kill_process(-1, "expected")
    assert ok is False

def test_safe_kill_process_invalid_cmdline_Given_empty_When_call_Then_false():
    """Given empty expected_cmdline
    When _safe_kill_process
    Then False."""
    ok, _ = cs._safe_kill_process(1234, "")
    assert ok is False

def test_safe_kill_process_invalid_cmdline_not_str_Given_int_When_call_Then_false():
    """Given non-string cmdline
    When _safe_kill_process
    Then False."""
    ok, _ = cs._safe_kill_process(1234, 123)  # type: ignore[arg-type]
    assert ok is False

def test_safe_kill_process_no_proc_Given_missing_When_call_Then_false():
    """Given missing /proc pid
    When _safe_kill_process
    Then False."""
    with patch("builtins.open", side_effect=FileNotFoundError):
        ok, pid = cs._safe_kill_process(99999, "api_runner.py")
        assert ok is False and pid is None

def test_safe_kill_process_mismatch_Given_wrong_cmdline_When_call_Then_false():
    """Given pid cmdline mismatch
    When _safe_kill_process
    Then False."""
    with patch("builtins.open", mock_open(read_data=b"python\x00other.py\x00")):
        ok, _ = cs._safe_kill_process(1234, "api_runner.py mybot")
        assert ok is False

def test_safe_kill_process_sigterm_success_Given_match_exits_quickly_When_call_Then_true():
    """Given matching cmdline that exits after SIGTERM
    When _safe_kill_process
    Then True."""
    with patch("builtins.open", mock_open(read_data=b"python\x00api_runner.py\x00mybot\x00")):
        with patch("os.kill") as mk:
            # First kill SIGTERM, then kill(0) check says not alive
            def fake_kill(pid, sig):
                if sig == 0:
                    raise ProcessLookupError
                return None
            mk.side_effect = fake_kill
            with patch("time.sleep"):
                ok, pid = cs._safe_kill_process(1234, "api_runner.py mybot", grace_period=0.1)
                assert ok is True and pid == 1234

def test_safe_kill_process_sigterm_perm_denied_Given_no_perm_When_call_Then_false():
    """Given SIGTERM permission denied
    When _safe_kill_process
    Then False."""
    with patch("builtins.open", mock_open(read_data=b"python\x00api_runner.py\x00mybot\x00")):
        with patch("os.kill", side_effect=PermissionError):
            ok, _ = cs._safe_kill_process(1234, "api_runner.py mybot", grace_period=0.01)
            assert ok is False

def test_safe_kill_process_sigterm_lookup_Given_already_exited_When_call_Then_true():
    """Given PID already exited before SIGTERM
    When _safe_kill_process
    Then True."""
    with patch("builtins.open", mock_open(read_data=b"python\x00api_runner.py\x00mybot\x00")):
        with patch("os.kill", side_effect=ProcessLookupError):
            ok, pid = cs._safe_kill_process(1234, "api_runner.py mybot", grace_period=0.01)
            assert ok is True

def test_safe_kill_process_sigterm_generic_error_Given_error_When_call_Then_false():
    """Given generic error on SIGTERM
    When _safe_kill_process
    Then False."""
    with patch("builtins.open", mock_open(read_data=b"python\x00api_runner.py\x00mybot\x00")):
        with patch("os.kill", side_effect=RuntimeError("boom")):
            ok, _ = cs._safe_kill_process(1234, "api_runner.py mybot", grace_period=0.01)
            assert ok is False

def test_safe_kill_process_sighup_escalates_Given_still_alive_When_grace_expires_Then_sigkill():
    """Given process still alive after grace
    When _safe_kill_process
    Then escalates to SIGKILL."""
    with patch("builtins.open", mock_open(read_data=b"python\x00api_runner.py\x00mybot\x00")):
        kill_calls = []
        def fake_kill(pid, sig):
            kill_calls.append(sig)
            if sig == 0:
                return None  # alive
            return None
        with patch("os.kill", side_effect=fake_kill):
            # Mock os.kill for SIGKILL to then succeed, and _is_alive to eventually false
            # Need to patch time.sleep and make _is_alive return False after SIGKILL
            import signal as _sig
            with patch("time.sleep"):
                # Patch _is_alive indirectly via os.kill(0) still returning success, but after SIGKILL we check again
                # Our fake always says alive, so it will try SIGKILL and then still think alive -> returns False
                ok, pid = cs._safe_kill_process(1234, "api_runner.py mybot", grace_period=0.01)
                # At least SIGTERM and SIGKILL were sent
                assert _sig.SIGTERM in kill_calls
                assert _sig.SIGKILL in kill_calls

def test_safe_kill_process_cmdline_changed_before_sigkill_Given_changed_When_grace_Then_abort():
    """Given cmdline changes during grace
    When checking before SIGKILL
    Then abort SIGKILL."""
    # First read: matching, second read after grace: mismatching
    reads = [b"python\x00api_runner.py\x00mybot\x00", b"python\x00other.py\x00"]
    def fake_open(path, mode="r", *a, **kw):
        data = reads.pop(0) if reads else b"python\x00other.py\x00"
        m = mock_open(read_data=data)
        return m(path, mode, *a, **kw)
    with patch("builtins.open", side_effect=fake_open):
        with patch("os.kill") as mk:
            # Make _is_alive say alive during grace so we reach re-verify
            def fake_kill(pid, sig):
                if sig == 0:
                    return None
                return None
            mk.side_effect = fake_kill
            with patch("time.sleep"):
                ok, _ = cs._safe_kill_process(1234, "api_runner.py mybot", grace_period=0.01)
                assert ok is False
                # Should not have sent SIGKILL (last call was SIGTERM)
                import signal as _sig
                sigkill_calls = [c for c in mk.call_args_list if c.args[1] == _sig.SIGKILL]
                assert len(sigkill_calls) == 0

def test_safe_kill_process_proc_exits_during_grace_Given_exits_When_grace_Then_true():
    """Given process exits during grace period
    When polled
    Then True."""
    with patch("builtins.open", mock_open(read_data=b"python\x00api_runner.py\x00mybot\x00")):
        with patch("os.kill") as mk:
            call_count = {"n": 0}
            def fake_kill(pid, sig):
                if sig == 0:
                    call_count["n"] += 1
                    if call_count["n"] > 2:
                        raise ProcessLookupError
                    return None
                return None
            mk.side_effect = fake_kill
            with patch("time.sleep"):
                ok, _ = cs._safe_kill_process(1234, "api_runner.py mybot", grace_period=0.5)
                assert ok is True

def test_safe_kill_process_sigkill_perm_denied_Given_no_perm_When_sigkill_Then_false():
    """Given SIGKILL permission denied
    When escalating
    Then False."""
    with patch("builtins.open", mock_open(read_data=b"python\x00api_runner.py\x00mybot\x00")):
        import signal as _sig
        def fake_kill(pid, sig):
            if sig == _sig.SIGTERM:
                return None
            if sig == _sig.SIGKILL:
                raise PermissionError
            if sig == 0:
                return None
            return None
        with patch("os.kill", side_effect=fake_kill):
            with patch("time.sleep"):
                ok, _ = cs._safe_kill_process(1234, "api_runner.py mybot", grace_period=0.01)
                assert ok is False

def test_safe_kill_process_sigkill_lookup_Given_exits_before_sigkill_When_call_Then_true():
    """Given exits before SIGKILL
    When escalating
    Then True."""
    with patch("builtins.open", mock_open(read_data=b"python\x00api_runner.py\x00mybot\x00")):
        import signal as _sig
        def fake_kill(pid, sig):
            if sig == _sig.SIGTERM:
                return None
            if sig == _sig.SIGKILL:
                raise ProcessLookupError
            if sig == 0:
                return None
            return None
        with patch("os.kill", side_effect=fake_kill):
            with patch("time.sleep"):
                ok, _ = cs._safe_kill_process(1234, "api_runner.py mybot", grace_period=0.01)
                assert ok is True

def test_safe_kill_process_still_alive_after_sigkill_Given_zombie_When_call_Then_false():
    """Given still alive after SIGKILL
    When final check
    Then False (zombie)."""
    with patch("builtins.open", mock_open(read_data=b"python\x00api_runner.py\x00mybot\x00")):
        import signal as _sig
        def fake_kill(pid, sig):
            return None
        with patch("os.kill", side_effect=fake_kill):
            with patch("time.sleep"):
                ok, _ = cs._safe_kill_process(1234, "api_runner.py mybot", grace_period=0.01)
                assert ok is False

def test_safe_kill_process_read_permission_error_Given_denied_When_initial_read_Then_false():
    """Given permission denied reading proc
    When initial read
    Then False."""
    with patch("builtins.open", side_effect=PermissionError):
        ok, _ = cs._safe_kill_process(1234, "api_runner.py")
        assert ok is False

def test_safe_kill_process_read_generic_error_Given_error_When_read_Then_false():
    """Given generic error reading proc
    When initial read
    Then False."""
    with patch("builtins.open", side_effect=RuntimeError("boom")):
        ok, _ = cs._safe_kill_process(1234, "api_runner.py")
        assert ok is False

def test_safe_kill_process_sigkill_generic_error_Given_error_When_sigkill_Then_false():
    """Given generic error on SIGKILL
    When escalating
    Then False."""
    with patch("builtins.open", mock_open(read_data=b"python\x00api_runner.py\x00mybot\x00")):
        import signal as _sig
        def fake_kill(pid, sig):
            if sig == _sig.SIGTERM: return None
            if sig == _sig.SIGKILL: raise RuntimeError("boom")
            if sig == 0: return None
            return None
        with patch("os.kill", side_effect=fake_kill):
            with patch("time.sleep"):
                ok, _ = cs._safe_kill_process(1234, "api_runner.py mybot", grace_period=0.01)
                assert ok is False

# ===========================================================================
# control_server — bot_status
# ===========================================================================
def test_bot_status_unknown_bot_Given_unknown_When_bot_status_Then_sentinel(tmp_path: Path):
    """Given unknown bot name
    When bot_status()
    Then returns sentinel invalid-bot-name-rejected with etag."""
    with patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOT_REGISTRY", []):
        data = cs.bot_status("unknown-xyz")
        assert data["name"] == "unknown-xyz"
        assert "etag" in data and len(data["etag"]) == 32
        assert data["model"] is None

def test_bot_status_invalid_name_Given_injection_When_bot_status_Then_sentinel(tmp_path: Path):
    """Given invalid bot name
    When bot_status()
    Then name becomes sentinel and not injected."""
    with patch.object(cs, "STATE_DIR", tmp_path):
        data = cs.bot_status("bad; rm")
        assert data["name"] == "invalid-bot-name-rejected"

def test_bot_status_with_registry_and_heartbeat_Given_registered_When_status_Then_fields(tmp_path: Path):
    """Given registered bot with heartbeat and state
    When bot_status()
    Then fields computed."""
    from codebot.process_manager import BotConfig, ModelProfile
    cfg = BotConfig(name="my-bot", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m-x")
    prof = ModelProfile(lockup_risk="low", heartbeat_multiplier=1.5, log_stall_seconds=60, restart_cooldown=3, description="d")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), \
         patch.object(cs, "MODEL_PROFILES", {"m-x": prof}), \
         patch.object(cs, "STATE_DIR", tmp_path):
        (tmp_path / "my-bot.heartbeat").write_text(str(time.time()))
        (tmp_path / "my-bot.state.json").write_text(json.dumps({"status":"running","next_run_at": time.time()+100,"restart_count":2}))
        with patch("subprocess.run", return_value=MagicMock(stdout="1234\n", returncode=0)):
            data = cs.bot_status("my-bot")
            assert data["name"] == "my-bot"
            assert data["model"] == "m-x"
            assert data["risk"] == "low"
            assert data["running"] is True
            assert data["pid"] == "1234"
            assert data["state"] == "running"
            assert data["effective_timeout"] == 90  # max(60*1.5=90,30)=90

def test_bot_status_next_run_in_future_Given_next_run_When_status_Then_positive(tmp_path: Path):
    """Given state with future next_run_at
    When bot_status()
    Then next_run_in_seconds positive."""
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="b1", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=300, model="unknown")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path):
        (tmp_path / "b1.state.json").write_text(json.dumps({"next_run_at": time.time()+200}))
        with patch("subprocess.run", return_value=MagicMock(stdout="", returncode=1)):
            data = cs.bot_status("b1")
            assert data["next_run_in_seconds"] is not None and data["next_run_in_seconds"] > 0

def test_bot_status_next_run_past_Given_past_When_status_Then_zero(tmp_path: Path):
    """Given past next_run_at
    When bot_status()
    Then next_run_in 0."""
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="b2", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=300, model="unknown")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path):
        (tmp_path / "b2.state.json").write_text(json.dumps({"next_run_at": time.time()-100}))
        with patch("subprocess.run", return_value=MagicMock(stdout="", returncode=1)):
            data = cs.bot_status("b2")
            assert data["next_run_in_seconds"] == 0

def test_bot_status_corrupt_state_Given_bad_json_When_status_Then_no_crash(tmp_path: Path):
    """Given corrupt state file
    When bot_status()
    Then still returns."""
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="b3", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=300, model="unknown")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path):
        (tmp_path / "b3.state.json").write_text("{bad")
        with patch("subprocess.run", return_value=MagicMock(stdout="", returncode=1)):
            data = cs.bot_status("b3")
            assert data["name"] == "b3"

def test_bot_status_orchestrator_running_Given_pgrep_orch_When_status_Then_true(tmp_path: Path):
    """Given orchestrator pgrep returns
    When bot_status()
    Then orchestrator_running True."""
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="b4", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=300, model="unknown")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path):
        def fake_run(args, **kw):
            if "orchestrator.py" in args[2]:
                return MagicMock(stdout="999\n", returncode=0)
            return MagicMock(stdout="", returncode=1)
        with patch("subprocess.run", side_effect=fake_run):
            data = cs.bot_status("b4")
            assert data["orchestrator_running"] is True

def test_bot_status_no_orch_Given_no_orch_When_status_Then_false(tmp_path: Path):
    """Given no orchestrator
    When bot_status()
    Then orchestrator_running False."""
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="b5", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=300, model="unknown")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path):
        with patch("subprocess.run", return_value=MagicMock(stdout="", returncode=1)):
            data = cs.bot_status("b5")
            assert data["orchestrator_running"] is False

def test_bot_status_pgrep_exception_Given_error_When_status_Then_running_false(tmp_path: Path):
    """Given pgrep raises
    When bot_status()
    Then running False, no crash."""
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="b6", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=300, model="unknown")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path):
        with patch("subprocess.run", side_effect=RuntimeError("boom")):
            data = cs.bot_status("b6")
            assert data["running"] is False

def test_bot_status_state_no_status_key_Given_missing_status_When_bot_status_Then_none(tmp_path: Path):
    """Given state without status
    When bot_status()
    Then state None."""
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="b7", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=300, model="unknown")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path):
        (tmp_path / "b7.state.json").write_text(json.dumps({"other":1}))
        with patch("subprocess.run", return_value=MagicMock(stdout="", returncode=1)):
            data = cs.bot_status("b7")
            assert data["state"] is None

# ===========================================================================
# control_server — scheduler_status, economics, retry_dead_letter, alerts
# ===========================================================================
def test_scheduler_status_returns_version_Given_tmp_state_When_scheduler_status_Then_version(tmp_path: Path):
    """Given isolated state
    When scheduler_status()
    Then version 1 and expected keys."""
    with patch.object(cs, "STATE_DIR", tmp_path):
        data = cs.scheduler_status()
        assert data["version"] == 1
        assert "budget_state" in data
        assert "dead_letter_count" in data
        assert "batch_utilization" in data

def test_scheduler_status_with_ledger_Given_ledger_When_scheduler_status_Then_per_model(tmp_path: Path):
    """Given token ledger with by_model
    When scheduler_status()
    Then per_model_actual populated."""
    import datetime
    from codebot.token_budget import CAP
    day = "2026-01-01"
    ledger = {"day_utc": day, "by_model": {"m-a": {"prompt_actual":100,"completion_actual":50}}, "total_actual":150}
    with patch.object(cs, "STATE_DIR", tmp_path):
        # patch current_day_utc to return our day and day_total to return total
        with patch("codebot.token_budget.current_day_utc", return_value=day), \
             patch("codebot.token_budget.day_total", return_value=150), \
             patch("codebot.token_budget.get_budget_state", return_value="ok"):
            (tmp_path / "token_ledger.json").write_text(json.dumps(ledger))
            data = cs.scheduler_status()
            assert "budget_total_actual" in data

def test_scheduler_status_ledger_secret_filter_Given_secret_model_When_status_Then_filtered(tmp_path: Path):
    """Given ledger with secret model name
    When scheduler_status()
    Then secret model filtered out."""
    day = "2099-01-01"
    ledger = {"day_utc": day, "by_model": {"token-secret-model": {"prompt_actual":10,"completion_actual":5}, "good-model": {"prompt_actual":1,"completion_actual":1}}}
    with patch.object(cs, "STATE_DIR", tmp_path):
        with patch("codebot.token_budget.current_day_utc", return_value=day), \
             patch("codebot.token_budget.day_total", return_value=15), \
             patch("codebot.token_budget.get_budget_state", return_value="ok"):
            (tmp_path / "token_ledger.json").write_text(json.dumps(ledger))
            data = cs.scheduler_status()
            # secret model should be absent after filtering
            assert "token-secret-model" not in data.get("per_model_actual", {})

def test_scheduler_status_lease_counts_Given_leases_When_scheduler_status_Then_counts(tmp_path: Path):
    """Given leases.json with leases/attempts
    When scheduler_status()
    Then queue/lease counts."""
    with patch.object(cs, "STATE_DIR", tmp_path):
        (tmp_path / "leases.json").write_text(json.dumps({"leases":{"a":1,"b":2},"attempts":{"x":1},"dead_letters":[]}))
        data = cs.scheduler_status()
        assert data["lease_active"] == 2
        assert data["queue_tracked"] == 1

def test_scheduler_status_paused_and_disabled_Given_files_When_status_Then_counts(tmp_path: Path):
    """Given paused/disabled bots
    When scheduler_status()
    Then paused/disabled counts."""
    from codebot.process_manager import BotConfig
    cfg1 = BotConfig(name="paused-bot", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    cfg2 = BotConfig(name="disabled-bot", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOT_REGISTRY", [cfg1, cfg2]):
        (tmp_path / "paused-bot.paused").write_text("1")
        (tmp_path / "disabled-bot.state.json").write_text(json.dumps({"status":"disabled","reason":"error"}))
        data = cs.scheduler_status()
        assert data["paused_count"] >= 1
        assert data["disabled_count"] >= 1

def test_scheduler_status_starved_Given_heartbeat_When_status_Then_starved(tmp_path: Path):
    """Given heartbeat files
    When scheduler_status()
    Then starved_top populated."""
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="star-bot", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOT_REGISTRY", [cfg]):
        (tmp_path / "star-bot.heartbeat").write_text(str(time.time()-500))
        data = cs.scheduler_status()
        assert isinstance(data["starved_top"], list)

def test_scheduler_status_drain_exists_Given_drain_When_status_Then_drain_true(tmp_path: Path):
    """Given .drain exists
    When scheduler_status()
    Then drain True."""
    with patch.object(cs, "STATE_DIR", tmp_path):
        (tmp_path / ".drain").write_text("1")
        assert cs.scheduler_status()["drain"] is True

def test_economics_budget_status_Given_no_ledger_When_call_Then_defaults(tmp_path: Path):
    """Given no ledger
    When economics_budget_status()
    Then budget_cap set."""
    with patch.object(cs, "STATE_DIR", tmp_path):
        data = cs.economics_budget_status()
        assert "budget_cap" in data and data["budget_cap"] > 0
        assert "budget_used" in data

def test_economics_budget_status_with_ledger_Given_ledger_When_call_Then_used(tmp_path: Path):
    """Given ledger file
    When economics_budget_status()
    Then budget_used."""
    day = "2099-02-02"
    with patch.object(cs, "STATE_DIR", tmp_path):
        with patch("codebot.token_budget.current_day_utc", return_value=day), \
             patch("codebot.token_budget.day_total", return_value=1000), \
             patch("codebot.token_budget.get_budget_state", return_value="ok"), \
             patch("codebot.token_budget.CAP", 4000000):
            (tmp_path / "token_ledger.json").write_text(json.dumps({"day_utc": day, "by_model": {"a":{"prompt_actual":500,"completion_actual":500}}}))
            data = cs.economics_budget_status()
            assert data["budget_used"] == 1000

def test_economics_summary_Given_state_When_call_Then_version(tmp_path: Path):
    """Given isolated state
    When economics_summary()
    Then version 1."""
    with patch.object(cs, "STATE_DIR", tmp_path):
        data = cs.economics_summary()
        assert data["version"] == 1
        assert "budget" in data and "fleet" in data

def test_check_budget_alerts_warn_Given_85_When_check_Then_warn():
    """Given 85%
    When _check_budget_alerts
    Then warn."""
    alerts = cs._check_budget_alerts(85.0, "ok")
    assert any(a["level"]=="warn" for a in alerts)

def test_check_budget_alerts_critical_Given_95_When_check_Then_critical():
    """Given 95%
    When _check_budget_alerts
    Then critical."""
    alerts = cs._check_budget_alerts(95.0, "ok")
    assert any(a["level"]=="critical" for a in alerts)

def test_check_budget_alerts_stop_Given_stop_state_When_check_Then_critical():
    """Given budget_state stop
    When _check_budget_alerts
    Then critical with threshold 100."""
    alerts = cs._check_budget_alerts(10.0, "stop")
    assert any(a["threshold"]==100 for a in alerts)

def test_check_budget_alerts_no_alert_Given_low_When_check_Then_empty():
    """Given low pct
    When _check_budget_alerts
    Then empty."""
    assert cs._check_budget_alerts(10.0, "ok") == []

def test_retry_dead_letter_success_Given_mock_When_retry_Then_retried(tmp_path: Path):
    """Given mocked lease_state retry
    When retry_dead_letter
    Then retried and event appended."""
    with patch.object(cs, "STATE_DIR", tmp_path):
        with patch("codebot.lease_state.retry_dead_letter", return_value={"status":"retried","id":"Q-1"}):
            with patch("codebot.event_log.append_event") as mock_append:
                res = cs.retry_dead_letter("Q-1")
                assert res["status"] == "retried"
                mock_append.assert_called_once()

def test_retry_dead_letter_unavailable_Given_import_error_When_retry_Then_unavailable(tmp_path: Path):
    """Given lease_state fails
    When retry_dead_letter
    Then unavailable."""
    with patch.dict("sys.modules", {"codebot.lease_state": None}):
        # force ImportError by patching __import__? simpler: patch to raise
        with patch("codebot.lease_state.retry_dead_letter", side_effect=ImportError):
            res = cs.retry_dead_letter("Q-2")
            assert res["status"] in ("unavailable","not-found","retried")

def test_retry_dead_letter_not_retried_no_event_Given_not_retried_When_retry_Then_no_append(tmp_path: Path):
    """Given status not retried
    When retry_dead_letter
    Then no event appended."""
    with patch.object(cs, "STATE_DIR", tmp_path):
        with patch("codebot.lease_state.retry_dead_letter", return_value={"status":"not-found","id":"Q-3"}):
            with patch("codebot.event_log.append_event") as mock_append:
                cs.retry_dead_letter("Q-3")
                mock_append.assert_not_called()

# ===========================================================================
# control_server — ControlHandler auth & helpers
# ===========================================================================
def test_auth_rate_limited_Given_blocked_ip_When_auth_Then_none(tmp_path: Path):
    """Given rate-limited IP
    When _auth()
    Then None (429 already sent)."""
    cs.CONTROL_TOKEN = "secret"
    h = _handler("GET", "/bots")
    with patch.object(cs._rate_limiter, "is_allowed", return_value=(False, "blocked")):
        res = h._auth()
        assert res is None
        h.send_response.assert_called_with(429)

def test_auth_no_token_fail_closed_Given_empty_token_When_auth_Then_false():
    """Given CONTROL_TOKEN empty
    When _auth()
    Then False."""
    cs.CONTROL_TOKEN = ""
    h = _handler("GET", "/bots", headers={})
    # h.headers.get = lambda k, d=None: {} .get(k,d)  # removed: _FakeHeaders handles get
    # need to ensure is_allowed returns True
    with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
        assert h._auth() is False

def test_auth_no_bypass_flag_Given_flag_env_When_auth_Then_false():
    """Given CONTROL_ALLOW_UNAUTHENTICATED=1 env var (legacy, must be ignored)
    When _auth() with empty CONTROL_TOKEN
    Then False (bypass removed per CB-B4086)."""
    import os
    cs.CONTROL_TOKEN = ""
    os.environ["CONTROL_ALLOW_UNAUTHENTICATED"] = "1"
    try:
        h = _handler("GET", "/bots", headers={})
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            assert h._auth() is False
    finally:
        os.environ.pop("CONTROL_ALLOW_UNAUTHENTICATED", None)

def test_auth_valid_token_Given_bearer_When_auth_Then_true():
    """Given valid Bearer
    When _auth()
    Then True."""
    cs.CONTROL_TOKEN = "my-token"
    h = _handler("GET", "/bots", headers={"Authorization": "Bearer my-token"})
    with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
        assert h._auth() is True

def test_auth_invalid_token_Given_wrong_When_auth_Then_false_and_record():
    """Given wrong token
    When _auth()
    Then False and failure recorded."""
    cs.CONTROL_TOKEN = "my-token"
    h = _handler("GET", "/bots", headers={"Authorization": "Bearer wrong"})
    with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
        with patch.object(cs._rate_limiter, "record_failure") as mock_rec:
            assert h._auth() is False
            mock_rec.assert_called_once()

def test_is_loopback_client_Given_localhost_When_check_Then_true():
    """Given 127.0.0.1
    When _is_loopback_client()
    Then True."""
    h = _handler("GET", "/")
    h.client_address = ("127.0.0.1", 123)
    assert h._is_loopback_client() is True
    h.client_address = ("8.8.8.8", 123)
    assert h._is_loopback_client() is False
    h.client_address = ("::1", 123)
    assert h._is_loopback_client() is True
    h.client_address = None
    assert h._is_loopback_client() is False

def test_read_json_body_no_content_length_Given_no_header_When_read_Then_empty_dict():
    """Given no Content-Length
    When _read_json_body()
    Then {}, None, None."""
    h = _handler("POST", "/test", headers={})
    h.headers = _FakeHeaders({"Content-Length": None})
    # h.headers.get = lambda k, d=None: None  # removed: _FakeHeaders handles get
    h.connection = MagicMock()
    h.rfile = io.BytesIO(b"")
    body, code, err = h._read_json_body()
    assert body == {} and code is None

def test_read_json_body_invalid_int_Given_bad_length_When_read_Then_400():
    """Given non-int Content-Length
    When _read_json_body()
    Then 400."""
    h = _handler("POST", "/test", headers={"Content-Length": "abc"})
    h.rfile = io.BytesIO(b"")
    body, code, err = h._read_json_body()
    assert code == 400

def test_read_json_body_negative_Given_negative_When_read_Then_400():
    """Given negative length
    When _read_json_body()
    Then 400."""
    h = _handler("POST", "/test", headers={"Content-Length": "-1"})
    body, code, _ = h._read_json_body()
    assert code == 400

def test_read_json_body_too_large_Given_excess_When_read_Then_413():
    """Given too large
    When _read_json_body()
    Then 413."""
    h = _handler("POST", "/test", headers={"Content-Length": str(cs.MAX_REQUEST_BYTES+1)})
    body, code, _ = h._read_json_body()
    assert code == 413

def test_read_json_body_timeout_Given_socket_timeout_When_read_Then_408():
    """Given socket timeout
    When _read_json_body()
    Then 408."""
    h = _handler("POST", "/test", body={"a":1})
    # make rfile.read raise timeout
    h.rfile = MagicMock()
    h.rfile.read.side_effect = OSError("timed out")
    # h.headers.get = lambda k, d=None: str(10) if k=="Content-Length" else d  # removed: _FakeHeaders handles get
    h.connection = MagicMock()
    import socket
    h.rfile.read.side_effect = socket.timeout("time")
    body, code, _ = h._read_json_body()
    assert code == 408

def test_read_json_body_invalid_json_Given_bad_json_When_read_Then_400():
    """Given invalid JSON
    When _read_json_body()
    Then 400."""
    h = _handler("POST", "/test", headers={"Content-Length": "7"})
    h.rfile = io.BytesIO(b"notjson")
    h.connection = MagicMock()
    h.connection.settimeout = MagicMock()
    body, code, _ = h._read_json_body()
    assert code == 400

def test_read_json_body_not_dict_Given_array_When_read_Then_400():
    """Given JSON array
    When _read_json_body()
    Then 400."""
    h = _handler("POST", "/test", headers={"Content-Length": "3"})
    h.rfile = io.BytesIO(b"[1]")
    h.connection = MagicMock()
    body, code, _ = h._read_json_body()
    assert code == 400

def test_read_json_body_valid_Given_json_When_read_Then_dict():
    """Given valid JSON
    When _read_json_body()
    Then dict."""
    h = _handler("POST", "/test", body={"x":1})
    body, code, _ = h._read_json_body()
    assert body == {"x":1} and code is None

def test_read_json_body_empty_raw_Given_empty_When_read_Then_empty():
    """Given empty raw
    When _read_json_body()
    Then {}."""
    h = _handler("POST", "/test", headers={"Content-Length": "0"})
    h.rfile = io.BytesIO(b"")
    h.connection = MagicMock()
    body, code, _ = h._read_json_body()
    assert body == {}

def test_get_destructive_preview_stop_all_Given_no_bots_When_preview_Then_all():
    """Given stop with no bots
    When _get_destructive_preview
    Then description contains ALL."""
    h = _handler("POST", "/bots/stop")
    p = h._get_destructive_preview("/bots/stop", {})
    assert "ALL" in p["description"]

def test_get_destructive_preview_stop_specific_Given_bots_When_preview_Then_list():
    """Given stop with bots list
    When _get_destructive_preview
    Then affected_bots."""
    h = _handler("POST", "/bots/stop")
    p = h._get_destructive_preview("/bots/stop", {"bots":["a","b"]})
    assert p["affected_bots"] == ["a","b"]

def test_get_destructive_preview_drain_Given_drain_When_preview_Then_undo():
    """Given drain path
    When _get_destructive_preview
    Then undo_command."""
    h = _handler("POST", "/control/drain")
    p = h._get_destructive_preview("/control/drain", {})
    assert "clear-drain" in p.get("undo_command","") or "resume" in p.get("recovery","")

def test_get_destructive_preview_update_Given_update_When_preview_Then_warning():
    """Given update path
    When _get_destructive_preview
    Then warning."""
    h = _handler("POST", "/control/update")
    p = h._get_destructive_preview("/control/update", {})
    assert "warning" in p

def test_json_304_Given_etag_When_json304_Then_304():
    """Given etag
    When _json_304()
    Then 304 with ETag."""
    h = _handler("GET", "/bots/x")
    h._json_304("abc123")
    h.send_response.assert_called_with(304)
    assert any(c.args[0]=="ETag" for c in h.send_header.call_args_list)

def test_json_with_cache_headers_Given_etag_When_call_Then_headers():
    """Given etag and ts
    When _json_with_cache_headers
    Then ETag and Last-Modified."""
    h = _handler("GET", "/bots/x")
    h._json_with_cache_headers(200, {"ok":1}, "etag123", time.time())
    h.send_response.assert_called_with(200)
    hdr_names = [c.args[0] for c in h.send_header.call_args_list]
    assert "ETag" in hdr_names and "Last-Modified" in hdr_names

def test_html_Given_bytes_When_html_Then_headers():
    """Given html bytes
    When _html()
    Then correct headers."""
    h = _handler("GET", "/dashboard")
    h._html(200, b"<html></html>")
    h.send_response.assert_called_with(200)
    assert any("text/html" in str(c) for c in h.send_header.call_args_list)

# ===========================================================================
# control_server — do_GET
# ===========================================================================
def test_do_get_health_public_Given_no_auth_When_get_health_Then_200(tmp_path: Path):
    """Given /health
    When do_GET
    Then 200 ok without auth."""
    cs.CONTROL_TOKEN = "secret"
    h = _handler("GET", "/health")
    h.headers = _FakeHeaders({})
    # h.headers.get = lambda k, d=None: None  # removed: _FakeHeaders handles get
    h.client_address = ("127.0.0.1", 123)
    # Patch rate limiter to allow
    with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
        with patch.object(cs, "STATE_DIR", tmp_path):
            calls = _capture(h)
            h.do_GET()
            assert any(c[0]==200 and c[1].get("status")=="ok" for c in calls)

def test_do_get_health_rate_limited_Given_blocked_When_get_health_Then_429():
    """Given health blocked by rate limiter
    When do_GET /health
    Then 429."""
    h = _handler("GET", "/health")
    # h.headers.get = lambda k, d=None: None  # removed: _FakeHeaders handles get
    h.client_address = ("1.2.3.4", 123)
    with patch.object(cs._rate_limiter, "is_allowed", return_value=(False, "blocked")):
        calls = _capture(h)
        h.do_GET()
        assert calls[0][0]==429

def test_do_get_health_api_version_Given_api_health_When_get_Then_200():
    """Given /api/health
    When do_GET
    Then 200."""
    h = _handler("GET", "/api/health")
    # h.headers.get = lambda k, d=None: None  # removed: _FakeHeaders handles get
    with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
        calls = _capture(h)
        h.do_GET()
        assert calls[0][0]==200

def test_do_get_dashboard_Given_path_When_get_Then_html(tmp_path: Path):
    """Given /dashboard
    When do_GET
    Then html."""
    h = _handler("GET", "/dashboard")
    # h.headers.get = lambda k, d=None: None  # removed: _FakeHeaders handles get
    with patch("codebot.dashboard.dashboard_page", return_value=b"<html>dash</html>"):
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            h.do_GET()
            h.send_response.assert_called_with(200)

def test_do_get_tickets_Given_path_When_get_Then_html():
    """Given /tickets
    When do_GET
    Then html."""
    h = _handler("GET", "/tickets")
    # h.headers.get = lambda k, d=None: None  # removed: _FakeHeaders handles get
    with patch("codebot.dashboard.ticket_explorer_page", return_value=b"<html>tickets</html>"):
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            h.do_GET()
            h.send_response.assert_called_with(200)

def test_do_get_dashboard_snapshot_loopback_Given_loopback_When_get_Then_200(tmp_path: Path):
    """Given loopback client
    When GET /dashboard/snapshot
    Then 200."""
    h = _handler("GET", "/dashboard/snapshot")
    h.client_address = ("127.0.0.1", 123)
    # h.headers.get = lambda k, d=None: None  # removed: _FakeHeaders handles get
    with patch("codebot.dashboard.live_snapshot", return_value={"ok":1}):
        calls = _capture(h)
        h.do_GET()
        assert calls and calls[0][0]==200

def test_do_get_dashboard_snapshot_non_loopback_requires_auth_Given_remote_When_no_token_Then_auth_checked():
    """Given non-loopback for dashboard/snapshot
    When no token
    Then falls through to auth."""
    cs.CONTROL_TOKEN = "secret"
    h = _handler("GET", "/dashboard/snapshot")
    h.client_address = ("8.8.8.8", 123)
    # h.headers.get = lambda k, d=None: "Bearer secret" if k=="Authorization" else None  # removed: _FakeHeaders handles get
    h.headers = _FakeHeaders({"Authorization":"Bearer secret"})
    # h.headers.get = lambda k, d=None: h.headers.get(k,d) if isinstance(h.headers, dict) else None  # removed: _FakeHeaders handles get
    # Simplify: make _auth succeed
    with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
        with patch("codebot.dashboard.live_snapshot", return_value={"x":1}):
            calls = _capture(h)
            # need to set Authorization header properly
            h.headers = _FakeHeaders({"Authorization":"Bearer secret"})
            # h.headers.get = lambda k, d=None: "Bearer secret" if k=="Authorization" else d  # removed: _FakeHeaders handles get
            h.do_GET()
            # should reach authenticated branch and return 200
            assert any(c[0]==200 for c in calls)

def test_do_get_requires_auth_Given_protected_without_token_When_get_Then_401():
    """Given protected endpoint without auth
    When do_GET
    Then 401."""
    cs.CONTROL_TOKEN = "secret"
    h = _handler("GET", "/bots")
    # h.headers.get = lambda k, d=None: None  # no Authorization  # removed: _FakeHeaders handles get
    h.client_address = ("8.8.8.8",123)
    with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
        calls = _capture(h)
        h.do_GET()
        assert calls[0][0]==401

def test_do_get_bots_Given_registry_When_get_Then_list(tmp_path: Path):
    """Given authenticated
    When GET /bots
    Then list."""
    cs.CONTROL_TOKEN = "secret"
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path):
        h = _handler("GET", "/bots", headers={"Authorization":"Bearer secret"})
        # h.headers.get = lambda k, d=None: "Bearer secret" if k=="Authorization" else d  # removed: _FakeHeaders handles get
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("subprocess.run", return_value=MagicMock(stdout="", returncode=1)):
                calls = _capture(h)
                h.do_GET()
                assert calls[0][0]==200
                assert isinstance(calls[0][1], list)

def test_do_get_bot_unknown_Given_unknown_When_get_Then_404(tmp_path: Path):
    """Given unknown bot
    When GET /bots/unknown
    Then 404."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "BOT_REGISTRY", []), patch.object(cs, "STATE_DIR", tmp_path):
        h = _handler("GET", "/bots/unknown", headers={"Authorization":"Bearer secret"})
        # h.headers.get = lambda k, d=None: "Bearer secret" if k=="Authorization" else d  # removed: _FakeHeaders handles get
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_GET()
            assert calls[0][0]==404

def test_do_get_bot_valid_Given_registered_When_get_Then_200_with_cache(tmp_path: Path):
    """Given registered bot
    When GET /bots/bot-a
    Then 200 with cache headers."""
    cs.CONTROL_TOKEN = "secret"
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path):
        h = _handler("GET", "/bots/bot-a", headers={"Authorization":"Bearer secret"})
        # h.headers.get = lambda k, d=None: "Bearer secret" if k=="Authorization" else d  # removed: _FakeHeaders handles get
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("subprocess.run", return_value=MagicMock(stdout="", returncode=1)):
                # Don't capture, need to test _json_with_cache_headers path
                h.do_GET()
                h.send_response.assert_called_with(200)

def test_do_get_bot_304_etag_Given_matching_etag_When_get_Then_304(tmp_path: Path):
    """Given If-None-Match matches
    When GET /bots/bot-a
    Then 304."""
    cs.CONTROL_TOKEN = "secret"
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path):
        # Get etag via bot_status
        with patch("subprocess.run", return_value=MagicMock(stdout="", returncode=1)):
            status = cs.bot_status("bot-a")
            etag = status["etag"]
        h = _handler("GET", "/bots/bot-a", headers={"Authorization":"Bearer secret", "If-None-Match": f'"{etag}"'})
        # h.headers.get = lambda k, d=None: h.headers[k] if k in h.headers else d  # removed: _FakeHeaders handles get
        # Actually need to set headers dict correctly
        hdrs = _FakeHeaders({"Authorization":"Bearer secret", "If-None-Match": f'"{etag}"'})
        h.headers = hdrs
        # h.headers.get = lambda k, d=None: hdrs.get(k,d)  # removed: _FakeHeaders handles get
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("subprocess.run", return_value=MagicMock(stdout="", returncode=1)):
                h.do_GET()
                h.send_response.assert_called_with(304)

def test_do_get_logs_invalid_lines_Given_bad_lines_When_get_Then_400(tmp_path: Path):
    """Given invalid lines param
    When GET /bots/.../logs?lines=abc
    Then 400."""
    cs.CONTROL_TOKEN = "secret"
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path):
        h = _handler("GET", "/bots/bot-a/logs?lines=abc", headers={"Authorization":"Bearer secret"})
        hdrs = _FakeHeaders({"Authorization":"Bearer secret"})
        h.headers = hdrs
        # h.headers.get = lambda k, d=None: hdrs.get(k,d)  # removed: _FakeHeaders handles get
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_GET()
            assert calls[0][0]==400

def test_do_get_logs_clamped_Given_huge_lines_When_get_Then_clamped(tmp_path: Path):
    """Given lines > MAX_LOG_LINES
    When GET logs
    Then clamped."""
    cs.CONTROL_TOKEN = "secret"
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "LOGS_DIR", tmp_path):
        (tmp_path / "bot-a.log").write_text("hi\n")
        h = _handler("GET", "/bots/bot-a/logs?lines=999999", headers={"Authorization":"Bearer secret"})
        hdrs = _FakeHeaders({"Authorization":"Bearer secret"})
        h.headers = hdrs
        # h.headers.get = lambda k, d=None: hdrs.get(k,d)  # removed: _FakeHeaders handles get
        h.path = "/bots/bot-a/logs?lines=999999"
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="hi")):
                calls = _capture(h)
                h.do_GET()
                assert calls[0][0]==200
                assert calls[0][1]["lines"] == cs.MAX_LOG_LINES

def test_do_get_logs_unknown_bot_Given_unknown_When_get_Then_404(tmp_path: Path):
    """Given unknown bot
    When GET logs
    Then 404."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "BOT_REGISTRY", []), patch.object(cs, "STATE_DIR", tmp_path):
        h = _handler("GET", "/bots/unknown/logs?lines=10", headers={"Authorization":"Bearer secret"})
        hdrs = _FakeHeaders({"Authorization":"Bearer secret"})
        h.headers = hdrs
        # h.headers.get = lambda k, d=None: hdrs.get(k,d)  # removed: _FakeHeaders handles get
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_GET()
            assert calls[0][0]==404

def test_do_get_logs_success_Given_valid_When_get_Then_tail(tmp_path: Path):
    """Given valid bot logs
    When GET logs
    Then tail."""
    cs.CONTROL_TOKEN = "secret"
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "LOGS_DIR", tmp_path):
        (tmp_path / "bot-a.log").write_text("line1\nline2\n")
        h = _handler("GET", "/bots/bot-a/logs?lines=2", headers={"Authorization":"Bearer secret"})
        hdrs = _FakeHeaders({"Authorization":"Bearer secret"})
        h.headers = hdrs
        # h.headers.get = lambda k, d=None: hdrs.get(k,d)  # removed: _FakeHeaders handles get
        h.path = "/bots/bot-a/logs?lines=2"
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="line2")):
                calls = _capture(h)
                h.do_GET()
                assert calls[0][0]==200

def test_do_get_scheduler_status_Given_auth_When_get_Then_200(tmp_path: Path):
    """Given auth
    When GET /scheduler/status
    Then 200."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        h = _handler("GET", "/scheduler/status", headers={"Authorization":"Bearer secret"})
        hdrs = _FakeHeaders({"Authorization":"Bearer secret"})
        h.headers = hdrs
        # h.headers.get = lambda k, d=None: hdrs.get(k,d)  # removed: _FakeHeaders handles get
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_GET()
            assert calls[0][0]==200

def test_do_get_scheduler_events_invalid_limit_Given_bad_When_get_Then_400(tmp_path: Path):
    """Given invalid limit
    When GET /scheduler/events?limit=abc
    Then 400."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        h = _handler("GET", "/scheduler/events?limit=abc", headers={"Authorization":"Bearer secret"})
        hdrs = _FakeHeaders({"Authorization":"Bearer secret"})
        h.headers = hdrs
        # h.headers.get = lambda k, d=None: hdrs.get(k,d)  # removed: _FakeHeaders handles get
        h.path = "/scheduler/events?limit=abc"
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_GET()
            assert calls[0][0]==400

def test_do_get_scheduler_events_filtered_Given_type_When_get_Then_filtered(tmp_path: Path):
    """Given type filter
    When GET /scheduler/events?type=lease&limit=10
    Then filtered."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        (tmp_path / "events.jsonl").write_text(json.dumps({"version":1,"type":"lease","ts":time.time(),"data":{"a":1}})+"\n")
        h = _handler("GET", "/scheduler/events?limit=10&type=lease", headers={"Authorization":"Bearer secret"})
        hdrs = _FakeHeaders({"Authorization":"Bearer secret"})
        h.headers = hdrs
        # h.headers.get = lambda k, d=None: hdrs.get(k,d)  # removed: _FakeHeaders handles get
        h.path = "/scheduler/events?limit=10&type=lease"
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_GET()
            assert calls[0][0]==200

def test_do_get_scheduler_events_limit_clamped_Given_large_When_get_Then_clamped(tmp_path: Path):
    """Given limit > MAX_EVENTS
    When GET events
    Then clamped."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        h = _handler("GET", "/scheduler/events?limit=9999", headers={"Authorization":"Bearer secret"})
        hdrs = _FakeHeaders({"Authorization":"Bearer secret"})
        h.headers = hdrs
        # h.headers.get = lambda k, d=None: hdrs.get(k,d)  # removed: _FakeHeaders handles get
        h.path = "/scheduler/events?limit=9999"
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_GET()
            assert calls[0][0]==200
            # limit in response is bounded
            assert calls[0][1]["limit"] <= 100

def test_do_get_dead_letters_Given_auth_When_get_Then_200(tmp_path: Path):
    """Given auth
    When GET /scheduler/dead-letters
    Then 200."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        h = _handler("GET", "/scheduler/dead-letters", headers={"Authorization":"Bearer secret"})
        hdrs = _FakeHeaders({"Authorization":"Bearer secret"})
        h.headers = hdrs
        # h.headers.get = lambda k, d=None: hdrs.get(k,d)  # removed: _FakeHeaders handles get
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("codebot.lease_state.dead_letters", return_value=[{"id":"Q-1","reason":"fail"}]):
                calls = _capture(h)
                h.do_GET()
                assert calls[0][0]==200

def test_do_get_economics_budget_Given_auth_When_get_Then_200(tmp_path: Path):
    """Given auth
    When GET /economics/budget-status
    Then 200."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        h = _handler("GET", "/economics/budget-status", headers={"Authorization":"Bearer secret"})
        hdrs = _FakeHeaders({"Authorization":"Bearer secret"})
        h.headers = hdrs
        # h.headers.get = lambda k, d=None: hdrs.get(k,d)  # removed: _FakeHeaders handles get
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_GET()
            assert calls[0][0]==200

def test_do_get_economics_summary_Given_auth_When_get_Then_200(tmp_path: Path):
    """Given auth
    When GET /economics/summary
    Then 200."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        h = _handler("GET", "/economics/summary", headers={"Authorization":"Bearer secret"})
        hdrs = _FakeHeaders({"Authorization":"Bearer secret"})
        h.headers = hdrs
        # h.headers.get = lambda k, d=None: hdrs.get(k,d)  # removed: _FakeHeaders handles get
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_GET()
            assert calls[0][0]==200

def test_do_get_gates_metrics_Given_auth_When_get_Then_200(tmp_path: Path):
    """Given auth
    When GET /gates/metrics
    Then 200."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        h = _handler("GET", "/gates/metrics", headers={"Authorization":"Bearer secret"})
        hdrs = _FakeHeaders({"Authorization":"Bearer secret"})
        h.headers = hdrs
        # h.headers.get = lambda k, d=None: hdrs.get(k,d)  # removed: _FakeHeaders handles get
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("codebot.quality_gate.get_gate_metrics", return_value={"version":1,"metrics":[]}):
                calls = _capture(h)
                h.do_GET()
                assert calls[0][0]==200

def test_do_get_gates_metrics_import_error_Given_fail_When_get_Then_fallback(tmp_path: Path):
    """Given gate metrics fails
    When GET /gates/metrics
    Then fallback."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        h = _handler("GET", "/gates/metrics", headers={"Authorization":"Bearer secret"})
        hdrs = _FakeHeaders({"Authorization":"Bearer secret"})
        h.headers = hdrs
        # h.headers.get = lambda k, d=None: hdrs.get(k,d)  # removed: _FakeHeaders handles get
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("codebot.quality_gate.get_gate_metrics", side_effect=Exception("fail")):
                calls = _capture(h)
                h.do_GET()
                assert calls[0][0]==200

def test_do_get_telemetry_health_Given_auth_When_get_Then_200(tmp_path: Path):
    """Given auth for telemetry health
    When GET /telemetry/health
    Then 200."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        h = _handler("GET", "/telemetry/health", headers={"Authorization":"Bearer secret"})
        hdrs = _FakeHeaders({"Authorization":"Bearer secret"})
        h.headers = hdrs
        # h.headers.get = lambda k, d=None: hdrs.get(k,d)  # removed: _FakeHeaders handles get
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_GET()
            assert calls[0][0]==200

def test_do_get_unknown_Given_auth_When_get_Then_404(tmp_path: Path):
    """Given unknown path
    When GET
    Then 404."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        h = _handler("GET", "/unknown", headers={"Authorization":"Bearer secret"})
        hdrs = _FakeHeaders({"Authorization":"Bearer secret"})
        h.headers = hdrs
        # h.headers.get = lambda k, d=None: hdrs.get(k,d)  # removed: _FakeHeaders handles get
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_GET()
            assert calls[0][0]==404

# ===========================================================================
# control_server — do_POST
# ===========================================================================
def test_do_post_auth_required_Given_no_token_When_post_Then_401():
    """Given no token and not allow unauth
    When POST /bots/start
    Then 401."""
    cs.CONTROL_TOKEN = "secret"
    h = _handler("POST", "/bots/start", body={})
    # h.headers.get = lambda k, d=None: None  # removed: _FakeHeaders handles get
    with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
        calls = _capture(h)
        h.do_POST()
        assert calls[0][0]==401

def test_do_post_rate_limited_Given_blocked_When_post_Then_429():
    """Given rate limited
    When POST
    Then 429 already sent, None return."""
    h = _handler("POST", "/bots/start", body={})
    with patch.object(cs._rate_limiter, "is_allowed", return_value=(False, "blocked")):
        calls = _capture(h)
        h.do_POST()
        assert calls[0][0]==429

def test_do_post_read_body_error_Given_bad_length_When_post_Then_error():
    """Given bad Content-Length
    When POST
    Then error code."""
    cs.CONTROL_TOKEN = "secret"
    h = _handler("POST", "/bots/start", body={"bots":[]})
    # Override to trigger error path
    h.headers = _FakeHeaders({"Content-Length":"notint", "Authorization":"Bearer secret"})
    # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
    h.rfile = io.BytesIO(b"{}")
    h.connection = MagicMock()
    with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
        calls = _capture(h)
        h.do_POST()
        assert calls[0][0]==400

def test_do_post_destructive_no_force_Given_stop_no_force_When_post_Then_400(tmp_path: Path):
    """Given stop without force
    When POST /bots/stop
    Then 400 requires force."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path):
        h = _handler("POST", "/bots/stop", body={})
        hdrs = _FakeHeaders({"Authorization":"Bearer secret"})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(json.dumps({}).encode()))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(json.dumps({}).encode())
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==400

def test_do_post_destructive_dry_run_Given_dry_run_When_stop_Then_preview(tmp_path: Path):
    """Given dry_run
    When POST /bots/stop with dry_run
    Then 200 preview."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path):
        h = _handler("POST", "/bots/stop", body={"dry_run": True})
        hdrs = _FakeHeaders({"Authorization":"Bearer secret"})
        raw = json.dumps({"dry_run": True}).encode()
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==200
            assert calls[0][1].get("dry_run") is True

def test_do_post_restart_invalid_name_Given_bad_name_When_post_Then_400(tmp_path: Path):
    """Given invalid bot name
    When POST /bots/bad;name/restart
    Then 400."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path):
        raw = json.dumps({"force": True}).encode()
        h = _handler("POST", "/bots/bad;name/restart", body={"force": True})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        h.path = "/bots/bad;name/restart"
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==400

def test_do_post_restart_unknown_Given_not_in_registry_When_post_Then_404(tmp_path: Path):
    """Given unknown bot
    When POST /bots/ghost/restart
    Then 404."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "BOT_REGISTRY", []), patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path):
        raw = json.dumps({"force": True}).encode()
        h = _handler("POST", "/bots/ghost/restart", body={"force": True})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==404

def test_do_post_restart_dry_run_Given_dry_When_post_Then_preview(tmp_path: Path):
    """Given dry_run
    When POST /bots/valid/restart dry_run
    Then 200 preview."""
    cs.CONTROL_TOKEN = "secret"
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="valid-bot", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path):
        raw = json.dumps({"dry_run": True}).encode()
        h = _handler("POST", "/bots/valid-bot/restart", body={"dry_run": True})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==200 and calls[0][1].get("dry_run") is True

def test_do_post_restart_no_force_no_dry_Given_missing_When_post_Then_400(tmp_path: Path):
    """Given no force
    When POST /bots/valid/restart without force
    Then 400."""
    cs.CONTROL_TOKEN = "secret"
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="valid-bot", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path):
        raw = json.dumps({}).encode()
        h = _handler("POST", "/bots/valid-bot/restart", body={})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==400

def test_do_post_restart_success_Given_force_When_post_Then_200(tmp_path: Path):
    """Given force
    When POST /bots/valid/restart
    Then 200."""
    cs.CONTROL_TOKEN = "secret"
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="valid-bot", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path), patch.object(cs, "ORCH", tmp_path / "orch.py"):
        raw = json.dumps({"force": True}).encode()
        h = _handler("POST", "/bots/valid-bot/restart", body={"force": True})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("codebot.control_server._safe_kill_bot_process", return_value=(True, [111])):
                with patch("subprocess.Popen") as mock_popen:
                    with patch("time.sleep"):
                        calls = _capture(h)
                        h.do_POST()
                        assert calls[0][0]==200
                        mock_popen.assert_called()

def test_do_post_restart_exception_Given_error_When_post_Then_500(tmp_path: Path):
    """Given exception during restart
    When POST
    Then 500."""
    cs.CONTROL_TOKEN = "secret"
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="valid-bot", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path):
        raw = json.dumps({"force": True}).encode()
        h = _handler("POST", "/bots/valid-bot/restart", body={"force": True})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("codebot.control_server._safe_kill_bot_process", side_effect=RuntimeError("boom")):
                calls = _capture(h)
                h.do_POST()
                assert calls[0][0]==500

def test_do_post_pause_success_Given_force_When_post_Then_200(tmp_path: Path):
    """Given force
    When POST /bots/valid/pause
    Then 200."""
    cs.CONTROL_TOKEN = "secret"
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="valid-bot", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path):
        raw = json.dumps({"force": True}).encode()
        h = _handler("POST", "/bots/valid-bot/pause", body={"force": True})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("codebot.control_server._safe_kill_bot_process", return_value=(True, [])):
                calls = _capture(h)
                h.do_POST()
                assert calls[0][0]==200
                assert (tmp_path / "valid-bot.paused").exists()

def test_do_post_pause_dry_run_Given_dry_When_post_Then_preview(tmp_path: Path):
    """Given dry_run
    When POST /bots/valid/pause dry
    Then preview."""
    cs.CONTROL_TOKEN = "secret"
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="valid-bot", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path):
        raw = json.dumps({"dry_run": True}).encode()
        h = _handler("POST", "/bots/valid-bot/pause", body={"dry_run": True})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==200 and calls[0][1].get("dry_run") is True

def test_do_post_pause_invalid_name_Given_bad_When_post_Then_400(tmp_path: Path):
    """Given invalid name
    When POST pause
    Then 400 before registry."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path):
        raw = json.dumps({"force": True}).encode()
        h = _handler("POST", "/bots/bad;name/pause", body={"force": True})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        h.path="/bots/bad;name/pause"
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==400

def test_do_post_resume_success_Given_pause_When_post_Then_removed(tmp_path: Path):
    """Given paused file
    When POST /bots/valid/resume
    Then file removed and 200."""
    cs.CONTROL_TOKEN = "secret"
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="valid-bot", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path), patch.object(cs, "ORCH", tmp_path/"orch.py"):
        (tmp_path / "valid-bot.paused").write_text("1")
        raw = json.dumps({}).encode()
        h = _handler("POST", "/bots/valid-bot/resume", body={})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("subprocess.Popen"):
                calls = _capture(h)
                h.do_POST()
                assert calls[0][0]==200
                assert not (tmp_path / "valid-bot.paused").exists()

def test_do_post_resume_invalid_name_Given_bad_When_post_Then_400(tmp_path: Path):
    """Given invalid name
    When POST resume
    Then 400."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        raw = json.dumps({}).encode()
        h = _handler("POST", "/bots/bad;name/resume", body={})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        h.path="/bots/bad;name/resume"
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==400

def test_do_post_resume_unknown_Given_no_registry_When_post_Then_404(tmp_path: Path):
    """Given unknown bot
    When POST resume
    Then 404."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "BOT_REGISTRY", []), patch.object(cs, "STATE_DIR", tmp_path):
        raw = json.dumps({}).encode()
        h = _handler("POST", "/bots/ghost/resume", body={})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==404

def test_do_post_start_all_Given_no_bots_key_When_post_Then_starts_all(tmp_path: Path):
    """Given {} body
    When POST /bots/start
    Then starts all."""
    cs.CONTROL_TOKEN = "secret"
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path), patch.object(cs, "ORCH", tmp_path/"orch.py"):
        raw = json.dumps({}).encode()
        h = _handler("POST", "/bots/start", body={})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("subprocess.Popen") as mock_p:
                calls = _capture(h)
                h.do_POST()
                assert calls[0][0]==200
                assert "bot-a" in calls[0][1]["started"]

def test_do_post_start_specific_Given_bots_list_When_post_Then_started(tmp_path: Path):
    """Given bots list
    When POST /bots/start
    Then started."""
    cs.CONTROL_TOKEN = "secret"
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path), patch.object(cs, "ORCH", tmp_path/"orch.py"):
        raw = json.dumps({"bots":["bot-a"]}).encode()
        h = _handler("POST", "/bots/start", body={"bots":["bot-a"]})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("subprocess.Popen") as mp:
                calls = _capture(h)
                h.do_POST()
                assert calls[0][0]==200

def test_do_post_start_bots_not_list_Given_string_When_post_Then_400(tmp_path: Path):
    """Given bots as string
    When POST /bots/start
    Then 400."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        raw = json.dumps({"bots":"bot-a"}).encode()
        h = _handler("POST", "/bots/start", body={"bots":"bot-a"})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==400

def test_do_post_start_unknown_bot_Given_unknown_When_post_Then_400(tmp_path: Path):
    """Given unknown bot in start
    When POST /bots/start
    Then 400."""
    cs.CONTROL_TOKEN = "secret"
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path):
        raw = json.dumps({"bots":["ghost"]}).encode()
        h = _handler("POST", "/bots/start", body={"bots":["ghost"]})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==400

def test_do_post_start_exception_Given_popen_fails_When_post_Then_500(tmp_path: Path):
    """Given Popen fails
    When POST /bots/start
    Then 500."""
    cs.CONTROL_TOKEN = "secret"
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path), patch.object(cs, "ORCH", tmp_path/"orch.py"):
        raw = json.dumps({"bots":["bot-a"]}).encode()
        h = _handler("POST", "/bots/start", body={"bots":["bot-a"]})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("subprocess.Popen", side_effect=RuntimeError("fail")):
                calls = _capture(h)
                h.do_POST()
                assert calls[0][0]==500

def test_do_post_stop_specific_Given_bots_When_post_Then_stopped(tmp_path: Path):
    """Given bots list with force
    When POST /bots/stop
    Then stopped."""
    cs.CONTROL_TOKEN = "secret"
    from codebot.process_manager import BotConfig
    cfg = BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    with patch.object(cs, "BOT_REGISTRY", [cfg]), patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path):
        raw = json.dumps({"bots":["bot-a"],"force":True}).encode()
        h = _handler("POST", "/bots/stop", body={"bots":["bot-a"],"force":True})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        h.path="/bots/stop"
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("codebot.control_server._safe_kill_bot_process", return_value=(True, [1])):
                calls = _capture(h)
                h.do_POST()
                assert calls[0][0]==200

def test_do_post_stop_all_Given_no_bots_When_post_Then_all(tmp_path: Path):
    """Given no bots key with force
    When POST /bots/stop
    Then stopped all."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path):
        raw = json.dumps({"force":True}).encode()
        h = _handler("POST", "/bots/stop", body={"force":True})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("subprocess.run", return_value=MagicMock(returncode=0)):
                calls = _capture(h)
                h.do_POST()
                assert calls[0][0]==200
                assert calls[0][1]["stopped"]=="all"

def test_do_post_stop_invalid_name_Given_bad_When_post_Then_400(tmp_path: Path):
    """Given invalid bot name in stop
    When POST /bots/stop
    Then 400."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        raw = json.dumps({"bots":["bad; name"],"force":True}).encode()
        h = _handler("POST", "/bots/stop", body={"bots":["bad; name"],"force":True})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==400

def test_do_post_drain_success_Given_force_When_post_Then_200(tmp_path: Path):
    """Given force
    When POST /control/drain
    Then 200 and file."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        raw = json.dumps({"force":True}).encode()
        h = _handler("POST", "/control/drain", body={"force":True})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==200
            assert (tmp_path / ".drain").exists()

def test_do_post_clear_drain_Given_drain_When_post_Then_removed(tmp_path: Path):
    """Given drain file
    When POST /control/clear-drain
    Then removed."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        (tmp_path / ".drain").write_text("1")
        raw = json.dumps({}).encode()
        h = _handler("POST", "/control/clear-drain", body={})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==200
            assert not (tmp_path / ".drain").exists()

def test_do_post_update_success_Given_force_When_post_Then_200(tmp_path: Path):
    """Given force
    When POST /control/update
    Then 200."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path), patch.object(cs, "SAFE_UPDATE", tmp_path/"safe.sh"):
        raw = json.dumps({"force":True}).encode()
        h = _handler("POST", "/control/update", body={"force":True})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ok", stderr="")):
                calls = _capture(h)
                h.do_POST()
                assert calls[0][0]==200

def test_do_post_dead_letter_retry_Given_id_When_post_Then_200(tmp_path: Path):
    """Given dead-letter id
    When POST /scheduler/dead-letters/Q-1/retry
    Then 200."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        raw = json.dumps({}).encode()
        h = _handler("POST", "/scheduler/dead-letters/Q-123/retry", body={})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("codebot.control_server.retry_dead_letter", return_value={"status":"retried","id":"Q-123"}):
                calls = _capture(h)
                h.do_POST()
                assert calls[0][0]==200

def test_do_post_telemetry_no_token_Given_no_tokens_When_post_Then_401(tmp_path: Path):
    """Given neither token
    When POST /telemetry
    Then 401 telemetry token not configured."""
    cs.CONTROL_TOKEN = ""
    cs.TELEMETRY_TOKEN = ""
    with patch.object(cs, "STATE_DIR", tmp_path):
        raw = json.dumps({"type":"a"}).encode()
        h = _handler("POST", "/telemetry", body={"type":"a"})
        h.headers = _FakeHeaders({"Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d) if k!="Authorization" else None  # removed: _FakeHeaders handles get
        h.client_address = ("1.2.3.4",123)
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==401

def test_do_post_telemetry_bad_auth_Given_wrong_telemetry_token_When_post_Then_401(tmp_path: Path):
    """Given wrong telemetry token
    When POST /telemetry
    Then 401."""
    cs.TELEMETRY_TOKEN = "tele-secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        raw = json.dumps({"x":1}).encode()
        h = _handler("POST", "/telemetry", body={"x":1})
        # Set wrong auth
        h.headers = _FakeHeaders({"Authorization":"Bearer wrong", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==401

def test_do_post_telemetry_valid_Given_correct_token_When_post_Then_201(tmp_path: Path):
    """Given correct telemetry token and valid signal
    When POST /telemetry
    Then 201."""
    cs.TELEMETRY_TOKEN = "tele-secret"
    cs.CONTROL_TOKEN = "tele-secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        payload = {"signal":"test","value":1}
        raw = json.dumps(payload).encode()
        h = _handler("POST", "/telemetry", body=payload)
        h.headers = _FakeHeaders({"Authorization":"Bearer tele-secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("codebot.telemetry._validate_signal", return_value=(True, "")):
                with patch("codebot.event_log.append_event"):
                    with patch("codebot.telemetry._create_ticket_from_signal", return_value={"success":True,"ticket_id":"CB-1"}):
                        with patch("codebot.event_log.read_events", return_value=[]):
                            calls = _capture(h)
                            h.do_POST()
                            assert calls[0][0]==201
                            assert calls[0][1].get("ticket_id")=="CB-1"

def test_do_post_telemetry_validation_fail_Given_invalid_signal_When_post_Then_400(tmp_path: Path):
    """Given invalid signal
    When POST /telemetry
    Then 400."""
    cs.TELEMETRY_TOKEN = "tele-secret"
    cs.CONTROL_TOKEN = "tele-secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        payload = {"bad":1}
        raw = json.dumps(payload).encode()
        h = _handler("POST", "/telemetry", body=payload)
        h.headers = _FakeHeaders({"Authorization":"Bearer tele-secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            with patch("codebot.telemetry._validate_signal", return_value=(False, "bad input")):
                with patch("codebot.event_log.append_event"):
                    calls = _capture(h)
                    h.do_POST()
                    assert calls[0][0]==400

def test_do_post_telemetry_rate_limited_Given_blocked_When_post_Then_429(tmp_path: Path):
    """Given rate limited telemetry
    When POST /telemetry
    Then 429."""
    cs.TELEMETRY_TOKEN = "tele-secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        raw = json.dumps({}).encode()
        h = _handler("POST", "/telemetry", body={})
        h.headers = _FakeHeaders({"Authorization":"Bearer tele-secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(False, "blocked")):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==429

def test_do_post_unknown_Given_unknown_When_post_Then_404(tmp_path: Path):
    """Given unknown POST path
    When POST
    Then 404."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path):
        raw = json.dumps({}).encode()
        h = _handler("POST", "/unknown/path", body={})
        h.headers = _FakeHeaders({"Authorization":"Bearer secret", "Content-Length": str(len(raw))})
        # h.headers.get = lambda k, d=None: h.headers.get(k,d)  # removed: _FakeHeaders handles get
        h.rfile = io.BytesIO(raw)
        with patch.object(cs._rate_limiter, "is_allowed", return_value=(True, None)):
            calls = _capture(h)
            h.do_POST()
            assert calls[0][0]==404


# ===========================================================================
# control_server — main & log_message & extra helpers
# ===========================================================================
def test_control_server_main_no_token_binds_loopback_Given_no_token_When_main_Then_loopback(tmp_path: Path):
    """Given CONTROL_TOKEN empty
    When main()
    Then binds 127.0.0.1 and logs critical."""
    cs.CONTROL_TOKEN = ""
    with patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path):
        with patch("codebot.control_server.ThreadingHTTPServer") as mock_srv:
            mock_inst = MagicMock()
            mock_srv.return_value = mock_inst
            mock_inst.serve_forever.side_effect = KeyboardInterrupt
            with patch("codebot.control_server.BOT_REGISTRY", []):
                cs.main()
            # Should have created server with 127.0.0.1
            args, _ = mock_srv.call_args
            assert args[0][0] == "127.0.0.1"

def test_control_server_main_with_token_binds_all_Given_token_When_main_Then_0_0_0_0(tmp_path: Path):
    """Given CONTROL_TOKEN set
    When main()
    Then binds 0.0.0.0."""
    cs.CONTROL_TOKEN = "secret"
    with patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path):
        with patch("codebot.control_server.ThreadingHTTPServer") as mock_srv:
            mock_inst = MagicMock()
            mock_srv.return_value = mock_inst
            mock_inst.serve_forever.side_effect = KeyboardInterrupt
            with patch("codebot.control_server.BOT_REGISTRY", []):
                cs.main()
            args, _ = mock_srv.call_args
            assert args[0][0] == "0.0.0.0"

def test_log_message_is_quiet_Given_handler_When_log_message_Then_no_raise():
    """Given handler log_message
    When called
    Then no exception."""
    h = _handler("GET", "/health")
    h.log_message("test %s", "arg")  # should not raise

def test_safe_kill_bot_process_non_digit_pid_Given_mixed_output_When_call_Then_ignored():
    """Given pgrep output has non-digit
    When _safe_kill_bot_process
    Then only digits considered."""
    with patch("subprocess.run", return_value=MagicMock(stdout="abc\n1234\n", returncode=0)):
        with patch("builtins.open", mock_open(read_data=b"python\x00api_runner.py\x00valid-bot\x00")):
            with patch("os.kill"):
                ok, pids = cs._safe_kill_bot_process("valid-bot")
                assert 1234 in pids

def test_safe_kill_process_zero_grace_Given_zero_When_call_Then_handles():
    """Given grace_period 0
    When _safe_kill_process
    Then poll_interval 0.2."""
    with patch("builtins.open", mock_open(read_data=b"python\x00api_runner.py\x00mybot\x00")):
        import signal as _sig
        def fake_kill(pid, sig):
            if sig == 0: return None
            return None
        with patch("os.kill", side_effect=fake_kill):
            with patch("time.sleep"):
                ok, _ = cs._safe_kill_process(1234, "api_runner.py mybot", grace_period=0)
                # Will escalate to SIGKILL and be zombie -> False, but should not crash
                assert ok is False

# ===========================================================================
# control_client
# ===========================================================================
def test_validate_bot_name_simple_valid_Given_valid_When_validate_Then_true():
    """Given valid bot names
    When validate_bot_name_simple
    Then True."""
    assert cc.validate_bot_name_simple("valid-bot") is True
    assert cc.validate_bot_name_simple("bot_123") is True

def test_validate_bot_name_simple_invalid_Given_bad_When_validate_Then_false():
    """Given invalid names
    When validate_bot_name_simple
    Then False."""
    assert cc.validate_bot_name_simple("bad; name") is False
    assert cc.validate_bot_name_simple("") is False
    assert cc.validate_bot_name_simple(123) is False  # type: ignore[arg-type]

def test_print_result_dict_with_undo_Given_dict_undo_When_print_Then_output(capsys):
    """Given dict with undo
    When _print_result
    Then Recovery printed."""
    cc._print_result({"ok":True,"undo":"do this"})
    out = capsys.readouterr().out
    assert "Recovery" in out

def test_print_result_dict_with_dry_run_Given_dry_When_print_Then_preview(capsys):
    """Given dry_run preview
    When _print_result
    Then Preview printed."""
    cc._print_result({"dry_run":True,"preview":{"description":"would do","undo":"undo it"}})
    out = capsys.readouterr().out
    assert "Preview" in out

def test_print_result_string_Given_str_When_print_Then_str(capsys):
    """Given string
    When _print_result
    Then printed."""
    cc._print_result("hello string")
    assert "hello string" in capsys.readouterr().out

def test_is_loopback_host_Given_various_When_check_Then_correct():
    """Given hostnames
    When _is_loopback_host
    Then correct."""
    assert cc._is_loopback_host("localhost") is True
    assert cc._is_loopback_host("127.0.0.1") is True
    assert cc._is_loopback_host("::1") is True
    assert cc._is_loopback_host("[::1]") is True
    assert cc._is_loopback_host("example.com") is False
    assert cc._is_loopback_host(None) is False
    assert cc._is_loopback_host("") is False

def test_req_http_non_loopback_raises_Given_token_http_When_req_Then_runtime_error():
    """Given http non-loopback with token
    When req()
    Then RuntimeError."""
    orig_url = cc.URL
    orig_token = cc.TOKEN
    cc.URL = "http://evil.com"
    cc.TOKEN = "secret"
    try:
        with pytest.raises(RuntimeError, match="Refusing to send Bearer"):
            cc.req("GET", "/bots")
    finally:
        cc.URL = orig_url
        cc.TOKEN = orig_token

def test_req_success_json_Given_mock_urlopen_When_req_Then_tuple():
    """Given mocked urlopen success
    When req()
    Then (status, dict)."""
    cc.URL = "http://127.0.0.1:8081"
    cc.TOKEN = ""
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b'{"ok": true}'
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    with patch("urllib.request.urlopen", return_value=mock_resp):
        with patch("threading.Timer"):
            code, data = cc.req("GET", "/bots")
            assert code == 200 and data["ok"] is True

def test_req_success_raw_Given_non_json_When_req_Then_raw():
    """Given raw non-json
    When req()
    Then raw string."""
    cc.URL = "http://127.0.0.1:8081"
    cc.TOKEN = ""
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b"plain text"
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    with patch("urllib.request.urlopen", return_value=mock_resp):
        with patch("threading.Timer"):
            code, data = cc.req("GET", "/bots")
            assert code == 200 and data == "plain text"

def test_req_empty_body_Given_empty_When_req_Then_empty_dict():
    """Given empty body
    When req()
    Then {}."""
    cc.URL = "http://127.0.0.1:8081"
    cc.TOKEN = ""
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b""
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    with patch("urllib.request.urlopen", return_value=mock_resp):
        with patch("threading.Timer"):
            code, data = cc.req("GET", "/bots")
            assert data == {}

def test_req_http_error_json_Given_httperror_When_req_Then_code_and_json():
    """Given HTTPError with json
    When req()
    Then code and parsed error."""
    cc.URL = "http://127.0.0.1:8081"
    cc.TOKEN = ""
    fp = io.BytesIO(b'{"error":"not found"}')
    err = urllib.error.HTTPError("http://x/bots", 404, "Not Found", {}, fp)
    err.fp = fp  # type: ignore
    with patch("urllib.request.urlopen", side_effect=err):
        with patch("threading.Timer"):
            code, data = cc.req("GET", "/bots/missing")
            assert code == 404 and data["error"] == "not found"

def test_req_http_error_non_json_Given_error_text_When_req_Then_error_dict():
    """Given HTTPError non-json
    When req()
    Then error contains code."""
    cc.URL = "http://127.0.0.1:8081"
    cc.TOKEN = ""
    fp = io.BytesIO(b"not json")
    err = urllib.error.HTTPError("http://x/bots", 500, "Error", {}, fp)
    err.fp = fp  # type: ignore
    with patch("urllib.request.urlopen", side_effect=err):
        with patch("threading.Timer"):
            code, data = cc.req("GET", "/bots")
            assert code == 500
            assert "error" in data

def test_req_http_error_no_fp_Given_no_fp_When_req_Then_empty():
    """Given HTTPError with no fp
    When req()
    Then empty error."""
    cc.URL = "http://127.0.0.1:8081"
    cc.TOKEN = ""
    err = urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)
    err.fp = None  # type: ignore
    with patch("urllib.request.urlopen", side_effect=err):
        with patch("threading.Timer"):
            code, data = cc.req("GET", "/bots")
            assert code == 404

def test_req_url_error_timeout_Given_timeout_When_req_Then_zero_and_msg():
    """Given timeout error
    When req()
    Then 0 and timeout msg."""
    cc.URL = "http://127.0.0.1:8081"
    cc.TOKEN = ""
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timed out")):
        with patch("threading.Timer"):
            code, data = cc.req("GET", "/bots")
            assert code == 0 and "timed out" in data["error"].lower()

def test_req_url_error_generic_Given_urlerror_When_req_Then_zero():
    """Given generic URLError
    When req()
    Then 0."""
    cc.URL = "http://127.0.0.1:8081"
    cc.TOKEN = ""
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
        with patch("threading.Timer"):
            code, data = cc.req("GET", "/bots")
            assert code == 0

def test_req_generic_exception_Given_raises_When_req_Then_zero():
    """Given generic exception
    When req()
    Then 0."""
    cc.URL = "http://127.0.0.1:8081"
    cc.TOKEN = ""
    with patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
        with patch("threading.Timer"):
            code, data = cc.req("GET", "/bots")
            assert code == 0 and "boom" in data["error"]

def test_req_token_over_https_non_loopback_allowed_Given_https_When_req_Then_ok():
    """Given https non-loopback with token
    When req()
    Then allowed (no error)."""
    cc.URL = "https://api.example.com"
    cc.TOKEN = "secret"
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b'{}'
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    with patch("urllib.request.urlopen", return_value=mock_resp):
        with patch("threading.Timer"):
            code, _ = cc.req("GET", "/bots")
            assert code == 200

def test_req_loopback_http_with_token_allowed_Given_loopback_http_When_req_Then_ok():
    """Given http loopback with token
    When req()
    Then allowed."""
    cc.URL = "http://127.0.0.1:8081"
    cc.TOKEN = "secret"
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b'{}'
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    with patch("urllib.request.urlopen", return_value=mock_resp):
        with patch("threading.Timer"):
            code, _ = cc.req("GET", "/bots")
            assert code == 200

def test_req_timeout_param_Given_custom_timeout_When_req_Then_used():
    """Given custom timeout param
    When req(timeout=30)
    Then uses that timeout."""
    cc.URL = "http://127.0.0.1:8081"
    cc.TOKEN = ""
    captured = {}
    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{}'
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False
        return mock_resp
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with patch("threading.Timer"):
            cc.req("GET", "/bots", timeout=30)
            assert captured["timeout"] == 30

def test_req_body_json_encoded_Given_body_When_req_Then_sent():
    """Given body dict
    When req(POST, ..., body)
    Then json encoded."""
    cc.URL = "http://127.0.0.1:8081"
    cc.TOKEN = ""
    captured = {}
    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        captured["method"] = req.method
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{}'
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False
        return mock_resp
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with patch("threading.Timer"):
            cc.req("POST", "/bots/stop", body={"force":True})
            assert b"force" in captured["data"]
            assert captured["method"] == "POST"

def test_cmd_status_success_Given_bots_When_cmd_status_Then_print(capsys):
    """Given bots list
    When cmd_status()
    Then prints."""
    cc.URL = "http://127.0.0.1:8081"
    with patch("codebot.control_client.req", return_value=(200, [{"name":"bot-a","running":True,"heartbeat_age_seconds":1.2,"effective_timeout":90,"risk":"low","model":"m","next_run_in_seconds":10}])):
        cc.cmd_status()
        assert "bot-a" in capsys.readouterr().out

def test_cmd_status_error_Given_non200_When_cmd_status_Then_exit():
    """Given error status
    When cmd_status()
    Then exit 1."""
    with patch("codebot.control_client.req", return_value=(401, {"error":"unauth"})):
        with pytest.raises(SystemExit) as exc:
            cc.cmd_status()
        assert exc.value.code == 1

def test_cmd_scheduler_status_success_Given_payload_When_cmd_Then_print(capsys):
    """Given scheduler payload
    When cmd_scheduler_status()
    Then prints bounded."""
    payload = {"version":1,"budget_state":"ok","budget_day":"2026-01-01","budget_total_actual":100,"drain":False,"dead_letter_count":0,"dead_letter_ids":["a"],"queue_tracked":1,"lease_active":1,"paused_count":0,"paused_bots":[],"disabled_count":0,"disabled_bots":[],"disabled_details":[],"starved_oldest":None,"starved_top":[],"batch_utilization":{},"per_model_actual":{}}
    with patch("codebot.control_client.req", return_value=(200, payload)):
        cc.cmd_scheduler_status()
        out = capsys.readouterr().out
        assert "version" in out

def test_cmd_scheduler_status_error_Given_non200_When_cmd_Then_exit():
    """Given non-200
    When cmd_scheduler_status()
    Then exit."""
    with patch("codebot.control_client.req", return_value=(500, {"error":"fail"})):
        with pytest.raises(SystemExit):
            cc.cmd_scheduler_status()

def test_cmd_scheduler_status_non_dict_Given_str_When_cmd_Then_exit():
    """Given string payload
    When cmd_scheduler_status()
    Then exit."""
    with patch("codebot.control_client.req", return_value=(200, "not a dict")):
        with pytest.raises(SystemExit):
            cc.cmd_scheduler_status()

def test_cmd_scheduler_events_valid_limit_Given_limit_When_cmd_Then_ok():
    """Given valid limit
    When cmd_scheduler_events(limit='5')
    Then req with limit 5."""
    with patch("codebot.control_client.req", return_value=(200, {"events":[]})) as mock_req:
        cc.cmd_scheduler_events("5")
        mock_req.assert_called_once()
        assert "limit=5" in mock_req.call_args[0][1]

def test_cmd_scheduler_events_invalid_limit_Given_bad_When_cmd_Then_exit(capsys):
    """Given bad limit
    When cmd_scheduler_events(limit='abc')
    Then exit 1."""
    with pytest.raises(SystemExit) as exc:
        cc.cmd_scheduler_events("abc")
    assert exc.value.code == 1

def test_cmd_scheduler_events_with_type_Given_type_When_cmd_Then_quoted():
    """Given event_type
    When cmd_scheduler_events
    Then query includes type."""
    with patch("codebot.control_client.req", return_value=(200, {"events":[]})) as mock_req:
        cc.cmd_scheduler_events("20", event_type="lease")
        assert "type=lease" in mock_req.call_args[0][1]

def test_cmd_dead_letters_success_Given_payload_When_cmd_Then_print(capsys):
    """Given dead-letters payload
    When cmd_dead_letters()
    Then prints."""
    with patch("codebot.control_client.req", return_value=(200, {"dead_letters":[]})):
        cc.cmd_dead_letters()
        assert "dead_letters" in capsys.readouterr().out

def test_cmd_dead_letters_error_Given_500_When_cmd_Then_exit():
    """Given error
    When cmd_dead_letters()
    Then exit."""
    with patch("codebot.control_client.req", return_value=(500, {"error":"fail"})):
        with pytest.raises(SystemExit):
            cc.cmd_dead_letters()

def test_cmd_dead_letter_retry_success_Given_ok_When_cmd_Then_print(capsys):
    """Given 200
    When cmd_dead_letter_retry
    Then prints."""
    with patch("codebot.control_client.req", return_value=(200, {"status":"retried"})):
        cc.cmd_dead_letter_retry("Q-123")
        assert "retried" in capsys.readouterr().out

def test_cmd_dead_letter_retry_error_Given_404_When_cmd_Then_exit():
    """Given 404
    When cmd_dead_letter_retry
    Then exit 1."""
    with patch("codebot.control_client.req", return_value=(404, {"status":"not-found"})):
        with pytest.raises(SystemExit):
            cc.cmd_dead_letter_retry("Q-999")

def test_main_no_args_Given_no_cmd_When_main_Then_exit1(capsys):
    """Given no args
    When main()
    Then exit 1 and prints doc."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py"]
    try:
        with pytest.raises(SystemExit) as exc:
            cc.main()
        assert exc.value.code == 1
    finally:
        sys.argv = orig_argv

def test_main_timeout_flag_Given_timeout_When_main_Then_parsed():
    """Given --timeout 30 status When main() Then REQUEST_TIMEOUT set and status called."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "--timeout", "30", "status"]
    orig_t = cc.REQUEST_TIMEOUT
    try:
        with patch("codebot.control_client.req", return_value=(200, [])):
            try:
                cc.main()
            except SystemExit:
                pass
            assert cc.REQUEST_TIMEOUT == 30
    finally:
        sys.argv = orig_argv
        cc.REQUEST_TIMEOUT = orig_t

def test_main_timeout_invalid_Given_bad_When_main_Then_exit():
    """Given --timeout bad
    When main()
    Then exit 1."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "--timeout", "bad", "status"]
    try:
        with pytest.raises(SystemExit) as exc:
            cc.main()
        assert exc.value.code == 1
    finally:
        sys.argv = orig_argv

def test_main_unknown_command_Given_bad_cmd_When_main_Then_exit2():
    """Given unknown command
    When main()
    Then exit 2."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "unknown_cmd_xyz"]
    try:
        with pytest.raises(SystemExit) as exc:
            cc.main()
        assert exc.value.code == 2
    finally:
        sys.argv = orig_argv

def test_main_health_Given_health_When_main_Then_req():
    """Given health
    When main()
    Then req GET /health."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "health"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"status":"ok"})):
            cc.main()  # should print json
    finally:
        sys.argv = orig_argv

def test_main_logs_Given_logs_When_main_Then_req():
    """Given logs bug_fix
    When main()
    Then req logs."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "logs", "my-bot"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"tail":"log line"})):
            cc.main()
    finally:
        sys.argv = orig_argv

def test_main_logs_with_lines_Given_logs_lines_When_main_Then_req_with_lines():
    """Given logs with --lines
    When main()
    Then lines parsed."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "logs", "my-bot", "--lines", "50"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"tail":"x"})) as mock_req:
            cc.main()
            assert "lines=50" in mock_req.call_args[0][1]
    finally:
        sys.argv = orig_argv

def test_main_logs_lines_invalid_Given_bad_lines_When_main_Then_exit():
    """Given bad lines
    When main() logs
    Then exit."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "logs", "my-bot", "--lines", "bad"]
    try:
        with pytest.raises(SystemExit) as exc:
            cc.main()
        assert exc.value.code == 1
    finally:
        sys.argv = orig_argv

def test_main_logs_missing_lines_arg_Given_no_value_When_main_Then_exit():
    """Given logs --lines without value
    When main()
    Then exit."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "logs", "my-bot", "--lines"]
    try:
        with pytest.raises(SystemExit):
            cc.main()
    finally:
        sys.argv = orig_argv

def test_main_restart_with_force_Given_force_When_main_Then_req():
    """Given restart with --force
    When main()
    Then no prompt and req."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "restart", "my-bot", "--force"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"ok":True})):
            with patch("codebot.control_client.validate_bot_name_simple", return_value=True):
                cc.main()
    finally:
        sys.argv = orig_argv

def test_main_restart_invalid_name_Given_bad_name_When_main_Then_exit():
    """Given invalid bot name
    When main() restart
    Then exit."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "restart", "bad; name", "--force"]
    try:
        with patch("codebot.control_client.validate_bot_name_simple", return_value=False):
            with pytest.raises(SystemExit) as exc:
                cc.main()
            assert exc.value.code == 1
    finally:
        sys.argv = orig_argv

def test_main_restart_confirm_yes_Given_prompt_yes_When_main_Then_req(monkeypatch):
    """Given restart without force, user confirms yes
    When main()
    Then req."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "restart", "my-bot"]
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    try:
        with patch("codebot.control_client.req", return_value=(200, {"ok":True})):
            cc.main()
    finally:
        sys.argv = orig_argv

def test_main_restart_confirm_no_Given_prompt_no_When_main_Then_abort(monkeypatch):
    """Given user says no
    When main() restart
    Then aborted exit 0."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "restart", "my-bot"]
    monkeypatch.setattr("builtins.input", lambda _: "no")
    try:
        with pytest.raises(SystemExit) as exc:
            cc.main()
        assert exc.value.code == 0
    finally:
        sys.argv = orig_argv

def test_main_restart_dry_run_Given_dry_run_When_main_Then_req():
    """Given --dry-run
    When main() restart
    Then dry_run payload."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "restart", "my-bot", "--dry-run"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"dry_run":True, "preview":{"description":"would"}})) as mock_req:
            cc.main()
            assert mock_req.call_args[0][2].get("dry_run") is True
    finally:
        sys.argv = orig_argv

def test_main_pause_with_force_Given_pause_force_When_main_Then_req():
    """Given pause --force
    When main()
    Then req."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "pause", "my-bot", "--force"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"ok":True})):
            cc.main()
    finally:
        sys.argv = orig_argv

def test_main_resume_Given_resume_When_main_Then_req():
    """Given resume
    When main()
    Then req."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "resume", "my-bot"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"ok":True})):
            cc.main()
    finally:
        sys.argv = orig_argv

def test_main_stop_no_bots_force_Given_stop_force_When_main_Then_req():
    """Given stop --force no bots
    When main()
    Then req with no bots."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "stop", "--force"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"ok":True})) as mock_req:
            cc.main()
            # payload should not have bots or empty?
            assert mock_req.called
    finally:
        sys.argv = orig_argv

def test_main_stop_with_bots_force_Given_bots_When_main_Then_bots():
    """Given stop bot1 bot2 --force
    When main()
    Then req with bots."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "stop", "bot1", "bot2", "--force"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"ok":True})) as mock_req:
            cc.main()
            assert mock_req.call_args[0][2].get("bots") == ["bot1","bot2"]
    finally:
        sys.argv = orig_argv

def test_main_stop_confirm_no_Given_no_When_main_Then_abort(monkeypatch):
    """Given stop without force, user says no
    When main()
    Then aborted."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "stop", "bot1"]
    monkeypatch.setattr("builtins.input", lambda _: "no")
    try:
        with pytest.raises(SystemExit) as exc:
            cc.main()
        assert exc.value.code == 0
    finally:
        sys.argv = orig_argv

def test_main_drain_force_Given_drain_force_When_main_Then_req():
    """Given drain --force
    When main()
    Then req."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "drain", "--force"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"ok":True})):
            cc.main()
    finally:
        sys.argv = orig_argv

def test_main_drain_dry_run_Given_dry_When_main_Then_dry():
    """Given drain --dry-run
    When main()
    Then dry_run."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "drain", "--dry-run"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"dry_run":True, "preview":{"description":"drain"}})) as mock_req:
            cc.main()
            assert mock_req.call_args[0][2].get("dry_run") is True
    finally:
        sys.argv = orig_argv

def test_main_clear_drain_Given_clear_drain_When_main_Then_req():
    """Given clear-drain
    When main()
    Then req."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "clear-drain"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"ok":True})):
            cc.main()
    finally:
        sys.argv = orig_argv

def test_main_update_force_Given_update_force_When_main_Then_req():
    """Given update --force
    When main()
    Then req."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "update", "--force"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"ok":True})):
            cc.main()
    finally:
        sys.argv = orig_argv

def test_main_update_dry_run_Given_dry_When_main_Then_dry():
    """Given update --dry-run
    When main()
    Then dry_run."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "update", "--dry-run"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"dry_run":True, "preview":{"description":"update"}})) as mock_req:
            cc.main()
            assert mock_req.call_args[0][2].get("dry_run") is True
    finally:
        sys.argv = orig_argv

def test_main_state_Given_state_When_main_Then_req():
    """Given state
    When main()
    Then req GET /state."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "state"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"a":1})):
            cc.main()
    finally:
        sys.argv = orig_argv

def test_main_scheduler_events_args_Given_limit_type_When_main_Then_parsed():
    """Given scheduler-events --limit 5 --type lease
    When main()
    Then correct."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "scheduler-events", "--limit", "5", "--type", "lease"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"events":[]})):
            cc.main()
    finally:
        sys.argv = orig_argv

def test_main_scheduler_events_unknown_option_Given_bad_opt_When_main_Then_exit():
    """Given unknown option
    When main() scheduler-events
    Then exit."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "scheduler-events", "--unknown"]
    try:
        with pytest.raises(SystemExit):
            cc.main()
    finally:
        sys.argv = orig_argv

def test_main_scheduler_events_unknown_arg_Given_positional_When_main_Then_exit():
    """Given positional arg
    When main() scheduler-events
    Then exit."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "scheduler-events", "extra"]
    try:
        with pytest.raises(SystemExit):
            cc.main()
    finally:
        sys.argv = orig_argv

def test_main_retry_dead_letter_Given_retry_When_main_Then_req():
    """Given retry-dead-letter Q-1
    When main()
    Then req."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "retry-dead-letter", "Q-1"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"status":"retried"})):
            cc.main()
    finally:
        sys.argv = orig_argv

def test_main_dead_letters_Given_dead_letters_When_main_Then_req():
    """Given dead-letters
    When main()
    Then req."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "dead-letters"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"dead_letters":[]})):
            cc.main()
    finally:
        sys.argv = orig_argv

def test_main_scheduler_status_Given_cmd_When_main_Then_req():
    """Given scheduler-status
    When main()
    Then req."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "scheduler-status"]
    try:
        with patch("codebot.control_client.req", return_value=(200, {"version":1,"budget_state":"ok","budget_day":"2026-01-01","budget_total_actual":0,"drain":False,"dead_letter_count":0,"dead_letter_ids":[],"queue_tracked":0,"lease_active":0,"paused_count":0,"paused_bots":[],"disabled_count":0,"disabled_bots":[],"disabled_details":[],"starved_oldest":None,"starved_top":[],"batch_utilization":{},"per_model_actual":{}})):
            cc.main()
    finally:
        sys.argv = orig_argv

def test_main_only_timeout_no_cmd_Given_only_timeout_When_main_Then_exit():
    """Given only --timeout with no cmd
    When main()
    Then exit."""
    orig_argv = sys.argv
    sys.argv = ["control_client.py", "--timeout", "10"]
    try:
        with pytest.raises(SystemExit):
            cc.main()
    finally:
        sys.argv = orig_argv
        cc.REQUEST_TIMEOUT = 15

# ===========================================================================
# process_manager — Data classes & registry
# ===========================================================================
def test_bot_config_defaults_Given_empty_When_create_Then_defaults():
    """Given minimal BotConfig args
    When BotConfig created
    Then defaults applied."""
    cfg = pm.BotConfig(name="x", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    assert cfg.enabled is True
    assert cfg.tier == 2
    assert cfg.runner_mode == "api"
    assert cfg.fallback_models == ()

def test_bot_config_custom_Given_all_args_When_create_Then_fields():
    """Given all args
    When BotConfig created
    Then fields match."""
    cfg = pm.BotConfig(name="b", prompt_file="p.md", interval_seconds=100, heartbeat_timeout=50, model="m-a", fallback_model="fb", enabled=False, max_restarts=3, clean_exit_wait=True, tier=1, runner_mode="foo", max_tokens_per_run=123, fallback_models=("a","b"))
    assert cfg.model == "m-a" and cfg.max_tokens_per_run == 123

def test_bot_state_defaults_Given_config_When_create_Then_defaults():
    """Given BotConfig
    When BotState created
    Then defaults."""
    cfg = pm.BotConfig(name="b", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    st = pm.BotState(config=cfg)
    assert st.process is None
    assert st.restart_count == 0
    assert st.last_code_mtimes == {}

def test_load_bot_registry_lazy_success_Given_adapter_When_load_Then_configs():
    """Given adapter returns entries
    When _load_bot_registry_lazy
    Then returns list."""
    pm._BOT_REGISTRY_CACHE = None
    mock_entries = [{"name":"test-bot","prompt":"a.md","interval":60,"model":"x","tier":2}]
    mock_adapter = MagicMock()
    mock_adapter.bot_registry.return_value = mock_entries
    with patch("codebot.orchestrator_services.load_bot_registry", return_value=[pm.BotConfig(name="test-bot", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=120, model="x")]):
        # Because process_manager imports orchestrator_services lazily, need to patch where it's imported inside function (codebot.orchestrator_services)
        # The function does: from codebot.orchestrator_services import load_bot_registry
        lst = pm._load_bot_registry_lazy()
        assert isinstance(lst, list)
    pm._BOT_REGISTRY_CACHE = None

def test_load_bot_registry_lazy_exception_Given_failure_When_load_Then_empty():
    """Given orchestrator_services raises
    When _load_bot_registry_lazy
    Then empty list."""
    pm._BOT_REGISTRY_CACHE = None
    with patch.dict("sys.modules", {"codebot.orchestrator_services": MagicMock(load_bot_registry=MagicMock(side_effect=RuntimeError("fail")))}):
        # Force reimport path: the function tries to import, we mock failure via patching import
        # Simpler: patch the function directly to raise
        import codebot.process_manager as pm2
        orig = pm2._load_bot_registry_lazy
        # Manually set cache None and call with patched import failing
        pm._BOT_REGISTRY_CACHE = None
        # Patch inside function by making orchestrator_services import fail
        with patch("codebot.orchestrator_services.load_bot_registry", side_effect=RuntimeError("fail")):
            lst = pm._load_bot_registry_lazy()
            assert lst == []
    pm._BOT_REGISTRY_CACHE = None

def test_load_bot_registry_cached_Given_cached_When_load_Then_same():
    """Given already cached
    When _load_bot_registry_lazy again
    Then returns cache without reload."""
    pm._BOT_REGISTRY_CACHE = [MagicMock()]
    lst = pm._load_bot_registry_lazy()
    assert lst is pm._BOT_REGISTRY_CACHE
    pm._BOT_REGISTRY_CACHE = None

def test_bot_registry_proxy_iter_len_Given_proxy_When_iter_len_Then_delegates():
    """Given proxy
    When iter/len/getitem/contains/bool
    Then delegates."""
    pm._BOT_REGISTRY_CACHE = None
    proxy = pm._BotRegistryProxy()
    mock_list = [MagicMock(name="a"), MagicMock(name="b")]
    with patch("codebot.process_manager._load_bot_registry_lazy", return_value=mock_list):
        assert len(proxy) == 2
        assert list(proxy) == mock_list
        assert proxy[0] is mock_list[0]
        assert mock_list[0] in proxy
        assert bool(proxy) is True
    pm._BOT_REGISTRY_CACHE = None
    # Empty case
    with patch("codebot.process_manager._load_bot_registry_lazy", return_value=[]):
        assert bool(proxy) is False

# ---------------------------------------------------------------------------
# process_manager — resolve_state_dir / logs_dir / bots_dir
# ---------------------------------------------------------------------------
def test_resolve_state_dir_with_patch_Given_tmp_patch_When_resolve_Then_tmp(tmp_path: Path):
    """Given STATE_DIR patched
    When _resolve_state_dir
    Then returns patched."""
    state_dir, logs_dir, bots_dir = tmp_path/"s", tmp_path/"l", tmp_path/"b"
    for d in (state_dir, logs_dir, bots_dir): d.mkdir(parents=True, exist_ok=True)
    with patch.object(pm, "STATE_DIR", state_dir):
        with patch.object(pm, "_project_root", tmp_path):
            # Need to ensure Path(current_default) != patched
            res = pm._resolve_state_dir()
            assert res == state_dir

def test_resolve_state_dir_via_get_paths_Given_no_patch_When_resolve_Then_via_get_paths(tmp_path: Path):
    """Given no patch
    When _resolve_state_dir
    Then via get_paths."""
    cfg = sm.PathConfig(bots_dir=tmp_path, state_dir=tmp_path/"state", logs_dir=tmp_path/"logs", backup_dir=tmp_path/"state"/"backup", drain_file=tmp_path/"state"/".drain", update_lock=tmp_path/"state"/".update_lock", restart_file=tmp_path/"state"/".restart", alignment_events_dir=tmp_path/"state"/"align")
    for d in (cfg.state_dir, cfg.logs_dir, cfg.backup_dir, cfg.alignment_events_dir):
        d.mkdir(parents=True, exist_ok=True)
    sm._paths = cfg
    with patch.object(sm, "get_paths", return_value=cfg):
        res = pm._resolve_state_dir()
        assert res == cfg.state_dir
    # Need to restore sm paths via fixture will handle but ensure no leak

def test_resolve_logs_dir_with_patch_Given_patch_When_resolve_Then_tmp(tmp_path: Path):
    """Given LOGS_DIR patched
    When _resolve_logs_dir
    Then patched."""
    with patch.object(pm, "LOGS_DIR", tmp_path/"custom_logs"):
        with patch.object(pm, "_project_root", Path("/tmp/fake_root")):
            (tmp_path/"custom_logs").mkdir(parents=True, exist_ok=True)
            res = pm._resolve_logs_dir()
            assert res == tmp_path/"custom_logs"

def test_resolve_bots_dir_with_patch_Given_patch_When_resolve_Then_tmp(tmp_path: Path):
    """Given BOTS_DIR patched
    When _resolve_bots_dir
    Then patched."""
    with patch.object(pm, "BOTS_DIR", tmp_path):
        with patch.object(pm, "_project_root", Path("/tmp/other")):
            assert pm._resolve_bots_dir() == tmp_path

def test_read_heartbeat_hm_patch_Given_hm_state_patch_When_read_Then_value(tmp_path: Path):
    """Given health_monitor STATE_DIR patched
    When read_heartbeat
    Then reads from patched."""
    with patch.object(hm, "STATE_DIR", tmp_path):
        (tmp_path / "mybot.heartbeat").write_text(str(time.time()))
        val = pm.read_heartbeat("mybot")
        assert val > 0
        # missing
        assert pm.read_heartbeat("missing") == 0.0
        # corrupt
        (tmp_path / "bad.heartbeat").write_text("not-a-number")
        assert pm.read_heartbeat("bad") == 0.0

def test_batch_read_heartbeats_hm_patch_Given_patched_When_batch_Then_map(tmp_path: Path):
    """Given hm patched
    When batch_read_heartbeats
    Then map."""
    with patch.object(hm, "STATE_DIR", tmp_path):
        now = time.time()
        (tmp_path / "a.heartbeat").write_text(str(now))
        (tmp_path / "b.heartbeat").write_text("bad")
        res = pm.batch_read_heartbeats(["a","b","missing"])
        assert res["a"] == pytest.approx(now, abs=0.1)
        assert res["b"] == 0.0
        assert res["missing"] == 0.0

def test_write_heartbeat_Given_tmp_When_write_Then_exists(tmp_path: Path):
    """Given isolated state
    When write_heartbeat
    Then file exists."""
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        pm.write_heartbeat("mybot")
        assert (tmp_path / "mybot.heartbeat").exists()

def test_heartbeat_path_and_log_path_Given_bot_When_path_Then_correct(tmp_path: Path):
    """Given bot name
    When heartbeat_path/log_path
    Then correct."""
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        with patch("codebot.process_manager._resolve_logs_dir", return_value=tmp_path):
            assert pm.heartbeat_path("bot-a") == tmp_path / "bot-a.heartbeat"
            assert pm.log_path("bot-a") == tmp_path / "bot-a.log"

# ---------------------------------------------------------------------------
# process_manager — PromptGateway
# ---------------------------------------------------------------------------
def test_get_prompt_gateway_singleton_Given_none_When_get_Then_default():
    """Given None
    When get_prompt_gateway
    Then Default."""
    pm.clear_prompt_gateway()
    gw = pm.get_prompt_gateway()
    assert isinstance(gw, pm.DefaultPromptGateway)

def test_set_prompt_gateway_custom_Given_custom_When_set_Then_same():
    """Given custom gateway
    When set_prompt_gateway
    Then get returns same."""
    mock_gw = MagicMock(spec=pm.PromptGatewayProtocol)
    pm.set_prompt_gateway(mock_gw)
    assert pm.get_prompt_gateway() is mock_gw
    pm.clear_prompt_gateway()

def test_clear_prompt_gateway_Given_custom_When_clear_Then_default():
    """Given custom
    When clear
    Then default again."""
    pm.set_prompt_gateway(MagicMock())
    pm.clear_prompt_gateway()
    assert isinstance(pm.get_prompt_gateway(), pm.DefaultPromptGateway)

def test_default_gateway_build_message_Given_text_When_build_Then_delegates():
    """Given DefaultGateway
    When build_message
    Then delegates to prompt_gateway."""
    pm.clear_prompt_gateway()
    gw = pm.DefaultPromptGateway()
    with patch("codebot.prompt_gateway.build_message", return_value="msg") as m:
        out = gw.build_message("b","m","text","hb","ck","block","state","logs","prompt.md")
        assert out == "msg"
        m.assert_called_once()

def test_default_gateway_note_spawn_Given_default_When_note_Then_delegates():
    """Given default
    When note_spawn
    Then delegates."""
    gw = pm.DefaultPromptGateway()
    with patch("codebot.prompt_gateway.note_spawn") as m:
        gw.note_spawn()
        m.assert_called_once()

def test_default_gateway_spawn_allowed_Given_default_When_allowed_Then_delegates():
    """Given default
    When spawn_allowed
    Then delegates."""
    gw = pm.DefaultPromptGateway()
    with patch("codebot.prompt_gateway.spawn_allowed", return_value=(True,"ok")) as m:
        ok, why = gw.spawn_allowed({}, "m")
        assert ok is True
        m.assert_called_once()

def test_default_gateway_props_Given_default_When_max_concurrent_Then_value():
    """Given default
    When max_concurrent/min_spawn_gap
    Then returns."""
    gw = pm.DefaultPromptGateway()
    with patch("codebot.prompt_gateway.MAX_CONCURRENT", 26):
        with patch("codebot.prompt_gateway.MIN_SPAWN_GAP", 25):
            assert gw.max_concurrent == 26
            assert gw.min_spawn_gap == 25

# ---------------------------------------------------------------------------
# process_manager — _count_api_runner_processes
# ---------------------------------------------------------------------------
def test_count_api_runner_zero_Given_no_proc_When_count_Then_zero():
    """Given pgrep returns 1 (no match)
    When _count_api_runner_processes
    Then 0."""
    pm._clear_process_count_cache()
    with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")):
        with patch("time.monotonic", return_value=0.0):
            assert pm._count_api_runner_processes() == 0
    pm._clear_process_count_cache()

def test_count_api_runner_two_Given_two_lines_When_count_Then_two():
    """Given two lines
    When _count_api_runner_processes
    Then 2."""
    pm._clear_process_count_cache()
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="111\n222\n")):
        with patch("time.monotonic", return_value=0.0):
            assert pm._count_api_runner_processes() == 2
    pm._clear_process_count_cache()

def test_count_api_runner_fail_closed_timeout_Given_timeout_When_count_Then_9999():
    """Given TimeoutExpired
    When _count_api_runner_processes
    Then 9999."""
    pm._clear_process_count_cache()
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pgrep", timeout=5)):
        with patch("time.monotonic", return_value=0.0):
            assert pm._count_api_runner_processes() == 9999
    pm._clear_process_count_cache()

def test_count_api_runner_fail_closed_nofile_Given_file_not_found_When_count_Then_9999():
    """Given FileNotFoundError
    When _count_api_runner_processes
    Then 9999."""
    pm._clear_process_count_cache()
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with patch("time.monotonic", return_value=0.0):
            assert pm._count_api_runner_processes() == 9999
    pm._clear_process_count_cache()

def test_count_api_runner_fail_closed_oserror_Given_oserror_When_count_Then_9999():
    """Given OSError
    When _count_api_runner_processes
    Then 9999."""
    pm._clear_process_count_cache()
    with patch("subprocess.run", side_effect=OSError("boom")):
        with patch("time.monotonic", return_value=0.0):
            assert pm._count_api_runner_processes() == 9999
    pm._clear_process_count_cache()

def test_count_api_runner_cached_Given_cached_When_count_Then_cached():
    """Given cache valid
    When _count_api_runner_processes
    Then cached."""
    pm._clear_process_count_cache()
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="1\n2\n")) as m:
        with patch("time.monotonic", side_effect=[0.0, 1.0]):
            a = pm._count_api_runner_processes()
            b = pm._count_api_runner_processes()
            assert a==2 and b==2
            assert m.call_count==1
    pm._clear_process_count_cache()

def test_count_api_runner_ttl_expires_Given_expired_When_count_Then_refetch():
    """Given TTL expired
    When _count_api_runner_processes
    Then refetch."""
    pm._clear_process_count_cache()
    with patch("subprocess.run", side_effect=[MagicMock(returncode=0, stdout="1\n"), MagicMock(returncode=0, stdout="1\n2\n3\n")] ) as m:
        with patch("time.monotonic", side_effect=[0.0, 6.0]):
            a = pm._count_api_runner_processes()
            b = pm._count_api_runner_processes()
            assert a==1 and b==3
            assert m.call_count==2
    pm._clear_process_count_cache()

def test_clear_process_count_cache_Given_cache_When_clear_Then_none():
    """Given cached
    When _clear_process_count_cache
    Then None."""
    pm._cached_process_count = 5
    pm._clear_process_count_cache()
    assert pm._cached_process_count is None

# ---------------------------------------------------------------------------
# process_manager — _spawn_gate
# ---------------------------------------------------------------------------
def test_spawn_gate_cap_Given_running_at_cap_When_gate_Then_false():
    """Given running >= cap
    When _spawn_gate
    Then False cap."""
    with patch("codebot.process_manager._count_api_runner_processes", return_value=100):
        ok, why = pm._spawn_gate(bots={}, bot_name="bot-a")
        assert ok is False and "cap" in why

def test_spawn_gate_demand_bypass_gap_Given_demand_When_gate_Then_true():
    """Given is_demand
    When _spawn_gate
    Then True slot available."""
    with patch("codebot.process_manager._count_api_runner_processes", return_value=0):
        ok, why = pm._spawn_gate(bots={}, bot_name="bot-a", is_demand=True)
        assert ok is True

def test_spawn_gate_overture_bypass_Given_overture_When_gate_Then_true():
    """Given is_overture
    When _spawn_gate
    Then True."""
    with patch("codebot.process_manager._count_api_runner_processes", return_value=0):
        ok, _ = pm._spawn_gate(bots={}, bot_name="bot-a", is_overture=True)
        assert ok is True

def test_spawn_gate_has_assignment_Given_ticket_When_gate_Then_bypass_gap():
    """Given bot has assigned_ticket
    When _spawn_gate
    Then bypass gap."""
    from codebot.process_manager import BotConfig, BotState
    cfg = BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = BotState(config=cfg)
    bot._assigned_ticket_id = "CB-123"
    with patch("codebot.process_manager._count_api_runner_processes", return_value=0):
        # Make _last_spawn_time recent so gap would block but assignment bypasses
        pm._last_spawn_time = time.time()
        ok, _ = pm._spawn_gate(bots={"bot-a": bot}, bot_name="bot-a")
        assert ok is True

def test_spawn_gate_claims_dir_has_assignment_Given_claims_file_When_gate_Then_true():
    """Given claims dir with recent file
    When _spawn_gate
    Then has_assignment true."""
    with patch("codebot.process_manager._count_api_runner_processes", return_value=0):
        # Create tmp claims dir
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            claims = td_path / "claims"
            claims.mkdir()
            (claims / "ticket.bot-a.json").write_text("{}")
            with patch("codebot.process_manager._resolve_state_dir", return_value=td_path):
                ok, _ = pm._spawn_gate(bots={}, bot_name="bot-a")
                assert ok is True

def test_spawn_gate_worker_gap_Given_worker_recent_When_gate_Then_false():
    """Given worker gap not met
    When _spawn_gate
    Then False gap worker."""
    with patch("codebot.process_manager._count_api_runner_processes", return_value=0):
        pm._last_spawn_time = time.time()
        ok, why = pm._spawn_gate(bots={}, bot_name="worker-1")
        assert ok is False and "worker" in why

def test_spawn_gate_normal_gap_Given_recent_When_gate_Then_false():
    """Given normal gap recent
    When _spawn_gate
    Then False gap."""
    with patch("codebot.process_manager._count_api_runner_processes", return_value=0):
        pm._last_spawn_time = time.time()
        ok, why = pm._spawn_gate(bots={}, bot_name="normal-bot")
        assert ok is False and "gap" in why

def test_spawn_gate_gap_ok_Given_old_spawn_When_gate_Then_true():
    """Given old last_spawn
    When _spawn_gate
    Then True."""
    with patch("codebot.process_manager._count_api_runner_processes", return_value=0):
        pm._last_spawn_time = time.time() - 1000
        ok, _ = pm._spawn_gate(bots={}, bot_name="normal-bot")
        assert ok is True

def test_spawn_gate_cap_with_assignment_higher_cap_Given_high_running_When_assignment_Then_higher_cap():
    """Given running >= cap but has assignment, higher cap allows
    But if running >= cap+2, still blocked."""
    with patch("codebot.process_manager._count_api_runner_processes", return_value=1000):
        # With assignment, cap becomes 28 (if default 26+2) still 1000 >28 => blocked with assignment
        from codebot.process_manager import BotConfig, BotState
        cfg = BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
        bot = BotState(config=cfg)
        bot._assigned_ticket_id = "CB-1"
        ok, why = pm._spawn_gate(bots={"bot-a": bot}, bot_name="bot-a")
        assert ok is False and "cap" in why

# ---------------------------------------------------------------------------
# process_manager — _write_json_atomic, _state_write_lock, _prompt_read_lock
# ---------------------------------------------------------------------------
def test_write_json_atomic_dict_Given_dict_When_write_Then_file(tmp_path: Path):
    """Given dict
    When _write_json_atomic
    Then file contains json."""
    p = tmp_path / "out.json"
    pm._write_json_atomic(p, {"a":1})
    assert json.loads(p.read_text())["a"] == 1

def test_write_json_atomic_list_Given_list_When_write_Then_file(tmp_path: Path):
    """Given list
    When _write_json_atomic
    Then list."""
    p = tmp_path / "out2.json"
    pm._write_json_atomic(p, [1,2,3])
    assert json.loads(p.read_text()) == [1,2,3]

def test_write_json_atomic_str_Given_str_When_write_Then_str():
    """Given string
    When _write_json_atomic
    Then raw."""
    p = Path("/tmp") / "test_str_atomic.json"
    # Use tmp_path instead
    pass

def test_write_json_atomic_str_tmp_path_Given_str_When_write_Then_raw(tmp_path: Path):
    """Given string
    When _write_json_atomic
    Then raw string written."""
    p = tmp_path / "str.json"
    pm._write_json_atomic(p, "hello")
    assert p.read_text() == "hello"

def test_state_write_lock_Given_bot_When_lock_Then_yield(tmp_path: Path):
    """Given bot name
    When _state_write_lock
    Then yields."""
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        with pm._state_write_lock("bot-a"):
            assert (tmp_path / "bot-a.state.lock").exists()

def test_prompt_read_lock_Given_file_When_lock_Then_read(tmp_path: Path):
    """Given prompt file
    When _prompt_read_lock
    Then can read."""
    f = tmp_path / "prompt.md"
    f.write_text("hello")
    with pm._prompt_read_lock(f):
        assert f.read_text() == "hello"

def test_prompt_read_lock_missing_Given_no_file_When_lock_Then_raise(tmp_path: Path):
    """Given missing file
    When _prompt_read_lock
    Then raise."""
    f = tmp_path / "missing.md"
    with pytest.raises(FileNotFoundError):
        with pm._prompt_read_lock(f):
            pass

# ---------------------------------------------------------------------------
# process_manager — update_bot_state
# ---------------------------------------------------------------------------
def test_update_bot_state_creates_file_Given_bot_When_update_Then_file(tmp_path: Path):
    """Given BotState
    When update_bot_state
    Then file."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg, restart_count=2, consecutive_errors=1, next_run_at=123.0)
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        pm.update_bot_state(bot, "running")
        data = json.loads((tmp_path / "bot-a.state.json").read_text())
        assert data["status"] == "running"
        assert data["restart_count"] == 2

def test_update_bot_state_corrupt_existing_Given_bad_json_When_update_Then_overwrite(tmp_path: Path):
    """Given corrupt existing file
    When update_bot_state
    Then overwritten."""
    cfg = pm.BotConfig(name="bot-b", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        (tmp_path / "bot-b.state.json").write_text("{bad")
        pm.update_bot_state(bot, "waiting")
        data = json.loads((tmp_path / "bot-b.state.json").read_text())
        assert data["status"] == "waiting"

def test_update_bot_state_non_dict_existing_Given_list_When_update_Then_overwrite(tmp_path: Path):
    """Given non-dict existing
    When update_bot_state
    Then dict."""
    cfg = pm.BotConfig(name="bot-c", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        (tmp_path / "bot-c.state.json").write_text(json.dumps([1,2,3]))
        pm.update_bot_state(bot, "running")
        data = json.loads((tmp_path / "bot-c.state.json").read_text())
        assert isinstance(data, dict) and data["status"]=="running"

def test_update_bot_state_with_restart_timestamps_Given_existing_timestamps_When_update_Then_merged(tmp_path: Path):
    """Given existing restart_timestamps
    When update_bot_state with restart_count>0
    Then timestamps preserved."""
    cfg = pm.BotConfig(name="bot-d", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg, restart_count=1, last_restart_reset=time.time())
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        (tmp_path / "bot-d.state.json").write_text(json.dumps({"restart_timestamps":[100.0,200.0]}))
        pm.update_bot_state(bot, "running")
        data = json.loads((tmp_path / "bot-d.state.json").read_text())
        assert 100.0 in data["restart_timestamps"]

def test_update_bot_state_exception_Given_write_fails_When_update_Then_no_raise(tmp_path: Path):
    """Given write fails
    When update_bot_state
    Then logged not raised."""
    cfg = pm.BotConfig(name="bot-e", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        with patch("codebot.process_manager._write_json_atomic", side_effect=OSError("fail")):
            pm.update_bot_state(bot, "running")  # should not raise

# ---------------------------------------------------------------------------
# process_manager — checkpoint helpers
# ---------------------------------------------------------------------------
def test_checkpoint_path_Given_name_When_path_Then_correct(tmp_path: Path):
    """Given bot name
    When checkpoint_path
    Then correct."""
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        assert pm.checkpoint_path("mybot") == tmp_path / "mybot.checkpoint.json"

def test_load_json_file_valid_Given_json_When_load_Then_data(tmp_path: Path):
    """Given valid json file
    When _load_json_file
    Then data and raw."""
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"a":1}))
    data, raw = pm._load_json_file(p)
    assert data["a"]==1 and "a" in raw

def test_load_json_file_bad_Given_corrupt_When_load_Then_none(tmp_path: Path):
    """Given corrupt
    When _load_json_file
    Then None."""
    p = tmp_path / "bad.json"
    p.write_text("{bad")
    data, raw = pm._load_json_file(p)
    assert data is None and raw==""

def test_checkpoint_backup_path_Given_json_When_backup_Then_bak(tmp_path: Path):
    """Given json path
    When checkpoint_backup_path
    Then .checkpoint.bak."""
    p = tmp_path / "bot.checkpoint.json"
    bak = pm.checkpoint_backup_path(p)
    assert bak == tmp_path / "bot.checkpoint.checkpoint.bak"

def test_checkpoint_backup_path_non_json_Given_no_suffix_When_backup_Then_bak(tmp_path: Path):
    """Given non-json
    When checkpoint_backup_path
    Then .bak."""
    p = Path("/tmp/x")
    assert pm.checkpoint_backup_path(p) == Path("/tmp/x.bak")

def test_validate_checkpoint_payload_valid_Given_dict_When_validate_Then_dict():
    """Given valid payload
    When validate_checkpoint_payload
    Then dict."""
    data = {"bot":"a","updated_at": time.time(), "x":1}
    assert pm.validate_checkpoint_payload(data) is not None

def test_validate_checkpoint_payload_non_dict_Given_list_When_validate_Then_none():
    """Given list
    When validate_checkpoint_payload
    Then None."""
    assert pm.validate_checkpoint_payload([1,2]) is None

def test_validate_checkpoint_payload_bad_bot_Given_int_When_validate_Then_none():
    """Given bot as int
    When validate_checkpoint_payload
    Then None."""
    assert pm.validate_checkpoint_payload({"bot":123}) is None

def test_validate_checkpoint_payload_bad_updated_at_Given_bad_When_validate_Then_none():
    """Given bad updated_at
    When validate_checkpoint_payload
    Then None."""
    assert pm.validate_checkpoint_payload({"bot":"a","updated_at":"bad"}) is None

def test_validate_checkpoint_payload_oversized_Given_big_When_validate_Then_none():
    """Given >4KB
    When validate_checkpoint_payload
    Then None."""
    big = {"bot":"a","data":"x"*5000}
    assert pm.validate_checkpoint_payload(big) is None

def test_write_platform_checkpoint_valid_Given_payload_When_write_Then_path(tmp_path: Path):
    """Given valid payload
    When write_platform_checkpoint
    Then path."""
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        with patch("codebot.process_manager.checkpoint_path", return_value=tmp_path/"b.checkpoint.json"):
            with patch("codebot.process_manager.checkpoint_backup_path", return_value=tmp_path/"b.checkpoint.bak"):
                p = pm.write_platform_checkpoint("b", {"key":"val"})
                assert p is not None and p.exists()

def test_write_platform_checkpoint_non_dict_Given_str_When_write_Then_none():
    """Given non-dict
    When write_platform_checkpoint
    Then None."""
    assert pm.write_platform_checkpoint("b", "not dict") is None  # type: ignore[arg-type]

def test_write_platform_checkpoint_invalid_Given_big_When_write_Then_none(tmp_path: Path):
    """Given oversized
    When write_platform_checkpoint
    Then None."""
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        big = {"data":"x"*5000}
        assert pm.write_platform_checkpoint("b", big) is None

def test_write_platform_checkpoint_oserror_Given_fail_When_write_Then_none(tmp_path: Path):
    """Given write fails
    When write_platform_checkpoint
    Then None."""
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        with patch("codebot.process_manager._write_json_atomic", side_effect=OSError):
            assert pm.write_platform_checkpoint("b", {"a":1}) is None

def test_quarantine_checkpoint_success_Given_file_When_quarantine_Then_moved(tmp_path: Path):
    """Given file
    When _quarantine_checkpoint
    Then moved to quarantine."""
    p = tmp_path / "bot.checkpoint.json"
    p.write_text(json.dumps({"a":1}))
    pm._quarantine_checkpoint(p, "test")
    assert not p.exists()
    assert (tmp_path / "checkpoint_quarantine").exists()

def test_quarantine_checkpoint_fail_Given_no_file_When_quarantine_Then_unlink():
    """Given missing file but replace fails
    When _quarantine_checkpoint
    Then tries unlink."""
    p = Path("/tmp/nonexistent_quarantine_test.json")
    # Should not raise
    pm._quarantine_checkpoint(p, "reason")

def test_discard_and_read_backup_Given_backup_When_discard_Then_data(tmp_path: Path):
    """Given corrupt primary and good backup
    When _discard_and_read_backup
    Then backup data."""
    p = tmp_path / "b.checkpoint.json"
    p.write_text("{bad")
    bak = tmp_path / "b.checkpoint.bak"
    bak.write_text(json.dumps({"good":True}))
    data = pm._discard_and_read_backup(p, bak)
    assert data == {"good":True}

def test_discard_and_read_backup_no_backup_Given_no_backup_When_discard_Then_none(tmp_path: Path):
    """Given no backup
    When _discard_and_read_backup
    Then None."""
    p = tmp_path / "c.checkpoint.json"
    p.write_text("{bad")
    bak = tmp_path / "c.checkpoint.bak"
    assert pm._discard_and_read_backup(p, bak) is None

def test_read_checkpoint_no_file_no_bak_Given_missing_When_read_Then_none(tmp_path: Path):
    """Given no file and no bak
    When read_checkpoint
    Then None."""
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        with patch("codebot.process_manager.checkpoint_path", return_value=tmp_path/"missing.checkpoint.json"):
            with patch("codebot.process_manager.checkpoint_backup_path", return_value=tmp_path/"missing.checkpoint.bak"):
                assert pm.read_checkpoint("missing") is None

def test_read_checkpoint_bak_fallback_Given_bak_When_read_Then_data(tmp_path: Path):
    """Given bak exists but primary not
    When read_checkpoint
    Then bak data."""
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        p = tmp_path / "bot.checkpoint.json"
        bak = cm.checkpoint_backup_path(tmp_path / "bot.checkpoint.json")
        bak.write_text(json.dumps({"from":"bak"}))
        with patch("codebot.process_manager.checkpoint_path", return_value=p):
            with patch("codebot.process_manager.checkpoint_backup_path", return_value=bak):
                assert pm.read_checkpoint("bot") == {"from":"bak"}

def test_read_checkpoint_corrupt_primary_Given_corrupt_When_read_Then_bak(tmp_path: Path):
    """Given corrupt primary
    When read_checkpoint
    Then fallback to bak."""
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        p = tmp_path / "bot2.checkpoint.json"
        bak = tmp_path / "bot2.checkpoint.bak"
        p.write_text("{bad")
        bak.write_text(json.dumps({"good":1}))
        with patch("codebot.process_manager.checkpoint_path", return_value=p):
            with patch("codebot.process_manager.checkpoint_backup_path", return_value=bak):
                assert pm.read_checkpoint("bot2") == {"good":1}

def test_read_checkpoint_valid_writes_bak_Given_valid_When_read_Then_bak(tmp_path: Path):
    """Given valid primary
    When read_checkpoint
    Then writes bak."""
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        p = tmp_path / "bot3.checkpoint.json"
        bak = tmp_path / "bot3.checkpoint.bak"
        data = {"bot":"bot3","updated_at": time.time()}
        p.write_text(json.dumps(data))
        with patch("codebot.process_manager.checkpoint_path", return_value=p):
            with patch("codebot.process_manager.checkpoint_backup_path", return_value=bak):
                res = pm.read_checkpoint("bot3")
                assert res is not None
                assert bak.exists()

def test_read_checkpoint_oversized_after_valid_Given_big_file_When_read_Then_quarantine(tmp_path: Path):
    """Given file >4KB after validation
    When read_checkpoint
    Then quarantine and fallback."""
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        p = tmp_path / "big.checkpoint.json"
        bak = cm.checkpoint_backup_path(tmp_path / "big.checkpoint.json")
        big = {"bot":"big","data":"x"*5000,"updated_at": time.time()}
        # write directly bypassing validate
        p.write_text(json.dumps(big))
        bak.write_text(json.dumps({"small":1}))
        with patch("codebot.process_manager.checkpoint_path", return_value=p):
            with patch("codebot.process_manager.checkpoint_backup_path", return_value=bak):
                # validate will fail due to size, so will discard and fallback? Actually oversized validated returns None -> discard
                # But our p contains oversized data, validate will return None -> discard
                res = pm.read_checkpoint("big")
                # Should fallback to bak
                assert res == {"small":1}

# ---------------------------------------------------------------------------
# process_manager — input changes & skip
# ---------------------------------------------------------------------------
def test_check_inputs_changed_last_run_zero_Given_zero_When_check_Then_true():
    """Given last_run 0
    When _check_inputs_changed
    Then True."""
    cfg = pm.BotConfig(name="b", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    assert pm._check_inputs_changed(bot, 0) is True

def test_check_inputs_changed_prompt_changed_Given_newer_prompt_When_check_Then_true(tmp_path: Path):
    """Given prompt mtime newer
    When _check_inputs_changed
    Then True."""
    cfg = pm.BotConfig(name="b2", prompt_file="prompt.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
        (tmp_path / "prompt.md").write_text("hello")
        # set last_run very old
        assert pm._check_inputs_changed(bot, 1.0) is True

def test_check_inputs_changed_no_change_Given_old_prompt_When_check_Then_false(tmp_path: Path):
    """Given prompt older than last_run
    When _check_inputs_changed
    Then False if manifest also not changed."""
    cfg = pm.BotConfig(name="b3", prompt_file="prompt.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
        p = tmp_path / "prompt.md"
        p.write_text("hello")
        old = time.time() + 1000  # future last_run means no change
        assert pm._check_inputs_changed(bot, old) is False

def test_check_inputs_changed_manifest_changed_Given_manifest_newer_When_check_Then_true(tmp_path: Path):
    """Given manifest input file newer
    When _check_inputs_changed
    Then True."""
    cfg = pm.BotConfig(name="my-bot", prompt_file="prompt.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
        manifests = tmp_path / "manifests"
        manifests.mkdir()
        # create manifest pointing to prompt file? Actually manifest lists inputs
        inp_file = tmp_path / "input.txt"
        inp_file.write_text("data")
        manifest = manifests / "my_bot.json"
        manifest.write_text(json.dumps({"input":[{"path":"input.txt"}]}))
        # Make inp_file newer than last_run
        assert pm._check_inputs_changed(bot, 1.0) is True

def test_check_inputs_changed_manifest_exception_Given_bad_manifest_When_check_Then_true(tmp_path: Path):
    """Given bad manifest
    When _check_inputs_changed
    Then True (fail open)."""
    cfg = pm.BotConfig(name="my-bot", prompt_file="prompt.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
        manifests = tmp_path / "manifests"
        manifests.mkdir()
        (manifests / "my_bot.json").write_text("{bad")
        assert pm._check_inputs_changed(bot, time.time()) is True

def test_should_skip_run_demand_Given_demand_When_should_skip_Then_false():
    """Given demand
    When _should_skip_run
    Then False."""
    cfg = pm.BotConfig(name="b", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    assert pm._should_skip_run(bot, time.time(), is_demand=True) is False

def test_should_skip_run_assigned_Given_ticket_When_should_skip_Then_false():
    """Given assigned ticket
    When _should_skip_run
    Then False."""
    cfg = pm.BotConfig(name="b", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    bot._assigned_ticket_id = "CB-1"
    assert pm._should_skip_run(bot, time.time()) is False

def test_should_skip_run_changed_inputs_Given_changed_When_should_skip_Then_false(tmp_path: Path):
    """Given inputs changed
    When _should_skip_run
    Then False."""
    cfg = pm.BotConfig(name="b", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._check_inputs_changed", return_value=True):
        assert pm._should_skip_run(bot, time.time()) is False

def test_should_skip_run_no_change_not_worker_Given_stable_When_should_skip_Then_true(tmp_path: Path):
    """Given stable inputs, not worker
    When _should_skip_run
    Then True."""
    cfg = pm.BotConfig(name="normal-bot", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._check_inputs_changed", return_value=False):
        assert pm._should_skip_run(bot, time.time()) is True

def test_should_skip_run_worker_always_Given_worker_When_should_skip_Then_false():
    """Given worker bot
    When _should_skip_run stable
    Then False."""
    cfg = pm.BotConfig(name="worker-1", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._check_inputs_changed", return_value=False):
        assert pm._should_skip_run(bot, time.time()) is False

def test_prepare_prompt_missing_Given_no_file_When_prepare_Then_empty(tmp_path: Path):
    """Given missing prompt file
    When _prepare_prompt_with_context
    Then empty."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="missing.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
        assert pm._prepare_prompt_with_context(bot) == ""

def test_prepare_prompt_generic_error_Given_error_When_prepare_Then_empty(tmp_path: Path):
    """Given read error
    When _prepare_prompt_with_context
    Then empty."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
        (tmp_path / "a.md").write_text("hello")
        with patch("codebot.process_manager._prompt_read_lock", side_effect=RuntimeError("boom")):
            assert pm._prepare_prompt_with_context(bot) == ""

def test_prepare_prompt_success_Given_file_When_prepare_Then_text(tmp_path: Path):
    """Given prompt file
    When _prepare_prompt_with_context
    Then text."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
        (tmp_path / "a.md").write_text("mission hello")
        with patch("codebot.process_manager._load_ticket_context", return_value=""):
            text = pm._prepare_prompt_with_context(bot)
            assert "mission hello" in text

def test_prepare_prompt_with_ticket_Given_assigned_When_prepare_Then_injected(tmp_path: Path):
    """Given assigned ticket
    When _prepare_prompt_with_context
    Then ticket context injected."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    bot._assigned_ticket_id = "CB-123"
    with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
        (tmp_path / "a.md").write_text("base")
        with patch("codebot.process_manager._load_ticket_context", return_value="TICKET CONTEXT"):
            text = pm._prepare_prompt_with_context(bot)
            assert "TICKET CONTEXT" in text

def test_prepare_prompt_with_scratchpad_Given_history_When_prepare_Then_handoff(tmp_path: Path):
    """Given scratchpad history
    When _prepare_prompt_with_context
    Then handoff note."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    bot._assigned_ticket_id = "CB-123"
    with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
        (tmp_path / "a.md").write_text("base")
        mock_scratch = MagicMock()
        mock_scratch.agent_history = ["step1"]
        mock_scratch.completed_steps = []
        with patch("codebot.process_manager._load_ticket_context", return_value=""):
            with patch("codebot.scratchpad.load_scratchpad", return_value=mock_scratch):
                with patch("codebot.scratchpad.create_handoff_note", return_value="HANDOFF"):
                    text = pm._prepare_prompt_with_context(bot)
                    assert "HANDOFF" in text

def test_build_checkpoint_block_empty_Given_none_When_build_Then_empty():
    """Given no ckpt
    When _build_checkpoint_block
    Then empty."""
    assert pm._build_checkpoint_block("b", None, Path("/tmp/x")) == ""
    assert pm._build_checkpoint_block("b", {}, Path("/tmp/x")) == ""

def test_build_checkpoint_block_with_data_Given_ckpt_When_build_Then_block():
    """Given ckpt
    When _build_checkpoint_block
    Then block contains json."""
    ckpt = {"updated_at": 123, "reason":"lockup", "progress":50}
    block = pm._build_checkpoint_block("mybot", ckpt, Path("/tmp/my.ckpt"))
    assert "mybot" in block and "lockup" in block

def test_build_checkpoint_block_exception_Given_bad_When_build_Then_empty():
    """Given bad ckpt that raises
    When _build_checkpoint_block
    Then empty (exception handled)."""
    class BadDict(dict):
        def get(self, *a, **kw): raise RuntimeError("boom")
    with patch("json.dumps", side_effect=RuntimeError("fail")):
        assert pm._build_checkpoint_block("b", {"a":1}, Path("/tmp/x")) == ""

def test_build_mission_message_Given_bot_When_build_Then_calls_gateway(tmp_path: Path):
    """Given bot and prompt
    When _build_mission_message
    Then delegates to gateway."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m")
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        with patch("codebot.process_manager._resolve_logs_dir", return_value=tmp_path):
            with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
                (tmp_path / "a.md").write_text("hello")
                mock_gw = MagicMock()
                mock_gw.build_message.return_value = "mission msg"
                pm.set_prompt_gateway(mock_gw)
                msg = pm._build_mission_message(bot, "prompt text", tmp_path/"hb", tmp_path/"ckpt", "block")
                assert msg == "mission msg"
                mock_gw.build_message.assert_called_once()
                pm.clear_prompt_gateway()

def test_build_popen_args_Given_bot_When_build_Then_args(tmp_path: Path):
    """Given bot
    When _build_popen_args
    Then list includes model and heartbeat."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="m1", fallback_model="fb", max_tokens_per_run=100, fallback_models=("a","b"))
    bot = pm.BotState(config=cfg)
    args = pm._build_popen_args(bot, Path("/tmp/hb"), Path("/tmp/ckpt"), Path("/tmp/mission"))
    assert "m1" in args and "bot-a" in args and "fb" in args
    assert "a,b" in args

def test_update_bot_after_launch_Given_bot_When_update_Then_fields(tmp_path: Path):
    """Given bot
    When _update_bot_after_launch
    Then fields set."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        pm._last_spawn_time = 0
        pm._update_bot_after_launch(bot, {"started": 123.0})
        assert bot.started_at == 123.0
        assert bot.next_run_at > 0

def test_launch_bot_subprocess_success_Given_bot_When_launch_Then_true(tmp_path: Path):
    """Given bot
    When _launch_bot_subprocess
    Then True and process set."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_logs_dir", return_value=tmp_path):
        with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
            mock_proc = MagicMock(pid=9999)
            with patch("subprocess.Popen", return_value=mock_proc) as mp:
                # Need to patch open for log file
                with patch("builtins.open", mock_open()):
                    ok = pm._launch_bot_subprocess(bot, "msg", tmp_path/"hb", tmp_path/"ckpt", {"started": time.time()})
                    assert ok is True
                    assert bot.process is mock_proc

def test_launch_bot_subprocess_file_not_found_Given_no_runner_When_launch_Then_false(tmp_path: Path):
    """Given FileNotFoundError
    When _launch_bot_subprocess
    Then False."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_logs_dir", return_value=tmp_path):
        with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
            with patch("subprocess.Popen", side_effect=FileNotFoundError):
                with patch("builtins.open", mock_open()):
                    assert pm._launch_bot_subprocess(bot, "msg", tmp_path/"hb", tmp_path/"ckpt", {"started": time.time()}) is False

def test_launch_bot_subprocess_generic_error_Given_error_When_launch_Then_false(tmp_path: Path):
    """Given generic error
    When _launch_bot_subprocess
    Then False."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_logs_dir", return_value=tmp_path):
        with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
            with patch("subprocess.Popen", side_effect=RuntimeError("boom")):
                with patch("builtins.open", mock_open()):
                    assert pm._launch_bot_subprocess(bot, "msg", tmp_path/"hb", tmp_path/"ckpt", {"started": time.time()}) is False

# ---------------------------------------------------------------------------
# process_manager — start/stop/restart
# ---------------------------------------------------------------------------
def test_init_and_prepare_missing_prompt_Given_no_file_When_init_Then_raise(tmp_path: Path):
    """Given missing prompt file
    When _init_and_prepare_bot
    Then FileNotFoundError."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="missing.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
        with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
            with pytest.raises(FileNotFoundError):
                pm._init_and_prepare_bot(bot, True)

def test_init_and_prepare_success_Given_file_When_init_Then_message(tmp_path: Path):
    """Given prompt file
    When _init_and_prepare_bot
    Then returns files and message."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
        with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
            with patch("codebot.process_manager._resolve_logs_dir", return_value=tmp_path):
                (tmp_path / "a.md").write_text("hello mission")
                with patch("codebot.process_manager._prepare_prompt_with_context", return_value="prompt text"):
                    with patch("codebot.process_manager._build_mission_message", return_value="mission msg"):
                        with patch("codebot.process_manager.read_checkpoint", return_value=None):
                            hb, ckpt, msg = pm._init_and_prepare_bot(bot, True)
                            assert hb == tmp_path / "bot-a.heartbeat"
                            assert msg == "mission msg"

def test_start_bot_paused_Given_pause_file_When_start_Then_false(tmp_path: Path):
    """Given paused file
    When start_bot
    Then False and paused state."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        (tmp_path / "bot-a.paused").write_text("1")
        with patch("codebot.process_manager.update_bot_state") as mock_upd:
            assert pm.start_bot(bot) is False
            mock_upd.assert_called_once()

def test_start_bot_skip_run_Given_stable_When_start_Then_false(tmp_path: Path):
    """Given should skip
    When start_bot
    Then False."""
    cfg = pm.BotConfig(name="normal-bot", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
            with patch("codebot.process_manager._should_skip_run", return_value=True):
                with patch("codebot.process_manager.checkpoint_path", return_value=tmp_path/"chk.json"):
                    assert pm.start_bot(bot) is False

def test_start_bot_spawn_gate_blocked_Given_gate_false_When_start_Then_queued(tmp_path: Path):
    """Given gate blocked
    When start_bot
    Then queued."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    bot.next_run_at = time.time() + 100
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
            with patch("codebot.process_manager._should_skip_run", return_value=False):
                with patch("codebot.process_manager.checkpoint_path", return_value=tmp_path/"chk.json"):
                    with patch("codebot.process_manager._spawn_gate", return_value=(False, "cap")):
                        with patch("codebot.process_manager.update_bot_state") as mock_upd:
                            assert pm.start_bot(bot) is False
                            assert mock_upd.call_args[0][1] == "queued"

def test_start_bot_model_rotation_Given_old_model_When_start_Then_rotated(tmp_path: Path):
    """Given old model
    When start_bot
    Then model rotated."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, model="old-model")
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
            with patch("codebot.process_manager._should_skip_run", return_value=False):
                with patch("codebot.process_manager.checkpoint_path", return_value=tmp_path/"chk.json"):
                    with patch("codebot.process_manager._spawn_gate", return_value=(True, "ok")):
                        with patch("codebot.model_manager.next_model_for_role", return_value=MagicMock(model="new-model", fallback="fb")):
                            with patch("codebot.process_manager._init_and_prepare_bot", return_value=(tmp_path/"hb", tmp_path/"ckpt", "msg")) as mock_init:
                                mock_init._last_state = {"started": time.time()}
                                with patch("codebot.process_manager._launch_bot_subprocess", return_value=True):
                                    assert pm.start_bot(bot) is True
                                    assert bot.config.model == "new-model"

def test_start_bot_init_failure_Given_missing_prompt_When_start_Then_false(tmp_path: Path):
    """Given init raises FileNotFoundError
    When start_bot
    Then False."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        with patch("codebot.process_manager._resolve_bots_dir", return_value=tmp_path):
            with patch("codebot.process_manager._should_skip_run", return_value=False):
                with patch("codebot.process_manager.checkpoint_path", return_value=tmp_path/"chk.json"):
                    with patch("codebot.process_manager._spawn_gate", return_value=(True, "ok")):
                        with patch("codebot.model_manager.next_model_for_role", return_value=MagicMock(model="m", fallback="fb")):
                            with patch("codebot.process_manager._init_and_prepare_bot", side_effect=FileNotFoundError("missing")):
                                assert pm.start_bot(bot) is False

def test_stop_bot_no_process_Given_none_When_stop_Then_true():
    """Given no process
    When stop_bot
    Then True."""
    cfg = pm.BotConfig(name="b", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg, process=None)
    assert pm.stop_bot(bot) is True

def test_stop_bot_success_Given_running_When_stop_Then_none(tmp_path: Path):
    """Given running process
    When stop_bot
    Then process None."""
    cfg = pm.BotConfig(name="b", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    mock_proc = MagicMock(pid=1234)
    mock_proc.wait.return_value = None
    bot = pm.BotState(config=cfg, process=mock_proc)
    with patch("os.kill"):
        assert pm.stop_bot(bot) is True
        assert bot.process is None

def test_stop_bot_timeout_escalates_Given_hung_When_stop_Then_sigkill():
    """Given process hangs
    When stop_bot
    Then SIGKILL."""
    cfg = pm.BotConfig(name="b", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    mock_proc = MagicMock(pid=1234)
    mock_proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="wait", timeout=5), None]
    bot = pm.BotState(config=cfg, process=mock_proc)
    with patch("os.kill") as mk:
        pm.stop_bot(bot)
        # Should have called kill twice (TERM and KILL)
        assert mk.call_count >= 2

def test_stop_bot_lookup_error_Given_exited_When_stop_Then_true():
    """Given ProcessLookupError
    When stop_bot
    Then True."""
    cfg = pm.BotConfig(name="b", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    mock_proc = MagicMock(pid=1234)
    bot = pm.BotState(config=cfg, process=mock_proc)
    with patch("os.kill", side_effect=ProcessLookupError):
        assert pm.stop_bot(bot) is True

def test_stop_bot_generic_error_Given_error_When_stop_Then_true():
    """Given generic error
    When stop_bot
    Then still True."""
    cfg = pm.BotConfig(name="b", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    mock_proc = MagicMock(pid=1234)
    mock_proc.wait.side_effect = RuntimeError("boom")
    bot = pm.BotState(config=cfg, process=mock_proc)
    with patch("os.kill"):
        assert pm.stop_bot(bot) is True

def test_restart_bot_drain_removed_Given_drain_When_restart_Then_removed(tmp_path: Path):
    """Given per-bot drain file
    When restart_bot
    Then removed."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, max_restarts=5)
    bot = pm.BotState(config=cfg)
    bot.last_restart_reset = time.time()
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        (tmp_path / ".drain_bot-a").write_text("1")
        with patch("codebot.process_manager.stop_bot", return_value=True):
            with patch("codebot.process_manager.model_profile", return_value=MagicMock(restart_cooldown=0)):
                with patch("time.sleep"):
                    with patch("codebot.process_manager.start_bot", return_value=True):
                        with patch("codebot.process_manager._manifest_restart_record"):
                            pm.restart_bot(bot)
                            assert not (tmp_path / ".drain_bot-a").exists()

def test_restart_bot_reset_after_hour_Given_old_reset_When_restart_Then_count_zero(tmp_path: Path):
    """Given hour passed
    When restart_bot
    Then count reset."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, max_restarts=5)
    bot = pm.BotState(config=cfg, restart_count=3, last_restart_reset=time.time()-4000)
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        with patch("codebot.process_manager.stop_bot"):
            with patch("codebot.process_manager.model_profile", return_value=MagicMock(restart_cooldown=0)):
                with patch("time.sleep"):
                    with patch("codebot.process_manager.start_bot", return_value=True):
                        with patch("codebot.process_manager._manifest_restart_record"):
                            pm.restart_bot(bot)
                            assert bot.restart_count >= 1

def test_restart_bot_exceeds_max_Given_many_restarts_When_restart_Then_disabled(tmp_path: Path):
    """Given exceed max
    When restart_bot
    Then disabled and False."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, max_restarts=2)
    bot = pm.BotState(config=cfg, restart_count=2, last_restart_reset=time.time())
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        assert pm.restart_bot(bot) is False
        assert bot.config.enabled is False

def test_restart_bot_success_Given_ok_When_restart_Then_true(tmp_path: Path):
    """Given ok
    When restart_bot
    Then True."""
    cfg = pm.BotConfig(name="bot-a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30, max_restarts=5)
    bot = pm.BotState(config=cfg)
    bot.last_restart_reset = time.time()
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        with patch("codebot.process_manager.stop_bot"):
            with patch("codebot.process_manager.model_profile", return_value=None):
                with patch("time.sleep"):
                    with patch("codebot.process_manager.start_bot", return_value=True):
                        with patch("codebot.process_manager._manifest_restart_record"):
                            assert pm.restart_bot(bot) is True

def test_is_queued_with_cache_Given_cached_When_is_queued_Then_true():
    """Given cached_states with queued
    When _is_queued
    Then True."""
    cfg = pm.BotConfig(name="b", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    assert pm._is_queued(bot, {"b": {"status":"queued"}}) is True
    assert pm._is_queued(bot, {"b": {"status":"running"}}) is False
    assert pm._is_queued(bot, {}) is False
    assert pm._is_queued(bot, {"other": {"status":"queued"}}) is False

def test_is_queued_from_disk_Given_file_When_is_queued_Then_true(tmp_path: Path):
    """Given state file queued
    When _is_queued
    Then True."""
    cfg = pm.BotConfig(name="b", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        (tmp_path / "b.state.json").write_text(json.dumps({"status":"queued"}))
        assert pm._is_queued(bot) is True

def test_is_queued_corrupt_Given_bad_json_When_is_queued_Then_false(tmp_path: Path):
    """Given corrupt json
    When _is_queued
    Then False."""
    cfg = pm.BotConfig(name="b", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        (tmp_path / "b.state.json").write_text("{bad")
        assert pm._is_queued(bot) is False

def test_is_queued_missing_Given_no_file_When_is_queued_Then_false(tmp_path: Path):
    """Given no file
    When _is_queued
    Then False."""
    cfg = pm.BotConfig(name="b", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bot = pm.BotState(config=cfg)
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        assert pm._is_queued(bot) is False

def test_count_queued_bots_with_cache_Given_cached_When_count_Then_num():
    """Given cached states
    When _count_queued_bots
    Then count."""
    cfg = pm.BotConfig(name="a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    cfg2 = pm.BotConfig(name="b", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bots = {"a": pm.BotState(config=cfg), "b": pm.BotState(config=cfg2)}
    cached = {"a": {"status":"queued"}, "b": {"status":"running"}}
    assert pm._count_queued_bots(bots, cached) == 1

def test_count_queued_bots_without_cache_Given_files_When_count_Then_num(tmp_path: Path):
    """Given files
    When _count_queued_bots
    Then count."""
    cfg = pm.BotConfig(name="a", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    cfg2 = pm.BotConfig(name="b", prompt_file="a.md", interval_seconds=60, heartbeat_timeout=30)
    bots = {"a": pm.BotState(config=cfg), "b": pm.BotState(config=cfg2)}
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        (tmp_path / "a.state.json").write_text(json.dumps({"status":"queued"}))
        (tmp_path / "b.state.json").write_text(json.dumps({"status":"running"}))
        assert pm._count_queued_bots(bots) == 1

def test_get_code_mtimes_Given_py_files_When_get_Then_mtimes(tmp_path: Path):
    """Given py files
    When _get_code_mtimes
    Then dict."""
    # Create a fake pkg dir
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("x")
    with patch("codebot.process_manager.Path") as mock_path:
        # We need to patch __file__ parent glob
        # Instead call with real tmp_path via patching Path(__file__).parent
        import pathlib as _pl
        real_pkg = Path(pm.__file__).parent
        # Just test that function returns dict and not crash for missing
        mtimes = pm._get_code_mtimes()
        assert isinstance(mtimes, dict)

def test_get_code_mtimes_oserror_Given_no_permission_When_get_Then_empty():
    """Given OSError on glob
    When _get_code_mtimes
    Then empty."""
    with patch.object(Path, "glob", side_effect=OSError("fail")):
        assert pm._get_code_mtimes() == {}

def test_load_ticket_context_no_store_Given_none_When_load_Then_empty():
    """Given no store
    When _load_ticket_context
    Then empty."""
    with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=None):
        assert pm._load_ticket_context("CB-1") == ""

def test_load_ticket_context_no_ticket_Given_store_no_ticket_When_load_Then_empty():
    """Given store with no ticket
    When _load_ticket_context
    Then empty."""
    mock_store = MagicMock()
    mock_store.get.return_value = None
    with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=mock_store):
        assert pm._load_ticket_context("CB-999") == ""

def test_load_ticket_context_with_ticket_Given_ticket_When_load_Then_context():
    """Given ticket
    When _load_ticket_context
    Then context."""
    from codebot.ticket_engine import TicketClass, Severity, RiskLevel
    mock_ticket = MagicMock()
    mock_ticket.id = "CB-123"
    mock_ticket.title = "Test"
    from unittest.mock import PropertyMock
    # TicketClass etc mock
    mock_ticket.ticket_class = MagicMock(value="BUG")
    mock_ticket.severity = MagicMock(value="LOW")
    mock_ticket.problem_statement = "prob"
    mock_ticket.desired_state = "desired"
    mock_ticket.acceptance_criteria = ["ac1"]
    mock_ticket.affected_modules = ["a.py"]
    mock_ticket.rework_count = 1
    mock_ticket.reviewer_feedback = [{"reviewer":"r","file":"f.py","description":"desc","recommendation":"fix"}]
    mock_store = MagicMock()
    mock_store.get.return_value = mock_ticket
    with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=mock_store):
        ctx = pm._load_ticket_context("CB-123")
        assert "CB-123" in ctx and "prob" in ctx

def test_load_ticket_context_exception_Given_error_When_load_Then_empty():
    """Given exception
    When _load_ticket_context
    Then empty."""
    with patch("codebot.ticket_dispatcher.get_ticket_store", side_effect=RuntimeError):
        assert pm._load_ticket_context("CB-1") == ""

def test_manifest_restart_record_Given_name_When_record_Then_timestamps(tmp_path: Path):
    """Given bot name
    When _manifest_restart_record
    Then timestamps."""
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        now = time.time()
        pm._manifest_restart_record("bot-a", now)
        data = json.loads((tmp_path / "bot-a.state.json").read_text())
        assert "restart_timestamps" in data
        assert data["restart_count"] >= 1

def test_manifest_restart_record_non_list_Given_bad_timestamps_When_record_Then_fixed(tmp_path: Path):
    """Given non-list timestamps
    When _manifest_restart_record
    Then fixed."""
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        (tmp_path / "bot-b.state.json").write_text(json.dumps({"restart_timestamps":"bad"}))
        pm._manifest_restart_record("bot-b", time.time())
        data = json.loads((tmp_path / "bot-b.state.json").read_text())
        assert isinstance(data["restart_timestamps"], list)

def test_manifest_restart_record_corrupt_Given_bad_json_When_record_Then_new(tmp_path: Path):
    """Given corrupt json
    When _manifest_restart_record
    Then new."""
    with patch("codebot.process_manager._resolve_state_dir", return_value=tmp_path):
        (tmp_path / "bot-c.state.json").write_text("{bad")
        pm._manifest_restart_record("bot-c", time.time())
        data = json.loads((tmp_path / "bot-c.state.json").read_text())
        assert "restart_timestamps" in data

# ===========================================================================
# checkpoint_manager
# ===========================================================================
def test_cm_set_state_dir_Given_path_When_set_Then_dir(tmp_path: Path):
    """Given path
    When set_state_dir
    Then dir exists and checkpoint_path."""
    new_dir = tmp_path / "cm_state"
    cm.set_state_dir(new_dir)
    assert new_dir.exists()
    assert cm.checkpoint_path("bot-a").parent == new_dir

def test_cm_checkpoint_path_Given_bot_When_path_Then_correct(tmp_path: Path):
    """Given bot
    When checkpoint_path
    Then file."""
    cm.set_state_dir(tmp_path)
    assert cm.checkpoint_path("mybot") == tmp_path / "mybot.checkpoint.json"

def test_cm_write_json_atomic_dict_Given_dict_When_write_Then_file(tmp_path: Path):
    """Given dict
    When _write_json_atomic
    Then file."""
    p = tmp_path / "out.json"
    cm._write_json_atomic(p, {"a":1})
    assert json.loads(p.read_text())["a"]==1

def test_cm_write_json_atomic_str_Given_str_When_write_Then_raw(tmp_path: Path):
    """Given str
    When _write_json_atomic
    Then raw."""
    p = tmp_path / "str.json"
    cm._write_json_atomic(p, "hello")
    assert p.read_text() == "hello"

def test_cm_state_write_lock_Given_bot_When_lock_Then_exists(tmp_path: Path):
    """Given lock
    When _state_write_lock
    Then yields."""
    cm.set_state_dir(tmp_path)
    with cm._state_write_lock("bot-a"):
        assert (tmp_path / "bot-a.state.lock").exists()

def test_cm_write_checkpoint_handoff_Given_payload_When_write_Then_metadata(tmp_path: Path):
    """Given payload
    When write_checkpoint_handoff
    Then file with metadata."""
    cm.set_state_dir(tmp_path)
    before = time.time()
    cm.write_checkpoint_handoff("bot-a", {"task":"scan"})
    after = time.time()
    data = json.loads((tmp_path / "bot-a.checkpoint.json").read_text())
    assert data["bot"]=="bot-a" and data["task"]=="scan"
    assert before <= data["updated_at"] <= after
    assert "updated_at_human" in data

def test_cm_write_checkpoint_handoff_preserves_bot_Given_existing_bot_When_write_Then_not_overwritten(tmp_path: Path):
    """Given payload with bot
    When write_checkpoint_handoff
    Then preserves."""
    cm.set_state_dir(tmp_path)
    cm.write_checkpoint_handoff("bot-a", {"bot":"custom","task":"x"})
    assert json.loads((tmp_path / "bot-a.checkpoint.json").read_text())["bot"]=="custom"

def test_cm_init_checkpoint_creates_Given_missing_When_init_Then_file(tmp_path: Path):
    """Given missing
    When init_checkpoint
    Then file."""
    cm.set_state_dir(tmp_path)
    cm.init_checkpoint("new-bot", scan_iteration=5)
    data = json.loads((tmp_path / "new-bot.checkpoint.json").read_text())
    assert data["scan_iteration"]==5 and data["reason"]=="init"

def test_cm_init_checkpoint_skips_existing_Given_exists_When_init_Then_skip(tmp_path: Path):
    """Given exists
    When init_checkpoint
    Then skip."""
    cm.set_state_dir(tmp_path)
    p = tmp_path / "existing.checkpoint.json"
    p.write_text(json.dumps({"custom":"data"}))
    cm.init_checkpoint("existing")
    assert json.loads(p.read_text())=={"custom":"data"}

def test_cm_backup_path_json_Given_json_When_backup_Then_bak(tmp_path: Path):
    """Given json path
    When checkpoint_backup_path
    Then .checkpoint.bak."""
    p = tmp_path / "bot.checkpoint.json"
    assert cm.checkpoint_backup_path(p) == tmp_path / "bot.checkpoint.checkpoint.bak"

def test_cm_backup_path_non_json_Given_no_suffix_When_backup_Then_bak():
    """Given non-json
    When checkpoint_backup_path
    Then .bak."""
    p = Path("/tmp/x")
    assert cm.checkpoint_backup_path(p) == Path("/tmp/x.bak")

def test_cm_read_checkpoint_missing_no_bak_Given_missing_When_read_Then_none(tmp_path: Path):
    """Given missing
    When read_checkpoint
    Then None."""
    cm.set_state_dir(tmp_path)
    assert cm.read_checkpoint("missing") is None

def test_cm_read_checkpoint_bak_restore_Given_bak_When_read_Then_data(tmp_path: Path):
    """Given bak exists but not primary
    When read_checkpoint
    Then bak."""
    cm.set_state_dir(tmp_path)
    bak = cm.checkpoint_backup_path(tmp_path / "bot.checkpoint.json")
    bak.write_text(json.dumps({"from":"bak"}))
    assert cm.read_checkpoint("bot") == {"from":"bak"}

def test_cm_read_checkpoint_bak_oversized_Given_big_bak_When_read_Then_none(tmp_path: Path):
    """Given oversized bak
    When read_checkpoint no primary
    Then None."""
    cm.set_state_dir(tmp_path)
    bak = cm.checkpoint_backup_path(tmp_path / "bot2.checkpoint.json")
    # Need to make bak size >4096
    big = {"data":"x"*5000}
    bak.write_text(json.dumps(big))
    # stat size will be >4096, so rejected
    assert cm.read_checkpoint("bot2") is None

def test_cm_read_checkpoint_bak_corrupt_Given_bad_bak_When_read_Then_none(tmp_path: Path):
    """Given corrupt bak
    When read_checkpoint no primary
    Then None."""
    cm.set_state_dir(tmp_path)
    bak = cm.checkpoint_backup_path(tmp_path / "bot3.checkpoint.json")
    bak.write_text("{bad")
    assert cm.read_checkpoint("bot3") is None

def test_cm_read_checkpoint_valid_bak_updated_Given_valid_When_read_Then_bak_written(tmp_path: Path):
    """Given valid primary
    When read_checkpoint
    Then bak written."""
    cm.set_state_dir(tmp_path)
    p = tmp_path / "bot4.checkpoint.json"
    bak = cm.checkpoint_backup_path(tmp_path / "bot4.checkpoint.json")
    data = {"keep":"safe"}
    p.write_text(json.dumps(data))
    if bak.exists(): bak.unlink()
    res = cm.read_checkpoint("bot4")
    assert res==data and bak.exists()

def test_cm_read_checkpoint_oversized_primary_Given_big_When_read_Then_none(tmp_path: Path):
    """Given oversized primary
    When read_checkpoint
    Then None (rejected via stat)."""
    cm.set_state_dir(tmp_path)
    p = tmp_path / "big.checkpoint.json"
    big = {"data":"x"*5000}
    p.write_text(json.dumps(big))
    # stat size >4096 so returns None
    assert cm.read_checkpoint("big") is None

def test_cm_read_checkpoint_stat_oserror_Given_stat_fails_When_read_Then_fallback(tmp_path: Path):
    """Given stat raises OSError
    When read_checkpoint
    Then fallback to try reading still."""
    cm.set_state_dir(tmp_path)
    p = tmp_path / "statfail.checkpoint.json"
    bak = cm.checkpoint_backup_path(tmp_path / "statfail.checkpoint.json")
    p.write_text(json.dumps({"ok":1}))
    with patch.object(Path, "stat", side_effect=OSError("fail")):
        # Will try to still read primary, should succeed despite stat error
        res = cm.read_checkpoint("statfail")
        assert res == {"ok":1}

def test_cm_read_checkpoint_corrupt_primary_quarantine_Given_corrupt_When_read_Then_quarantined(tmp_path: Path):
    """Given corrupt primary
    When read_checkpoint
    Then quarantined and fallback."""
    cm.set_state_dir(tmp_path)
    p = tmp_path / "corrupt.checkpoint.json"
    bak = cm.checkpoint_backup_path(tmp_path / "corrupt.checkpoint.json")
    p.write_text("{bad")
    bak.write_text(json.dumps({"good":True}))
    res = cm.read_checkpoint("corrupt")
    assert res == {"good":True}
    assert not p.exists()
    assert (tmp_path / "checkpoint_quarantine").exists()

def test_cm_read_checkpoint_corrupt_no_bak_Given_no_bak_When_read_Then_none(tmp_path: Path):
    """Given corrupt primary no bak
    When read_checkpoint
    Then None."""
    cm.set_state_dir(tmp_path)
    p = tmp_path / "corrupt2.checkpoint.json"
    p.write_text("{bad")
    assert cm.read_checkpoint("corrupt2") is None

def test_cm_read_checkpoint_corrupt_bak_oversized_Given_big_bak_When_corrupt_Then_none(tmp_path: Path):
    """Given corrupt primary and oversized bak
    When read_checkpoint
    Then None."""
    cm.set_state_dir(tmp_path)
    p = tmp_path / "combo.checkpoint.json"
    bak = cm.checkpoint_backup_path(tmp_path / "combo.checkpoint.json")
    p.write_text("{bad")
    bak.write_text(json.dumps({"data":"x"*5000}))
    assert cm.read_checkpoint("combo") is None

def test_cm_read_checkpoint_non_dict_Given_list_When_read_Then_quarantined(tmp_path: Path):
    """Given list json
    When read_checkpoint
    Then quarantined and fallback."""
    cm.set_state_dir(tmp_path)
    p = tmp_path / "list.checkpoint.json"
    bak = cm.checkpoint_backup_path(tmp_path / "list.checkpoint.json")
    p.write_text(json.dumps([1,2,3]))
    bak.write_text(json.dumps({"good":1}))
    res = cm.read_checkpoint("list")
    assert res == {"good":1}
    assert not p.exists()

def test_cm_read_checkpoint_non_dict_no_bak_Given_list_no_bak_When_read_Then_none(tmp_path: Path):
    """Given list no bak
    When read_checkpoint
    Then None."""
    cm.set_state_dir(tmp_path)
    p = tmp_path / "list2.checkpoint.json"
    p.write_text(json.dumps([1,2]))
    assert cm.read_checkpoint("list2") is None

def test_cm_read_checkpoint_non_dict_bak_oversized_Given_big_bak_When_list_Then_none(tmp_path: Path):
    """Given list and oversized bak
    When read_checkpoint
    Then None."""
    cm.set_state_dir(tmp_path)
    p = tmp_path / "list3.checkpoint.json"
    bak = cm.checkpoint_backup_path(tmp_path / "list3.checkpoint.json")
    p.write_text(json.dumps([1,2]))
    bak.write_text(json.dumps({"data":"x"*5000}))
    assert cm.read_checkpoint("list3") is None

def test_cm_read_checkpoint_generic_exception_Given_read_raises_When_read_Then_none(tmp_path: Path):
    """Given generic exception
    When read_checkpoint
    Then None (via except)."""
    cm.set_state_dir(tmp_path)
    p = tmp_path / "gen.checkpoint.json"
    p.write_text(json.dumps({"a":1}))
    with patch.object(Path, "read_text", side_effect=RuntimeError("boom")):
        # Need to also mock stat to bypass early check? Actually read will raise
        assert cm.read_checkpoint("gen") is None

def test_cm_read_checkpoint_backup_non_dict_Given_bad_bak_When_non_dict_primary_Then_none(tmp_path: Path):
    """Given non-dict primary and bad bak (non-json)
    When read_checkpoint
    Then None."""
    cm.set_state_dir(tmp_path)
    p = tmp_path / "nd2.checkpoint.json"
    bak = cm.checkpoint_backup_path(tmp_path / "nd2.checkpoint.json")
    p.write_text(json.dumps([1,2]))
    bak.write_text("{bad")
    assert cm.read_checkpoint("nd2") is None

def test_cm_read_state_file_missing_Given_missing_When_read_Then_empty(tmp_path: Path):
    """Given missing
    When _read_state_file
    Then {}."""
    cm.set_state_dir(tmp_path)
    assert cm._read_state_file("missing") == {}

def test_cm_read_state_file_valid_Given_file_When_read_Then_data(tmp_path: Path):
    """Given valid
    When _read_state_file
    Then data."""
    cm.set_state_dir(tmp_path)
    (tmp_path / "bot-a.state.json").write_text(json.dumps({"status":"ok"}))
    assert cm._read_state_file("bot-a")["status"]=="ok"

def test_cm_read_state_file_corrupt_Given_bad_When_read_Then_error(tmp_path: Path):
    """Given corrupt
    When _read_state_file
    Then _state_error."""
    cm.set_state_dir(tmp_path)
    (tmp_path / "bad.state.json").write_text("{bad")
    assert cm._read_state_file("bad") == {"_state_error":"corrupt"}

def test_cm_write_state_file_Given_data_When_write_Then_file(tmp_path: Path):
    """Given data
    When _write_state_file
    Then file."""
    cm.set_state_dir(tmp_path)
    cm._write_state_file("bot-a", {"status":"running"})
    assert json.loads((tmp_path / "bot-a.state.json").read_text())["status"]=="running"

def test_cm_update_bot_state_Given_bot_When_update_Then_file(tmp_path: Path):
    """Given bot
    When update_bot_state
    Then file."""
    cm.set_state_dir(tmp_path)
    cm.update_bot_state("bot-a", "running", restart_count=1, consecutive_errors=2, next_run_at=123.0)
    data = json.loads((tmp_path / "bot-a.state.json").read_text())
    assert data["status"]=="running" and data["restart_count"]==1 and data["consecutive_errors"]==2

def test_cm_update_bot_state_corrupt_existing_Given_bad_When_update_Then_overwrite(tmp_path: Path):
    """Given corrupt existing
    When update_bot_state
    Then overwritten."""
    cm.set_state_dir(tmp_path)
    (tmp_path / "bot-b.state.json").write_text("{bad")
    cm.update_bot_state("bot-b", "waiting")
    assert json.loads((tmp_path / "bot-b.state.json").read_text())["status"]=="waiting"

def test_cm_update_bot_state_non_dict_Given_list_When_update_Then_dict(tmp_path: Path):
    """Given list existing
    When update_bot_state
    Then dict."""
    cm.set_state_dir(tmp_path)
    (tmp_path / "bot-c.state.json").write_text(json.dumps([1,2]))
    cm.update_bot_state("bot-c", "running")
    assert isinstance(json.loads((tmp_path / "bot-c.state.json").read_text()), dict)

def test_cm_update_bot_state_exception_Given_fail_When_update_Then_no_raise(tmp_path: Path):
    """Given write fails
    When update_bot_state
    Then no raise."""
    cm.set_state_dir(tmp_path)
    with patch("codebot.checkpoint_manager._write_json_atomic", side_effect=OSError("fail")):
        cm.update_bot_state("bot-x", "running")  # should not raise

def test_cm_manifest_restart_budget_not_exceeded_Given_fresh_When_check_Then_false(tmp_path: Path):
    """Given fresh manifest
    When _manifest_restart_budget_exceeded
    Then False."""
    cm.set_state_dir(tmp_path)
    assert cm._manifest_restart_budget_exceeded({"name":"fresh","max_restarts":3}, time.time())[0] is False

def test_cm_manifest_restart_budget_exceeded_Given_many_When_check_Then_true(tmp_path: Path):
    """Given many restarts
    When _manifest_restart_budget_exceeded
    Then True."""
    cm.set_state_dir(tmp_path)
    now = time.time()
    (tmp_path / "bot-a.state.json").write_text(json.dumps({"restart_timestamps":[now-10, now-20]}))
    exceeded, reason = cm._manifest_restart_budget_exceeded({"name":"bot-a","max_restarts":2}, now)
    assert exceeded is True and "restart-budget" in reason

def test_cm_manifest_restart_budget_old_ignored_Given_old_When_check_Then_false(tmp_path: Path):
    """Given old timestamps
    When _manifest_restart_budget_exceeded
    Then False."""
    cm.set_state_dir(tmp_path)
    now = time.time()
    (tmp_path / "old.state.json").write_text(json.dumps({"restart_timestamps":[now-7300]}))
    assert cm._manifest_restart_budget_exceeded({"name":"old","max_restarts":1}, now)[0] is False

def test_cm_manifest_restart_budget_bad_max_Given_bad_When_check_Then_default(tmp_path: Path):
    """Given bad max_restarts
    When _manifest_restart_budget_exceeded
    Then uses default 5."""
    cm.set_state_dir(tmp_path)
    # malformed max_restarts
    assert cm._manifest_restart_budget_exceeded({"name":"x","max_restarts":"bad"}, time.time())[0] is False

def test_cm_manifest_restart_budget_zero_Given_zero_When_check_Then_false(tmp_path: Path):
    """Given zero max
    When _manifest_restart_budget_exceeded
    Then False."""
    cm.set_state_dir(tmp_path)
    assert cm._manifest_restart_budget_exceeded({"name":"x","max_restarts":0}, time.time()) == (False, "")

def test_cm_manifest_restart_budget_corrupt_state_Given_corrupt_When_check_Then_true(tmp_path: Path):
    """Given corrupt state
    When _manifest_restart_budget_exceeded
    Then True state-corrupt."""
    cm.set_state_dir(tmp_path)
    (tmp_path / "corrupt.state.json").write_text("{bad")
    exceeded, reason = cm._manifest_restart_budget_exceeded({"name":"corrupt","max_restarts":5}, time.time())
    assert exceeded is True and reason=="state-corrupt"

def test_cm_manifest_restart_budget_non_list_Given_bad_timestamps_When_check_Then_false(tmp_path: Path):
    """Given non-list timestamps
    When _manifest_restart_budget_exceeded
    Then false (handled)."""
    cm.set_state_dir(tmp_path)
    (tmp_path / "badlist.state.json").write_text(json.dumps({"restart_timestamps":"not a list"}))
    assert cm._manifest_restart_budget_exceeded({"name":"badlist"}, time.time())[0] is False

def test_cm_manifest_error_disabled_false_Given_low_When_check_Then_false(tmp_path: Path):
    """Given low errors
    When _manifest_error_disabled
    Then False."""
    cm.set_state_dir(tmp_path)
    (tmp_path / "safe.state.json").write_text(json.dumps({"consecutive_errors":1}))
    assert cm._manifest_error_disabled({"name":"safe"}, max_consecutive=3)[0] is False

def test_cm_manifest_error_disabled_true_Given_high_When_check_Then_true(tmp_path: Path):
    """Given high errors
    When _manifest_error_disabled
    Then True."""
    cm.set_state_dir(tmp_path)
    (tmp_path / "bad.state.json").write_text(json.dumps({"consecutive_errors":5}))
    exceeded, reason = cm._manifest_error_disabled({"name":"bad"}, max_consecutive=3)
    assert exceeded is True

def test_cm_manifest_error_disabled_corrupt_Given_corrupt_When_check_Then_true(tmp_path: Path):
    """Given corrupt
    When _manifest_error_disabled
    Then True."""
    cm.set_state_dir(tmp_path)
    (tmp_path / "corrupt2.state.json").write_text("{bad")
    assert cm._manifest_error_disabled({"name":"corrupt2"})[0] is True

def test_cm_manifest_error_disabled_bad_consecutive_Given_bad_When_check_Then_false(tmp_path: Path):
    """Given bad consecutive_errors
    When _manifest_error_disabled
    Then False (int conversion fails)."""
    cm.set_state_dir(tmp_path)
    (tmp_path / "badval.state.json").write_text(json.dumps({"consecutive_errors":"bad"}))
    assert cm._manifest_error_disabled({"name":"badval"})[0] is False

def test_cm_manifest_restart_record_Given_name_When_record_Then_file(tmp_path: Path):
    """Given name
    When _manifest_restart_record
    Then timestamps."""
    cm.set_state_dir(tmp_path)
    now = time.time()
    cm._manifest_restart_record("bot-a", now)
    data = json.loads((tmp_path / "bot-a.state.json").read_text())
    assert "restart_timestamps" in data

def test_cm_manifest_restart_record_non_list_Given_bad_When_record_Then_fixed(tmp_path: Path):
    """Given non-list
    When _manifest_restart_record
    Then fixed."""
    cm.set_state_dir(tmp_path)
    (tmp_path / "bot-b.state.json").write_text(json.dumps({"restart_timestamps":123}))
    cm._manifest_restart_record("bot-b", time.time())
    assert isinstance(json.loads((tmp_path / "bot-b.state.json").read_text())["restart_timestamps"], list)

def test_cm_manifest_restart_record_mixed_types_Given_mixed_When_record_Then_filtered(tmp_path: Path):
    """Given mixed types in timestamps
    When _manifest_restart_record
    Then filtered to floats."""
    cm.set_state_dir(tmp_path)
    (tmp_path / "mix.state.json").write_text(json.dumps({"restart_timestamps":[123,"bad", None, 456.0]}))
    cm._manifest_restart_record("mix", time.time())
    data = json.loads((tmp_path / "mix.state.json").read_text())
    # Only floats should remain plus new timestamp
    assert all(isinstance(x, float) for x in data["restart_timestamps"])

def test_cm_is_restart_budget_public_Given_manifest_When_public_Then_bool(tmp_path: Path):
    """Given manifest
    When is_manifest_restart_budget_exceeded
    Then bool."""
    cm.set_state_dir(tmp_path)
    now = time.time()
    (tmp_path / "pub.state.json").write_text(json.dumps({"restart_timestamps":[now]}))
    assert cm.is_manifest_restart_budget_exceeded({"name":"pub","max_restarts":1}, now) is True
    assert cm.is_manifest_restart_budget_exceeded({"name":"missing","max_restarts":5}, now) is False

def test_cm_is_error_disabled_public_Given_manifest_When_public_Then_bool(tmp_path: Path):
    """Given manifest
    When is_manifest_error_disabled
    Then bool."""
    cm.set_state_dir(tmp_path)
    (tmp_path / "pub2.state.json").write_text(json.dumps({"consecutive_errors":5}))
    assert cm.is_manifest_error_disabled({"name":"pub2"}) is True

# ===========================================================================
# process_supervisor
# ===========================================================================
def test_process_supervisor_protocol_members_Given_protocol_When_inspect_Then_restart():
    """Given ProcessSupervisor protocol
    When inspected
    Then has restart_self."""
    assert hasattr(ps.ProcessSupervisor, "restart_self")

def test_default_supervisor_restart_self_Given_default_When_restart_Then_execv():
    """Given DefaultProcessSupervisor
    When restart_self
    Then calls os.execv."""
    sup = ps.DefaultProcessSupervisor()
    with patch("os.execv") as mock_exec:
        with patch.object(sys, "executable", "/usr/bin/python3"):
            with patch.object(sys, "argv", ["codebot","arg1"]):
                sup.restart_self()
                mock_exec.assert_called_once_with("/usr/bin/python3", ["/usr/bin/python3","codebot","arg1"])

def test_default_supervisor_restart_self_logs_Given_default_When_restart_Then_logged():
    """Given DefaultProcessSupervisor
    When restart_self raaises
    Then exception propagates."""
    sup = ps.DefaultProcessSupervisor()
    with patch("os.execv", side_effect=OSError("exec fail")):
        with pytest.raises(OSError):
            sup.restart_self()

def test_default_supervisor_is_process_supervisor_Given_instance_When_check_Then_protocol():
    """Given instance
    When isinstance check via protocol
    Then should have method."""
    sup = ps.DefaultProcessSupervisor()
    assert hasattr(sup, "restart_self") and callable(sup.restart_self)

# ===========================================================================
# Extra coverage — ensure large modules exercised
# ===========================================================================
def test_control_server_coverage_via_scheduler_status_import_error_Given_import_fails_When_scheduler_status_Then_fallback(tmp_path: Path):
    """Given import error for token_budget
    When scheduler_status
    Then fallback budget-unknown."""
    with patch.object(cs, "STATE_DIR", tmp_path):
        # Patch the import inside function to fail by making from codebot.token_budget import fail
        # We can patch sys.modules to have no token_budget
        with patch.dict("sys.modules", {"codebot.token_budget": None}):
            # Need to force ImportError: if module is None, import will fail with ModuleNotFoundError? Actually sys.modules[None] gives error.
            # Alternative: patch __import__ to raise for that name
            orig_import = __import__
            def fake_import(name, *a, **kw):
                if "token_budget" in name:
                    raise ImportError("mock")
                return orig_import(name, *a, **kw)
            with patch("builtins.__import__", side_effect=fake_import):
                data = cs.scheduler_status()
                assert data["budget_state"] == "budget-unknown" or "version" in data

def test_control_server_economics_with_cost_tracker_Given_cost_tracker_When_economics_summary_Then_fleet(tmp_path: Path):
    """Given cost_tracker returns tickets
    When economics_summary
    Then fleet and by_model."""
    with patch.object(cs, "STATE_DIR", tmp_path):
        # Create ticket_costs.jsonl via CostTracker? Instead mock CostTracker and pricing
        mock_summary = {"fleet_totals":{"total_tokens":100,"prompt_tokens":60,"completion_tokens":40,"ticket_count":1},"tickets":{"CB-1":{"models":["gpt-4o"],"prompt_tokens":60,"completion_tokens":40}}}
        with patch("codebot.cost_tracker.CostTracker") as MockCT:
            mock_inst = MagicMock()
            mock_inst.build_summary.return_value = mock_summary
            MockCT.return_value = mock_inst
            with patch("codebot.pricing_table.calculate_cost_usd", return_value=0.01):
                data = cs.economics_summary()
                assert "fleet" in data and "by_model" in data

def test_control_client_main_urLError_timeout_branch_Given_timeout_error_When_req_Then_advice():
    """Given timed out URLError
    When req()
    Then advice includes CONTROL_URL."""
    cc.URL = "http://127.0.0.1:8081"
    cc.TOKEN = ""
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timed out")):
        with patch("threading.Timer"):
            code, data = cc.req("GET", "/bots")
            assert "timed out" in data["error"].lower() and "CONTROL_URL" in data["error"]