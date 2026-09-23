"""Security tests for process injection via crafted bot names - CB-8391374F9FA2

Comprehensive test suite verifying the control server rejects bot names
containing shell metacharacters (;, |, $, `) or regex metacharacters
(., *, +, ?, ^, $, [, ]) and that pkill patterns are safely escaped.

Acceptance criteria:
- names with semicolons, pipes, backticks rejected
- names with regex chars escaped correctly
- valid names (alphanumeric+hyphen+underscore) accepted
- pkill pattern injection attempts fail
- BOT_REGISTRY validation enforced
- re.escape() behavior correct
"""
from __future__ import annotations

import json
import re
import subprocess
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from codebot.control_server import BOT_NAME_PATTERN, validate_bot_name


# ---------------------------------------------------------------------------
# Shell injection vectors — validate_bot_name must reject
# ---------------------------------------------------------------------------
class TestShellInjectionVectors:
    """Shell metacharacters (;, |, $, `, &, $(), ...) must be rejected."""

    @pytest.mark.parametrize("name", [
        "evil;rm -rf /",
        "test; echo pwned",
        ";",
        "a;b",
        "bot;whoami",
    ])
    def test_semicolon_rejected(self, name):
        assert validate_bot_name(name) is False, f"semicolon name '{name}' should be rejected"

    @pytest.mark.parametrize("name", [
        "evil|cat /etc/passwd",
        "a|b",
        "|",
        "bot|whoami",
        "test || evil",
    ])
    def test_pipe_rejected(self, name):
        assert validate_bot_name(name) is False, f"pipe name '{name}' should be rejected"

    @pytest.mark.parametrize("name", [
        "evil`whoami`",
        "`",
        "a`b`",
        "test`id`",
        "bot`echo hi`",
    ])
    def test_backtick_rejected(self, name):
        assert validate_bot_name(name) is False, f"backtick name '{name}' should be rejected"

    @pytest.mark.parametrize("name", [
        "evil$(rm -rf /)",
        "$HOME",
        "$",
        "a$b",
        "$(whoami)",
        "${PATH}",
        "test$var",
    ])
    def test_dollar_sign_rejected(self, name):
        assert validate_bot_name(name) is False, f"dollar name '{name}' should be rejected"

    @pytest.mark.parametrize("name", [
        "evil & rm",
        "a&b",
        "&",
        "test && evil",
        "bot&echo",
    ])
    def test_ampersand_rejected(self, name):
        assert validate_bot_name(name) is False

    @pytest.mark.parametrize("name", [
        "evil\nrm",
        "a\nb",
        "test\nevil",
        "evil\rcarriage",
        "a\tb",
        "a b",
        "test evil",
    ])
    def test_whitespace_and_newline_rejected(self, name):
        assert validate_bot_name(name) is False

    @pytest.mark.parametrize("name", [
        "evil$(cat /etc/passwd)",
        "test; rm -rf /tmp/*",
        "a|b;c`d`$e",
        "../../etc/passwd",
        "bot;whoami|cat",
    ])
    def test_combined_shell_payloads_rejected(self, name):
        assert validate_bot_name(name) is False


