import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import codebot.control_client as cc


class FakeResponse:
    """Mock HTTP response for urllib.request.urlopen context manager."""
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status
        self.code = status  # urllib uses .code in some contexts

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self._body
        return self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestModuleConstants:
    def test_url_is_string(self):
        assert isinstance(cc.URL, str)
        assert cc.URL.startswith("http")

    def test_url_has_no_trailing_slash(self):
        assert not cc.URL.endswith("/")

    def test_token_is_string(self):
        assert isinstance(cc.TOKEN, str)

    def test_token_is_stripped(self):
        assert cc.TOKEN == cc.TOKEN.strip()

    def test_url_default_when_env_unset(self):
        import importlib
        import os
        with patch.dict(os.environ, {}, clear=False):
            if "CONTROL_URL" in os.environ:
                del os.environ["CONTROL_URL"]
            import importlib as _imp
            reloaded = _imp.reload(cc)
            assert reloaded.URL == "http://127.0.0.1:8081"
            _imp.reload(cc)

    def test_url_strips_trailing_slash_via_env(self):
        import importlib
        import os
        with patch.dict(os.environ, {"CONTROL_URL": "http://example.com///"}):
            reloaded = importlib.reload(cc)
            assert reloaded.URL == "http://example.com"
            assert not reloaded.URL.endswith("/")
            importlib.reload(cc)

    def test_token_stripped_via_env(self):
        import importlib
        import os
        with patch.dict(os.environ, {"CONTROL_TOKEN": "  secret123  "}):
            reloaded = importlib.reload(cc)
            assert reloaded.TOKEN == "secret123"
            importlib.reload(cc)


class TestReqUrlConstruction:
    def test_url_concatenation(self):
        with patch("codebot.control_client.urllib.request.Request") as MockReq, \
             patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(b'{}', 200)
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    cc.req("GET", "/bots")
            args, kwargs = MockReq.call_args
            assert args[0] == "http://example.com/bots"

    def test_path_with_query_string(self):
        with patch("codebot.control_client.urllib.request.Request") as MockReq, \
             patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(b'{}', 200)
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    cc.req("GET", "/bots/name/logs?lines=50")
            args, _ = MockReq.call_args
            assert args[0] == "http://example.com/bots/name/logs?lines=50"

    def test_method_forwarded(self):
        for method in ("GET", "POST", "PUT", "DELETE"):
            with patch("codebot.control_client.urllib.request.Request") as MockReq, \
                 patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.return_value = FakeResponse(b'{}', 200)
                with patch.object(cc, "URL", "http://example.com"):
                    with patch.object(cc, "TOKEN", ""):
                        cc.req(method, "/path")
                _, kwargs = MockReq.call_args
                assert kwargs["method"] == method

    def test_timeout_is_15(self):
        with patch("codebot.control_client.urllib.request.Request"), \
             patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(b'{}', 200)
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    cc.req("GET", "/x")
            _, kwargs = mock_urlopen.call_args
            assert kwargs.get("timeout") == 15


class TestReqHeaders:
    def test_content_type_always_set(self):
        with patch("codebot.control_client.urllib.request.Request") as MockReq, \
             patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(b'{}', 200)
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    cc.req("GET", "/x")
            _, kwargs = MockReq.call_args
            assert kwargs["headers"]["Content-Type"] == "application/json"

    def test_no_auth_header_when_token_empty(self):
        with patch("codebot.control_client.urllib.request.Request") as MockReq, \
             patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(b'{}', 200)
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    cc.req("GET", "/x")
            _, kwargs = MockReq.call_args
            assert "Authorization" not in kwargs["headers"]

    def test_auth_header_when_token_set(self):
        with patch("codebot.control_client.urllib.request.Request") as MockReq, \
             patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(b'{}', 200)
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", "s3cr3t"):
                    cc.req("GET", "/x")
            _, kwargs = MockReq.call_args
            assert kwargs["headers"]["Authorization"] == "Bearer s3cr3t"

    def test_auth_header_uses_bearer_prefix(self):
        with patch("codebot.control_client.urllib.request.Request") as MockReq, \
             patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(b'{}', 200)
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", "tok"):
                    cc.req("GET", "/x")
            _, kwargs = MockReq.call_args
            assert kwargs["headers"]["Authorization"].startswith("Bearer ")


