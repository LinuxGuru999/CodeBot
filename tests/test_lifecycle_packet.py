from types import SimpleNamespace

from codebot.lifecycle_packet import LifecyclePacketStore


def _ticket(revision: float = 1.0):
    return SimpleNamespace(
        id="CB-packet",
        updated_at=revision,
        risk="medium",
        affected_modules=["codebot/example.py"],
        acceptance_criteria=["works"],
        state="PLANNING",
    )


def test_transition_creates_packet_with_stage_history(tmp_path):
    store = LifecyclePacketStore(tmp_path)
    store.record_transition(_ticket(), "DECOMPOSE", "decomposer")
    packet = store.load("CB-packet")
    assert packet is not None
    assert packet["stages"] == [{"from": "DECOMPOSE", "to": "PLANNING", "at": 1.0, "actor": "decomposer"}]


def test_evidence_requires_the_current_ticket_revision(tmp_path):
    store = LifecyclePacketStore(tmp_path)
    store.record_transition(_ticket(), "DECOMPOSE")
    assert not store.append_evidence("CB-packet", 2.0, "planning", {"plan": "stale"})
    assert store.append_evidence("CB-packet", 1.0, "planning", {"plan": "current"})
    packet = store.load("CB-packet")
    assert packet is not None
    assert packet["evidence"]["planning"]["data"] == {"plan": "current"}


def test_participants_are_unique_and_completion_marker_is_idempotent(tmp_path):
    store = LifecyclePacketStore(tmp_path)
    store.record_transition(_ticket(), "DECOMPOSE", "decomposer")
    store.record_participants("CB-packet", ["implementer", "decomposer", "reviewer"])

    assert store.participants("CB-packet") == ["decomposer", "implementer", "reviewer"]
    assert store.mark_completion_rewards_recorded("CB-packet")
    assert not store.mark_completion_rewards_recorded("CB-packet")