# ---------------------------------------------------------------------------
# Regex injection vectors — validate_bot_name must reject
# ---------------------------------------------------------------------------
class TestRegexInjectionVectors:
    """Regex metacharacters (., *, +, ?, ^, $, [, ]) must be rejected."""

    @pytest.mark.parametrize("name", [
        "evil.*",
        ".",
        "a.b",
        "bot.name",
        "..",
        "test.",
    ])
    def test_dot_rejected(self, name):
        assert validate_bot_name(name) is False, f"dot name '{name}' should be rejected"

    @pytest.mark.parametrize("name", [
        "evil*",
        "*",
        "a*b",
        ".*",
        "test*",
        "a**b",
    ])
    def test_asterisk_rejected(self, name):
        assert validate_bot_name(name) is False

    @pytest.mark.parametrize("name", [
        "evil+",
        "+",
        "a+b",
        ".+",
        "test+",
    ])
    def test_plus_rejected(self, name):
        assert validate_bot_name(name) is False

    @pytest.mark.parametrize("name", [
        "evil?",
        "?",
        "a?b",
        "test?",
        "foo?bar",
    ])
    def test_question_mark_rejected(self, name):
        assert validate_bot_name(name) is False

    @pytest.mark.parametrize("name", [
        "^root",
        "^",
        "a^b",
        "^test$",
        "evil^",
    ])
    def test_caret_rejected(self, name):
        assert validate_bot_name(name) is False

    @pytest.mark.parametrize("name", [
        "test$",
        "$",
        "a$b",  # also shell injection
        "evil$",
        "^$",
    ])
    def test_dollar_as_regex_rejected(self, name):
        assert validate_bot_name(name) is False

    @pytest.mark.parametrize("name", [
        "[abc]",
        "[",
        "]",
        "a[bc]d",
        "test[0-9]",
        "evil[",
        "evil]",
        "[]",
    ])
    def test_brackets_rejected(self, name):
        assert validate_bot_name(name) is False

    @pytest.mark.parametrize("name", [
        "(.*)",
        "(",
        ")",
        "a(b)c",
        "{1,3}",
        "{",
        "}",
        "a{100}",
        "a|b",
        "|",
        "\\",
        "a\\b",
    ])
    def test_other_regex_metachars_rejected(self, name):
        assert validate_bot_name(name) is False, f"regex metachar name '{name}' should be rejected"

    @pytest.mark.parametrize("name", [
        ".*",
        ".+",
        "(.*)",
        "a|b",
        "[abc]",
        "^root$",
        "foo?bar",
        "a{100}",
        "test; rm -rf /",
        "evil|cat",
        "bad`cmd`",
        "$HOME",
        "a/b",
        "a\\b",
        "name with spaces",
        "",
    ])
    def test_all_ticket_listed_metachars_rejected(self, name):
        """Every metacharacter explicitly listed in the ticket must be rejected."""
        assert validate_bot_name(name) is False


# ---------------------------------------------------------------------------
# Valid names — alphanumeric + hyphen + underscore accepted
# ---------------------------------------------------------------------------
class TestValidNamesAccepted:
    """Valid names per ^[a-zA-Z0-9_-]+$ must be accepted."""

    @pytest.mark.parametrize("name", [
        "issues",
        "features",
        "bug_hunter",
        "security-auditor",
        "bot123",
        "BotName",
        "a",
        "A",
        "0",
        "a-b_c",
        "my-bot_123",
        "ABC",
        "abc123",
        "test-bot",
        "test_bot",
        "worker-1",
        "worker_2",
        "a" * 64,
        "UPPERCASE",
        "lowercase",
        "MixedCase123",
        "with-hyphen",
        "with_underscore",
        "123numeric",
        "bot-1_2-3",
    ])
    def test_valid_names_accepted(self, name):
        assert validate_bot_name(name) is True, f"valid name '{name}' should be accepted"

    def test_bot_name_pattern_matches_spec(self):
        """BOT_NAME_PATTERN must equal the secure pattern ^[a-zA-Z0-9_-]+$."""
        assert BOT_NAME_PATTERN.pattern == r"^[a-zA-Z0-9_-]+$"

    def test_empty_string_rejected(self):
        assert validate_bot_name("") is False

    def test_non_string_rejected(self):
        assert validate_bot_name(None) is False  # type: ignore[arg-type]
        assert validate_bot_name(123) is False  # type: ignore[arg-type]
        assert validate_bot_name([]) is False  # type: ignore[arg-type]
        assert validate_bot_name({}) is False  # type: ignore[arg-type]

    def test_slash_rejected(self):
        assert validate_bot_name("a/b") is False
        assert validate_bot_name("bot/name") is False

    def test_path_traversal_rejected(self):
        assert validate_bot_name("../etc/passwd") is False
        assert validate_bot_name("..") is False
        assert validate_bot_name("./bot") is False

    def test_unicode_rejected(self):
        # Pattern only allows ASCII alphanumeric + - _
        assert validate_bot_name("böt") is False
        assert validate_bot_name("bot™") is False

    def test_hyphen_and_underscore_only_accepted(self):
        assert validate_bot_name("-") is True
        assert validate_bot_name("_") is True
        assert validate_bot_name("-_-") is True
        assert validate_bot_name("___") is True
        assert validate_bot_name("---") is True
        # but purely special chars still rejected
        assert validate_bot_name(".") is False
        assert validate_bot_name(";") is False