class TestReqBodyEncoding:
    def test_none_body_results_in_none_data(self):
        with patch("codebot.control_client.urllib.request.Request") as MockReq, \
             patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(b'{}', 200)
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    cc.req("GET", "/x", None)
            _, kwargs = MockReq.call_args
            assert kwargs["data"] is None

    def test_dict_body_is_json_encoded(self):
        with patch("codebot.control_client.urllib.request.Request") as MockReq, \
             patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(b'{}', 200)
            body = {"key": "value", "num": 42}
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    cc.req("POST", "/x", body)
            _, kwargs = MockReq.call_args
            assert kwargs["data"] == json.dumps(body).encode()
            assert isinstance(kwargs["data"], bytes)

    def test_empty_dict_body_is_encoded(self):
        with patch("codebot.control_client.urllib.request.Request") as MockReq, \
             patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(b'{}', 200)
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    cc.req("POST", "/x", {})
            _, kwargs = MockReq.call_args
            assert kwargs["data"] == b'{}'


class TestReqSuccessResponses:
    def test_json_object_response(self):
        payload = {"ok": True, "bots": 3}
        with patch("codebot.control_client.urllib.request.Request"), \
             patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(json.dumps(payload).encode(), 200)
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    code, data = cc.req("GET", "/x")
            assert code == 200
            assert data == payload

    def test_json_array_response(self):
        payload = [{"name": "issues"}, {"name": "build"}]
        with patch("codebot.control_client.urllib.request.Request"), \
             patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(json.dumps(payload).encode(), 200)
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    code, data = cc.req("GET", "/bots")
            assert code == 200
            assert data == payload

    def test_empty_body_returns_empty_dict(self):
        with patch("codebot.control_client.urllib.request.Request"), \
             patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(b'', 200)
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    code, data = cc.req("GET", "/x")
            assert code == 200
            assert data == {}

    def test_non_json_body_returns_raw_string(self):
        raw = "not json at all"
        with patch("codebot.control_client.urllib.request.Request"), \
             patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(raw.encode(), 200)
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    code, data = cc.req("GET", "/x")
            assert code == 200
            assert data == raw

    def test_status_code_forwarded(self):
        for status in (200, 201, 204, 404):
            with patch("codebot.control_client.urllib.request.Request"), \
                 patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.return_value = FakeResponse(b'{}', status)
                with patch.object(cc, "URL", "http://example.com"):
                    with patch.object(cc, "TOKEN", ""):
                        code, _ = cc.req("GET", "/x")
                assert code == status


class TestReqHttpErrorHandling:
    def _make_http_error(self, code, body_bytes, has_fp=True):
        err = urllib.error.HTTPError(
            url="http://example.com/x",
            code=code,
            msg="error",
            hdrs=None,
            fp=MagicMock() if has_fp else None,
        )
        if has_fp:
            err.fp = MagicMock()
            err.read = MagicMock(return_value=body_bytes)
            err.fp.read = MagicMock(return_value=body_bytes)
        else:
            err.fp = None
            err.read = MagicMock(return_value=body_bytes)
        return err

    def test_http_error_with_json_body(self):
        payload = {"error": "unauthorized"}
        err = self._make_http_error(401, json.dumps(payload).encode())
        with patch("codebot.control_client.urllib.request.Request"), \
             patch("codebot.control_client.urllib.request.urlopen", side_effect=err):
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    code, data = cc.req("GET", "/x")
            assert code == 401
            assert data == payload

    def test_http_error_with_non_json_body(self):
        err = self._make_http_error(500, b"internal server error")
        with patch("codebot.control_client.urllib.request.Request"), \
             patch("codebot.control_client.urllib.request.urlopen", side_effect=err):
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    code, data = cc.req("GET", "/x")
            assert code == 500
            assert data["error"] == "internal server error"
            assert data["code"] == 500

    def test_http_error_empty_body_with_fp(self):
        err = self._make_http_error(404, b"")
        with patch("codebot.control_client.urllib.request.Request"), \
             patch("codebot.control_client.urllib.request.urlopen", side_effect=err):
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    code, data = cc.req("GET", "/x")
            assert code == 404
            assert data == {"error": ""}

    def test_http_error_no_fp_returns_empty_string(self):
        err = urllib.error.HTTPError(
            url="http://example.com/x",
            code=403,
            msg="forbidden",
            hdrs=None,
            fp=None,
        )
        with patch("codebot.control_client.urllib.request.Request"), \
             patch("codebot.control_client.urllib.request.urlopen", side_effect=err):
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    code, data = cc.req("GET", "/x")
            assert code == 403

    def test_http_error_code_preserved(self):
        for code in (400, 401, 403, 404, 429, 500, 503):
            err = self._make_http_error(code, b'{"error":"x"}')
            with patch("codebot.control_client.urllib.request.Request"), \
                 patch("codebot.control_client.urllib.request.urlopen", side_effect=err):
                with patch.object(cc, "URL", "http://example.com"):
                    with patch.object(cc, "TOKEN", ""):
                        got_code, _ = cc.req("GET", "/x")
                assert got_code == code

    def test_http_error_json_array_body(self):
        payload = ["a", "b"]
        err = self._make_http_error(400, json.dumps(payload).encode())
        with patch("codebot.control_client.urllib.request.Request"), \
             patch("codebot.control_client.urllib.request.urlopen", side_effect=err):
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    code, data = cc.req("POST", "/x", {})
            assert code == 400
            assert data == payload


