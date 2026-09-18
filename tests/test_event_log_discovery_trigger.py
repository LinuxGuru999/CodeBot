"""Test that discovery-trigger is a valid event type in event_log."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.event_log import ALLOWED_EVENT_TYPES


class TestDiscoveryTriggerEventType:
    """Verify discovery-trigger is accepted by event_log."""

    def test_discovery_trigger_in_allowed_types(self) -> None:
        assert "discovery-trigger" in ALLOWED_EVENT_TYPES

    def test_telemetry_still_in_allowed_types(self) -> None:
        assert "telemetry" in ALLOWED_EVENT_TYPES