# ---------------------------------------------------------------------------
# re.escape() behavior — verify escaping correctness
# ---------------------------------------------------------------------------
class TestReEscapeBehavior:
    """Verify re.escape neutralizes metacharacters for safe pkill patterns."""

    def test_re_escape_dot_star(self):
        malicious = ".*"
        escaped = re.escape(malicious)
        assert escaped == r"\.\*"
        assert not re.match(escaped, "sleep 100")
        assert not re.match(escaped, "orchestrator.py")
        assert re.match(escaped, ".*")

    def test_re_escape_preserves_valid_names(self):
        valid_names = ["issues", "features", "bug_hunter", "security-auditor"]
        for name in valid_names:
            escaped = re.escape(name)
            pattern = f"api_runner\\.py {escaped}"
            test_cmdline = f"python3 api_runner.py {name}"
            assert re.search(pattern, test_cmdline), (
                f"Escaped pattern '{pattern}' failed to match '{test_cmdline}'"
            )

    @pytest.mark.parametrize("raw,escaped_literal", [
        (".*", r"\.\*"),
        (".+", r"\.\+"),
        ("a|b", r"a\|b"),
        ("[abc]", r"\[abc\]"),
        ("^root$", r"\^root\$"),
        ("foo?bar", r"foo\?bar"),
        ("a{100}", r"a\{100\}"),
        ("test; rm", r"test\;\ rm"),
        ("a`b`", r"a\`b\`"),
        ("$HOME", r"\$HOME"),
    ])
    def test_re_escape_produces_literal_match(self, raw, escaped_literal):
        escaped = re.escape(raw)
        # Escaped pattern must match the literal string
        assert re.fullmatch(escaped, raw), f"escaped '{escaped}' should match literal '{raw}'"
        # Escaped pattern must NOT match unrelated string
        assert not re.fullmatch(escaped, "completely_different_string_xyz")

    def test_re_escape_from_control_server_module(self):
        """control_server's imported re.escape must behave identically."""
        from codebot.control_server import re as cs_re

        for raw in [".*", "a|b", "[test]", "^$", "foo;bar"]:
            assert cs_re.escape(raw) == re.escape(raw)

    def test_various_regex_metachars_escaped(self):
        dangerous = [".*", ".+", "(.*)", "a|b", "[abc]", "^root$", "foo?bar", "a{100}"]
        for pat in dangerous:
            escaped = re.escape(pat)
            assert re.fullmatch(escaped, pat)
            if pat != "a|b":
                assert not re.fullmatch(escaped, "completely_different_string_xyz")

    def test_pkill_pattern_uses_escaped_name(self):
        """Simulate control_server's pkill pattern construction."""
        for malicious in [".*", "a|b", "[test]", "^evil$", "foo;bar"]:
            escaped = re.escape(malicious)
            pattern = f"api_runner\\.py {escaped}"
            # Pattern must NOT match an unrelated process cmdline
            assert not re.search(pattern, "python3 orchestrator.py")
            assert not re.search(pattern, "sleep 30")
            # Pattern must match the literal cmdline
            assert re.search(pattern, f"python3 api_runner.py {malicious}")


