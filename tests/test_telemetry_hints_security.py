"""Security regression tests for CB-D8B1325BF5C718159CA39AB204686C5F.

Verifies that telemetry 401 hint strings do not leak CONTROL_TOKEN
relationship per Constitution §2 secrets isolation.
"""
import codebot.control_server as cs


def test_telemetry_unauthorized_hint_no_control_token():
    """TELEMETRY_UNAUTHORIZED_HINT must not mention CONTROL_TOKEN."""
    assert "CONTROL_TOKEN" not in cs.TELEMETRY_UNAUTHORIZED_HINT, (
        "TELEMETRY_UNAUTHORIZED_HINT leaks CONTROL_TOKEN"
    )
    assert "CODEBOT_TELEMETRY_TOKEN" in cs.TELEMETRY_UNAUTHORIZED_HINT
    assert "Authorization: Bearer" in cs.TELEMETRY_UNAUTHORIZED_HINT


def test_telemetry_not_configured_hint_no_control_token():
    """TELEMETRY_NOT_CONFIGURED_HINT must not suggest CONTROL_TOKEN."""
    assert "CONTROL_TOKEN" not in cs.TELEMETRY_NOT_CONFIGURED_HINT, (
        "TELEMETRY_NOT_CONFIGURED_HINT leaks CONTROL_TOKEN"
    )
    assert "CODEBOT_TELEMETRY_TOKEN" in cs.TELEMETRY_NOT_CONFIGURED_HINT
    # Old buggy string contained "Neither" and "nor CONTROL_TOKEN"
    assert "Neither" not in cs.TELEMETRY_NOT_CONFIGURED_HINT


def test_control_unauthorized_hint_still_references_control_token():
    """CONTROL_UNAUTHORIZED_HINT must still guide control-plane operators."""
    assert "CONTROL_TOKEN" in cs.CONTROL_UNAUTHORIZED_HINT


def test_no_telemetry_hint_reveals_cross_token_relationship():
    """No telemetry error hint should contain cross-token language."""
    for hint_name in ("TELEMETRY_UNAUTHORIZED_HINT", "TELEMETRY_NOT_CONFIGURED_HINT"):
        hint = getattr(cs, hint_name)
        assert "(or CONTROL_TOKEN)" not in hint, f"{hint_name} still contains fallback phrase"
        assert "nor CONTROL_TOKEN" not in hint, f"{hint_name} still contains nor phrase"