class TestReqGenericExceptionHandling:
    def test_url_error_returns_zero(self):
        with patch("codebot.control_client.urllib.request.Request"), \
             patch("codebot.control_client.urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    code, data = cc.req("GET", "/x")
            assert code == 0
            assert "error" in data

    def test_timeout_returns_zero(self):
        with patch("codebot.control_client.urllib.request.Request"), \
             patch("codebot.control_client.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    code, data = cc.req("GET", "/x")
            assert code == 0
            assert "timed out" in data["error"]

    def test_generic_exception_returns_zero(self):
        with patch("codebot.control_client.urllib.request.Request"), \
             patch("codebot.control_client.urllib.request.urlopen", side_effect=RuntimeError("boom")):
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    code, data = cc.req("GET", "/x")
            assert code == 0
            assert "boom" in data["error"]

    def test_os_error_returns_zero(self):
        with patch("codebot.control_client.urllib.request.Request"), \
             patch("codebot.control_client.urllib.request.urlopen", side_effect=OSError("no route")):
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    code, data = cc.req("GET", "/x")
            assert code == 0
            assert "no route" in data["error"]


class TestCmdStatus:
    def test_success_prints_bots(self, capsys):
        bots = [
            {"name": "issues", "running": True, "heartbeat_age_seconds": 12.3, "effective_timeout": 600, "risk": "low", "model": "xiaomi-mimo-2.5", "next_run_in_seconds": 45},
            {"name": "build", "running": False, "heartbeat_age_seconds": None, "effective_timeout": 600, "risk": "medium", "model": "qwen-3.8-max", "next_run_in_seconds": None},
        ]
        with patch("codebot.control_client.req", return_value=(200, bots)):
            cc.cmd_status()
        out = capsys.readouterr().out
        assert "issues" in out
        assert "build" in out
        assert "RUN" in out
        assert "WAIT" in out

    def test_failure_exits_1(self):
        with patch("codebot.control_client.req", return_value=(401, {"error": "unauthorized"})):
            with pytest.raises(SystemExit) as exc:
                cc.cmd_status()
            assert exc.value.code == 1

    def test_calls_correct_endpoint(self):
        with patch("codebot.control_client.req", return_value=(200, [])) as mock_req:
            cc.cmd_status()
            mock_req.assert_called_once_with("GET", "/bots")

    def test_empty_list_no_crash(self, capsys):
        with patch("codebot.control_client.req", return_value=(200, [])):
            cc.cmd_status()
        assert capsys.readouterr().out == ""


class TestCmdSchedulerStatus:
    def test_success_prints_bounded_json(self, capsys):
        payload = {
            "version": 1,
            "budget_state": "ok",
            "budget_day": "2026-01-01",
            "budget_total_actual": 100,
            "drain": False,
            "dead_letter_count": 0,
            "dead_letter_ids": [],
            "queue_tracked": 5,
            "lease_active": 2,
            "paused_count": 0,
            "paused_bots": [],
            "disabled_count": 0,
            "disabled_bots": [],
            "disabled_details": [],
            "starved_oldest": None,
            "starved_top": [],
            "batch_utilization": {"max_per_batch": 5},
            "per_model_actual": {},
        }
        with patch("codebot.control_client.req", return_value=(200, payload)):
            cc.cmd_scheduler_status()
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["version"] == 1
        assert data["budget_state"] == "ok"

    def test_non_200_exits_1(self):
        with patch("codebot.control_client.req", return_value=(500, {"error": "fail"})):
            with pytest.raises(SystemExit) as exc:
                cc.cmd_scheduler_status()
            assert exc.value.code == 1

    def test_non_dict_payload_exits_1(self):
        with patch("codebot.control_client.req", return_value=(200, "not a dict")):
            with pytest.raises(SystemExit) as exc:
                cc.cmd_scheduler_status()
            assert exc.value.code == 1

    def test_truncates_long_lists_to_20(self, capsys):
        payload = {
            "version": 1,
            "budget_state": "ok",
            "budget_day": "2026-01-01",
            "budget_total_actual": 0,
            "drain": False,
            "dead_letter_count": 50,
            "dead_letter_ids": [f"id-{i}" for i in range(50)],
            "queue_tracked": 0,
            "lease_active": 0,
            "paused_count": 0,
            "paused_bots": [f"bot-{i}" for i in range(50)],
            "disabled_count": 0,
            "disabled_bots": [f"bot-{i}" for i in range(50)],
            "disabled_details": [{"bot": f"bot-{i}", "reason": "x"} for i in range(50)],
            "starved_oldest": None,
            "starved_top": [{"bot": f"bot-{i}", "starvation_age_seconds": i} for i in range(10)],
            "batch_utilization": {},
            "per_model_actual": {f"model-{i}": {"v": i} for i in range(50)},
        }
        with patch("codebot.control_client.req", return_value=(200, payload)):
            cc.cmd_scheduler_status()
        out = capsys.readouterr().out
        data = json.loads(out)
        assert len(data["dead_letter_ids"]) == 20
        assert len(data["paused_bots"]) == 20
        assert len(data["disabled_bots"]) == 20
        assert len(data["disabled_details"]) == 20
        assert len(data["starved_top"]) == 5
        assert len(data["per_model_actual"]) == 32

    def test_non_list_disabled_details_coerced(self, capsys):
        payload = {
            "version": 1,
            "budget_state": "ok",
            "budget_day": "2026-01-01",
            "budget_total_actual": 0,
            "drain": False,
            "dead_letter_count": 0,
            "dead_letter_ids": [],
            "queue_tracked": 0,
            "lease_active": 0,
            "paused_count": 0,
            "paused_bots": [],
            "disabled_count": 0,
            "disabled_bots": [],
            "disabled_details": "not-a-list",
            "starved_oldest": None,
            "starved_top": [],
            "batch_utilization": {},
            "per_model_actual": {},
        }
        with patch("codebot.control_client.req", return_value=(200, payload)):
            cc.cmd_scheduler_status()
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["disabled_details"] == []

    def test_calls_correct_endpoint(self):
        payload = {
            "version": 1,
            "budget_state": "ok",
            "budget_day": "2026-01-01",
            "budget_total_actual": 0,
            "drain": False,
            "dead_letter_count": 0,
            "dead_letter_ids": [],
            "queue_tracked": 0,
            "lease_active": 0,
            "paused_count": 0,
            "paused_bots": [],
            "disabled_count": 0,
            "disabled_bots": [],
            "disabled_details": [],
            "starved_oldest": None,
            "starved_top": [],
            "batch_utilization": {},
            "per_model_actual": None,
        }
        with patch("codebot.control_client.req", return_value=(200, payload)) as mock_req:
            cc.cmd_scheduler_status()
            mock_req.assert_called_once_with("GET", "/scheduler/status")


class TestCmdSchedulerEvents:
    def test_success_with_default_limit(self, capsys):
        payload = {"version": 1, "events": []}
        with patch("codebot.control_client.req", return_value=(200, payload)) as mock_req:
            cc.cmd_scheduler_events()
        mock_req.assert_called_once_with("GET", "/scheduler/events?limit=20")
        assert "version" in capsys.readouterr().out

    def test_custom_limit(self):
        with patch("codebot.control_client.req", return_value=(200, {})) as mock_req:
            cc.cmd_scheduler_events("50")
        mock_req.assert_called_once_with("GET", "/scheduler/events?limit=50")

    def test_limit_clamped_to_100(self):
        with patch("codebot.control_client.req", return_value=(200, {})) as mock_req:
            cc.cmd_scheduler_events("999")
        mock_req.assert_called_once_with("GET", "/scheduler/events?limit=100")

    def test_limit_clamped_to_1(self):
        with patch("codebot.control_client.req", return_value=(200, {})) as mock_req:
            cc.cmd_scheduler_events("0")
        mock_req.assert_called_once_with("GET", "/scheduler/events?limit=1")

    def test_limit_negative_clamped(self):
        with patch("codebot.control_client.req", return_value=(200, {})) as mock_req:
            cc.cmd_scheduler_events("-5")
        mock_req.assert_called_once_with("GET", "/scheduler/events?limit=1")

    def test_invalid_limit_exits_1(self):
        with pytest.raises(SystemExit) as exc:
            cc.cmd_scheduler_events("notanint")
        assert exc.value.code == 1

    def test_event_type_appends_query(self):
        with patch("codebot.control_client.req", return_value=(200, {})) as mock_req:
            cc.cmd_scheduler_events("20", "telemetry")
        mock_req.assert_called_once_with("GET", "/scheduler/events?limit=20&type=telemetry")

    def test_event_type_is_url_encoded(self):
        with patch("codebot.control_client.req", return_value=(200, {})) as mock_req:
            cc.cmd_scheduler_events("20", "my type")
        called_path = mock_req.call_args[0][1]
        assert "my%20type" in called_path

    def test_no_event_type_no_type_param(self):
        with patch("codebot.control_client.req", return_value=(200, {})) as mock_req:
            cc.cmd_scheduler_events("20", None)
        called_path = mock_req.call_args[0][1]
        assert "type=" not in called_path

    def test_non_200_exits_1(self):
        with patch("codebot.control_client.req", return_value=(500, {})):
            with pytest.raises(SystemExit) as exc:
                cc.cmd_scheduler_events("20")
            assert exc.value.code == 1


class TestCmdDeadLetters:
    def test_success(self, capsys):
        payload = {"version": 1, "count": 1, "dead_letters": [{"id": "Q-1", "reason": "x"}]}
        with patch("codebot.control_client.req", return_value=(200, payload)):
            cc.cmd_dead_letters()
        assert "Q-1" in capsys.readouterr().out

    def test_failure_exits_1(self):
        with patch("codebot.control_client.req", return_value=(500, {"error": "fail"})):
            with pytest.raises(SystemExit) as exc:
                cc.cmd_dead_letters()
            assert exc.value.code == 1

    def test_calls_correct_endpoint(self):
        with patch("codebot.control_client.req", return_value=(200, {})) as mock_req:
            cc.cmd_dead_letters()
            mock_req.assert_called_once_with("GET", "/scheduler/dead-letters")


class TestCmdDeadLetterRetry:
    def test_success_no_exit(self, capsys):
        with patch("codebot.control_client.req", return_value=(200, {"status": "retried"})):
            cc.cmd_dead_letter_retry("Q-123")
        assert "retried" in capsys.readouterr().out

    def test_failure_exits_1(self):
        with patch("codebot.control_client.req", return_value=(404, {"error": "not found"})):
            with pytest.raises(SystemExit) as exc:
                cc.cmd_dead_letter_retry("Q-999")
            assert exc.value.code == 1

    def test_calls_correct_endpoint(self):
        with patch("codebot.control_client.req", return_value=(200, {})) as mock_req:
            cc.cmd_dead_letter_retry("Q-42")
            mock_req.assert_called_once_with("POST", "/scheduler/dead-letters/Q-42/retry", {})

    def test_non_dict_payload_printed(self, capsys):
        with patch("codebot.control_client.req", return_value=(200, "raw string")):
            cc.cmd_dead_letter_retry("Q-1")
        assert "raw string" in capsys.readouterr().out


class TestMain:
    def test_no_args_exits_1(self, capsys):
        with patch.object(sys, "argv", ["control_client.py"]):
            with pytest.raises(SystemExit) as exc:
                cc.main()
            assert exc.value.code == 1

    def test_unknown_command_exits_2(self, capsys):
        with patch.object(sys, "argv", ["control_client.py", "unknown_cmd"]):
            with pytest.raises(SystemExit) as exc:
                cc.main()
            assert exc.value.code == 2

    def test_status_dispatch(self):
        with patch.object(sys, "argv", ["control_client.py", "status"]), \
             patch("codebot.control_client.cmd_status") as mock:
            cc.main()
            mock.assert_called_once()

    def test_scheduler_status_dispatch(self):
        with patch.object(sys, "argv", ["control_client.py", "scheduler-status"]), \
             patch("codebot.control_client.cmd_scheduler_status") as mock:
            cc.main()
            mock.assert_called_once()

    def test_dead_letters_dispatch(self):
        with patch.object(sys, "argv", ["control_client.py", "dead-letters"]), \
             patch("codebot.control_client.cmd_dead_letters") as mock:
            cc.main()
            mock.assert_called_once()

    def test_retry_dead_letter_dispatch(self):
        with patch.object(sys, "argv", ["control_client.py", "retry-dead-letter", "Q-1"]), \
             patch("codebot.control_client.cmd_dead_letter_retry") as mock:
            cc.main()
            mock.assert_called_once_with("Q-1")

    def test_health_dispatch(self, capsys):
        with patch.object(sys, "argv", ["control_client.py", "health"]), \
             patch("codebot.control_client.req", return_value=(200, {"status": "ok"})):
            cc.main()
        assert "ok" in capsys.readouterr().out

    def test_logs_dispatch_default_lines(self):
        with patch.object(sys, "argv", ["control_client.py", "logs", "issues"]), \
             patch("codebot.control_client.req", return_value=(200, {"tail": "log"})) as mock_req:
            cc.main()
            mock_req.assert_called_once_with("GET", "/bots/issues/logs?lines=200")

    def test_logs_dispatch_custom_lines(self):
        with patch.object(sys, "argv", ["control_client.py", "logs", "issues", "--lines", "50"]), \
             patch("codebot.control_client.req", return_value=(200, {"tail": "log"})) as mock_req:
            cc.main()
            mock_req.assert_called_once_with("GET", "/bots/issues/logs?lines=50")

    def test_logs_missing_lines_value_exits_1(self):
        with patch.object(sys, "argv", ["control_client.py", "logs", "issues", "--lines"]):
            with pytest.raises(SystemExit) as exc:
                cc.main()
            assert exc.value.code == 1

    def test_logs_invalid_lines_exits_1(self):
        with patch.object(sys, "argv", ["control_client.py", "logs", "issues", "--lines", "abc"]):
            with pytest.raises(SystemExit) as exc:
                cc.main()
            assert exc.value.code == 1

    def test_restart_dispatch(self):
        with patch.object(sys, "argv", ["control_client.py", "restart", "issues"]), \
             patch("codebot.control_client.req", return_value=(200, {"ok": True})) as mock_req:
            cc.main()
            mock_req.assert_called_once_with("POST", "/bots/issues/restart", {})

    def test_pause_dispatch(self):
        with patch.object(sys, "argv", ["control_client.py", "pause", "issues"]), \
             patch("codebot.control_client.req", return_value=(200, {"ok": True})) as mock_req:
            cc.main()
            mock_req.assert_called_once_with("POST", "/bots/issues/pause", {})

    def test_resume_dispatch(self):
        with patch.object(sys, "argv", ["control_client.py", "resume", "issues"]), \
             patch("codebot.control_client.req", return_value=(200, {"ok": True})) as mock_req:
            cc.main()
            mock_req.assert_called_once_with("POST", "/bots/issues/resume", {})

    def test_drain_dispatch(self):
        for cmd in ("drain", "safe-stop"):
            with patch.object(sys, "argv", ["control_client.py", cmd]), \
                 patch("codebot.control_client.req", return_value=(200, {"ok": True})) as mock_req:
                cc.main()
                mock_req.assert_called_once_with("POST", "/control/drain", {})

    def test_clear_drain_dispatch(self):
        for cmd in ("clear-drain", "undrain"):
            with patch.object(sys, "argv", ["control_client.py", cmd]), \
                 patch("codebot.control_client.req", return_value=(200, {"ok": True})) as mock_req:
                cc.main()
                mock_req.assert_called_once_with("POST", "/control/clear-drain", {})

    def test_update_dispatch(self, capsys):
        with patch.object(sys, "argv", ["control_client.py", "update"]), \
             patch("codebot.control_client.req", return_value=(200, {"ok": True})):
            cc.main()

    def test_state_dispatch(self, capsys):
        with patch.object(sys, "argv", ["control_client.py", "state"]), \
             patch("codebot.control_client.req", return_value=(200, {"x": 1})):
            cc.main()
        assert "x" in capsys.readouterr().out

    def test_scheduler_events_dispatch_default(self):
        with patch.object(sys, "argv", ["control_client.py", "scheduler-events"]), \
             patch("codebot.control_client.cmd_scheduler_events") as mock:
            cc.main()
            mock.assert_called_once_with("20", None)

    def test_scheduler_events_with_limit(self):
        with patch.object(sys, "argv", ["control_client.py", "scheduler-events", "--limit", "50"]), \
             patch("codebot.control_client.cmd_scheduler_events") as mock:
            cc.main()
            mock.assert_called_once_with("50", None)

    def test_scheduler_events_with_type(self):
        with patch.object(sys, "argv", ["control_client.py", "scheduler-events", "--type", "telemetry"]), \
             patch("codebot.control_client.cmd_scheduler_events") as mock:
            cc.main()
            mock.assert_called_once_with("20", "telemetry")

    def test_scheduler_events_with_both(self):
        with patch.object(sys, "argv", ["control_client.py", "scheduler-events", "--limit", "10", "--type", "foo"]), \
             patch("codebot.control_client.cmd_scheduler_events") as mock:
            cc.main()
            mock.assert_called_once_with("10", "foo")

    def test_scheduler_events_unknown_option_exits_1(self):
        with patch.object(sys, "argv", ["control_client.py", "scheduler-events", "--unknown", "val"]):
            with pytest.raises(SystemExit) as exc:
                cc.main()
            assert exc.value.code == 1

    def test_scheduler_events_unknown_arg_exits_1(self):
        with patch.object(sys, "argv", ["control_client.py", "scheduler-events", "unexpected"]):
            with pytest.raises(SystemExit) as exc:
                cc.main()
            assert exc.value.code == 1

    def test_logs_tail_extraction_from_dict(self, capsys):
        with patch.object(sys, "argv", ["control_client.py", "logs", "issues"]), \
             patch("codebot.control_client.req", return_value=(200, {"tail": "my tail content"})):
            cc.main()
        assert "my tail content" in capsys.readouterr().out

    def test_logs_fallback_to_json_when_no_tail(self, capsys):
        with patch.object(sys, "argv", ["control_client.py", "logs", "issues"]), \
             patch("codebot.control_client.req", return_value=(200, {"other": "data"})):
            cc.main()
        assert "other" in capsys.readouterr().out

    def test_logs_non_dict_response(self, capsys):
        with patch.object(sys, "argv", ["control_client.py", "logs", "issues"]), \
             patch("codebot.control_client.req", return_value=(200, "raw log text")):
            cc.main()
        assert "raw log text" in capsys.readouterr().out


class TestReqEdgeCases:
    def test_unicode_response_decoded(self):
        payload = {"msg": "héllo wörld"}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with patch("codebot.control_client.urllib.request.Request"), \
             patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(body, 200)
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    code, data = cc.req("GET", "/x")
            assert code == 200
            assert data["msg"] == "héllo wörld"

    def test_large_json_response(self):
        payload = {"data": "x" * 10000}
        with patch("codebot.control_client.urllib.request.Request"), \
             patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(json.dumps(payload).encode(), 200)
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    code, data = cc.req("GET", "/x")
            assert code == 200
            assert len(data["data"]) == 10000

    def test_request_object_receives_json_headers(self):
        with patch("codebot.control_client.urllib.request.Request") as MockReq, \
             patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(b'{}', 200)
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", "tok"):
                    cc.req("POST", "/control/drain", {})
            _, kwargs = MockReq.call_args
            assert kwargs["headers"]["Content-Type"] == "application/json"
            assert kwargs["headers"]["Authorization"] == "Bearer tok"

    def test_http_error_non_json_with_code_in_response(self):
        err_body = b"not json"
        err = urllib.error.HTTPError(
            url="http://example.com/x", code=502, msg="bad gateway", hdrs=None, fp=MagicMock()
        )
        err.read = MagicMock(return_value=err_body)
        err.fp = MagicMock()
        err.fp.read = MagicMock(return_value=err_body)
        with patch("codebot.control_client.urllib.request.Request"), \
             patch("codebot.control_client.urllib.request.urlopen", side_effect=err):
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    code, data = cc.req("GET", "/x")
            assert code == 502
            assert data["code"] == 502
            assert data["error"] == "not json"

    def test_req_returns_tuple_of_two(self):
        with patch("codebot.control_client.urllib.request.Request"), \
             patch("codebot.control_client.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(b'{"a":1}', 200)
            with patch.object(cc, "URL", "http://example.com"):
                with patch.object(cc, "TOKEN", ""):
                    result = cc.req("GET", "/x")
            assert isinstance(result, tuple)
            assert len(result) == 2