# ---------------------------------------------------------------------------
# BOT_REGISTRY validation — endpoints reject unregistered / malformed names
# ---------------------------------------------------------------------------
class TestBotRegistryValidation:
    """Verify endpoints enforce both format validation and registry membership."""

    def _make_handler(self, method: str, path: str, body: dict | None = None):
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.path = path
        handler.command = method
        handler.headers = {"Authorization": "Bearer test-token"}
        if body is not None:
            handler.rfile = BytesIO(json.dumps(body).encode())
            handler.headers["Content-Length"] = str(len(json.dumps(body)))
        else:
            handler.rfile = BytesIO(b"")
            handler.headers["Content-Length"] = "0"
        return handler

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_restart_rejects_semicolon_injection(self):
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/test; rm -rf /restart")
        responses: list = []
        handler._json = lambda code, data, r=responses: r.append((code, data))  # type: ignore
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        ControlHandler.do_POST(handler)

        assert len(responses) > 0
        code, body = responses[0]
        assert code == 400
        assert "invalid bot name" in body.get("error", "").lower()

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_restart_rejects_pipe_optionally_404(self):
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/test|cat /etc/passwd/restart")
        responses: list = []
        handler._json = lambda code, data, r=responses: r.append((code, data))  # type: ignore
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        ControlHandler.do_POST(handler)

        assert len(responses) > 0
        code, body = responses[0]
        assert code in (400, 404)

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_pause_rejects_backtick_injection(self):
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/test`whoami`/pause")
        responses: list = []
        handler._json = lambda code, data, r=responses: r.append((code, data))  # type: ignore
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        ControlHandler.do_POST(handler)

        assert len(responses) > 0
        code, body = responses[0]
        assert code == 400
        assert "invalid bot name" in body.get("error", "").lower()

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_restart_rejects_dollar_injection(self):
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/test$(whoami)/restart")
        responses: list = []
        handler._json = lambda code, data, r=responses: r.append((code, data))  # type: ignore
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        ControlHandler.do_POST(handler)

        assert len(responses) > 0
        code, body = responses[0]
        assert code in (400, 404)

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_unknown_but_valid_format_returns_404(self):
        """Valid-format names not in BOT_REGISTRY must return 404."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/valid_but_unknown_bot/restart")
        responses: list = []
        handler._json = lambda code, data, r=responses: r.append((code, data))  # type: ignore
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        ControlHandler.do_POST(handler)

        assert len(responses) > 0
        code, body = responses[0]
        assert code == 404
        assert "unknown bot" in body.get("error", "").lower()

    def test_valid_bot_accepted_when_in_registry(self):
        from codebot.control_server import ControlHandler

        mock_bot = MagicMock()
        mock_bot.name = "valid-bot"

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot]):
            with patch("codebot.control_server.subprocess.run"):
                with patch("codebot.control_server.subprocess.Popen"):
                    handler = self._make_handler("POST", "/bots/valid-bot/restart")
                    responses: list = []
                    handler._json = lambda code, data, r=responses: r.append((code, data))  # type: ignore
                    handler._auth = lambda: True
                    handler._read_json_body = lambda: ({"force": True}, None, None)
                    ControlHandler.do_POST(handler)
                    assert responses[0][0] == 200

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_pkill_not_called_for_invalid_name(self):
        """Ensure subprocess is never invoked with crafted names."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/evil;rm/restart")
        responses: list = []
        handler._json = lambda code, data, r=responses: r.append((code, data))  # type: ignore
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        with patch("codebot.control_server.subprocess.run") as mock_run:
            with patch("codebot.control_server.subprocess.Popen") as mock_popen:
                ControlHandler.do_POST(handler)
                mock_run.assert_not_called()
                mock_popen.assert_not_called()

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_regex_dot_star_rejected_at_endpoint(self):
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/.*/pause")
        responses: list = []
        handler._json = lambda code, data, r=responses: r.append((code, data))  # type: ignore
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        with patch("codebot.control_server.subprocess.run") as mock_run:
            ControlHandler.do_POST(handler)
            mock_run.assert_not_called()

        assert responses[0][0] == 400

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_batch_stop_rejects_injection(self):
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/stop", body={"bots": ["valid-bot", "evil; rm -rf /"], "force": True})
        responses: list = []
        handler._json = lambda code, data, r=responses: r.append((code, data))  # type: ignore
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"bots": ["valid-bot", "evil; rm -rf /"], "force": True}, None, None)

        ControlHandler.do_POST(handler)

        assert responses[0][0] == 400
        assert "invalid bot name" in responses[0][1].get("error", "").lower()

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_pause_rejects_path_traversal(self):
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/../../etc/passwd/pause")
        responses: list = []
        handler._json = lambda code, data, r=responses: r.append((code, data))  # type: ignore
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        ControlHandler.do_POST(handler)

        assert len(responses) > 0
        assert responses[0][0] != 200


# ---------------------------------------------------------------------------
# PID-file-based process termination — verifies secure kill path
# ---------------------------------------------------------------------------
class TestPidFileBasedKill:
    """Verify process termination uses PID files + cmdline verification, not pkill."""

    def test_restart_endpoint_uses_pid_file_kill(self):
        """Restart endpoint must use _safe_kill_bot_process with PID file verification."""
        from codebot.control_server import ControlHandler

        mock_bot = MagicMock()
        mock_bot.name = "valid-bot"

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot]):
            with patch("codebot.control_server._safe_kill_bot_process", return_value=(True, [1234])) as mock_safe_kill:
                with patch("codebot.control_server.subprocess.Popen"):
                    handler = MagicMock(spec=ControlHandler)
                    handler.path = "/bots/valid-bot/restart"
                    handler.headers = {"Authorization": "Bearer test-token"}
                    handler._json = lambda *a, **kw: None
                    handler._auth = lambda: True
                    handler._read_json_body = lambda: ({"force": True}, None, None)
                    # use real do_POST binding
                    ControlHandler.do_POST(handler)
                    # Verify _safe_kill_bot_process was called with validated bot name
                    mock_safe_kill.assert_called_once_with("valid-bot", timeout=5)

    def test_pause_endpoint_uses_pid_file_kill(self):
        """Pause endpoint must use _safe_kill_bot_process with PID file verification."""
        from codebot.control_server import ControlHandler

        mock_bot = MagicMock()
        mock_bot.name = "valid-bot"

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot]):
            with patch("codebot.control_server._safe_kill_bot_process", return_value=(True, [1234])) as mock_safe_kill:
                with patch("codebot.control_server.STATE_DIR") as mock_state:
                    mock_file = MagicMock()
                    mock_state.__truediv__ = MagicMock(return_value=mock_file)

                    handler = MagicMock(spec=ControlHandler)
                    handler.path = "/bots/valid-bot/pause"
                    handler.headers = {"Authorization": "Bearer test-token"}
                    handler._json = lambda *a, **kw: None
                    handler._auth = lambda: True
                    handler._read_json_body = lambda: ({"force": True}, None, None)

                    ControlHandler.do_POST(handler)

                    # Verify _safe_kill_bot_process was called with validated bot name
                    mock_safe_kill.assert_called_once_with("valid-bot", timeout=5)

    def test_bot_status_uses_pid_file_verification(self):
        """bot_status must use PID file + cmdline verification, not pgrep."""
        from codebot.control_server import bot_status

        mock_cfg = MagicMock()
        mock_cfg.name = "test-bot"
        mock_cfg.model = "test-model"
        mock_cfg.interval_seconds = 60
        mock_cfg.heartbeat_timeout = 300

        with patch("codebot.control_server.BOT_REGISTRY", [mock_cfg]):
            with patch("codebot.control_server._read_pid_file", return_value=1234) as mock_rpf, \
                 patch("codebot.control_server._verify_cmdline", return_value=True) as mock_vc, \
                 patch("codebot.control_server.heartbeat_age", return_value=None):
                result = bot_status("test-bot")
                # Verify PID file was read and cmdline verified
                mock_rpf.assert_called_with("test-bot")
                mock_vc.assert_called_with(1234, "test-bot")
                assert result["running"] is True
                assert result["pid"] == "1234"

    def test_injection_payloads_never_reach_subprocess(self):
        """All injection payloads must be rejected before any subprocess call."""
        from codebot.control_server import ControlHandler

        payloads = [
            "test; rm -rf /",
            "test|cat /etc/passwd",
            "test`whoami`",
            "test$(id)",
            "test&evil",
            ".*",
            "a|b",
            "[evil]",
            "^root$",
        ]
        for payload in payloads:
            handler = MagicMock(spec=ControlHandler)
            # Use a path that includes the payload; if payload contains '/', handle separately
            safe_payload = payload.split("/")[0] if "/" in payload else payload
            handler.path = f"/bots/{safe_payload}/restart"
            handler.headers = {"Authorization": "Bearer test-token"}
            responses: list = []
            handler._json = lambda code, data, r=responses: r.append((code, data))  # type: ignore
            handler._auth = lambda: True
            handler._read_json_body = lambda: (None, None, None)

            with patch("codebot.control_server.BOT_REGISTRY", []):
                with patch("codebot.control_server.subprocess.run") as mock_run:
                    with patch("codebot.control_server.subprocess.Popen") as mock_popen:
                        # Need to recreate handler with proper path handling for injection test
                        # Directly test validate_bot_name instead if path parsing is ambiguous
                        if not validate_bot_name(safe_payload):
                            ControlHandler.do_POST(handler)
                            mock_run.assert_not_called()
                            mock_popen.assert_not_called()
                            assert responses[0][0] == 400


# ---------------------------------------------------------------------------
# TOCTOU and Unicode Digit Security Tests for _read_pid_file (CB-43585)
# ---------------------------------------------------------------------------
class TestPidFileSecurityHardening:
    """Verify _read_pid_file resists TOCTOU races and rejects Unicode digits."""

    def test_read_pid_file_unicode_digit_rejection(self, tmp_path):
        """PIDs containing Unicode digits (e.g., U+0661) must be rejected.
        
        str.isdigit() accepts Unicode digits which int() may handle unexpectedly.
        Strict ASCII validation via regex ^[0-9]+$ is required.
        """
        from codebot.control_server import _read_pid_file
        import os

        # Patch STATE_DIR to use tmp_path
        with patch("codebot.control_server.STATE_DIR", tmp_path):
            pid_file = tmp_path / "unicode_bot.pid"
            # Write a PID using Arabic-Indic digit one (U+0661) mixed with ASCII
            # U+0661 is '١' which str.isdigit() returns True for
            malicious_content = "12١45"  # Contains U+0661
            pid_file.write_text(malicious_content)
            os.chmod(pid_file, 0o600)
            
            result = _read_pid_file("unicode_bot")
            assert result is None, f"Unicode digit PID should be rejected, got {result}"

    def test_read_pid_file_pure_unicode_digits_rejected(self, tmp_path):
        """Pure Unicode digit strings must be rejected even if they look numeric."""
        from codebot.control_server import _read_pid_file
        import os

        with patch("codebot.control_server.STATE_DIR", tmp_path):
            pid_file = tmp_path / "pure_unicode.pid"
            # All Arabic-Indic digits
            pid_file.write_text("١٢٣٤٥")
            os.chmod(pid_file, 0o600)
            
            result = _read_pid_file("pure_unicode")
            assert result is None

    def test_read_pid_file_valid_ascii_accepted(self, tmp_path):
        """Valid ASCII-only PIDs must still be accepted."""
        from codebot.control_server import _read_pid_file
        import os

        with patch("codebot.control_server.STATE_DIR", tmp_path):
            pid_file = tmp_path / "valid_bot.pid"
            pid_file.write_text("12345\n")
            os.chmod(pid_file, 0o600)
            
            result = _read_pid_file("valid_bot")
            assert result == 12345

    def test_read_pid_file_symlink_rejected(self, tmp_path):
        """Symlinks must be rejected at open time via O_NOFOLLOW.
        
        This verifies TOCTOU resistance: even if an attacker swaps the file
        for a symlink between stat and read, O_NOFOLLOW prevents following it.
        """
        from codebot.control_server import _read_pid_file
        import os

        with patch("codebot.control_server.STATE_DIR", tmp_path):
            # Create a target file that would leak content if followed
            target_file = tmp_path / "secret_target.txt"
            target_file.write_text("99999")
            os.chmod(target_file, 0o600)
            
            # Create a symlink pointing to the target
            pid_file = tmp_path / "symlink_bot.pid"
            try:
                os.symlink(target_file, pid_file)
            except OSError:
                pytest.skip("Symlink creation not supported on this platform")
            
            result = _read_pid_file("symlink_bot")
            assert result is None, "Symlinked PID file must be rejected (O_NOFOLLOW)"

    def test_read_pid_file_insecure_permissions_rejected(self, tmp_path):
        """PID files with permissions other than 0o600 must be rejected."""
        from codebot.control_server import _read_pid_file
        import os

        with patch("codebot.control_server.STATE_DIR", tmp_path):
            pid_file = tmp_path / "insecure_bot.pid"
            pid_file.write_text("12345")
            # World-readable permissions
            os.chmod(pid_file, 0o644)
            
            result = _read_pid_file("insecure_bot")
            assert result is None, "World-readable PID file must be rejected"

    def test_read_pid_file_non_regular_file_rejected(self, tmp_path):
        """Non-regular files (directories, devices) must be rejected via fstat check."""
        from codebot.control_server import _read_pid_file
        import os

        with patch("codebot.control_server.STATE_DIR", tmp_path):
            # Create a directory instead of a file
            dir_path = tmp_path / "dir_bot.pid"
            dir_path.mkdir()
            os.chmod(dir_path, 0o600)
            
            result = _read_pid_file("dir_bot")
            assert result is None, "Directory masquerading as PID file must be rejected"

    def test_read_orchestrator_pid_file_unicode_rejected(self, tmp_path):
        """Orchestrator PID file must also reject Unicode digits."""
        from codebot.control_server import _read_orchestrator_pid_file
        import os

        with patch("codebot.control_server.STATE_DIR", tmp_path):
            pid_file = tmp_path / ".orchestrator.pid"
            pid_file.write_text("١٢٣")  # Unicode digits
            os.chmod(pid_file, 0o600)
            
            result = _read_orchestrator_pid_file()
            assert result is None

    def test_read_orchestrator_pid_file_symlink_rejected(self, tmp_path):
        """Orchestrator PID file must reject symlinks via O_NOFOLLOW."""
        from codebot.control_server import _read_orchestrator_pid_file
        import os

        with patch("codebot.control_server.STATE_DIR", tmp_path):
            target = tmp_path / "real_orch.pid"
            target.write_text("999")
            os.chmod(target, 0o600)
            
            pid_file = tmp_path / ".orchestrator.pid"
            try:
                os.symlink(target, pid_file)
            except OSError:
                pytest.skip("Symlink creation not supported")
            
            result = _read_orchestrator_pid_file()
            assert result is None
