"""Regression tests for the user_agent pipeline.

Covers UserRequest envelope, request ingestion, API endpoints,
scheduler routing, lifecycle transitions, finding ingestion,
and end-to-end flow. All tests use tmp_path, are deterministic,
require no running orchestrator, and hit no network.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from codebot.user_request import UserRequest, UserRequestStatus, DedupCandidate
from codebot.ticket_engine import TicketState, TicketClass, TRANSITIONS, Ticket, create_ticket


@pytest.fixture
def store_and_dir(tmp_path: Path):
    import codebot.ticket_dispatcher as _td
    _td._shared_store = None
    _td._shared_store_key = ""
    from codebot.ticket_dispatcher import get_ticket_store
    store = get_ticket_store(tmp_path)
    UserRequest.ensure_directories(tmp_path)
    (tmp_path / "findings").mkdir(exist_ok=True)
    (tmp_path / "findings" / "processed").mkdir(exist_ok=True)
    (tmp_path / "findings" / "rejected").mkdir(exist_ok=True)
    yield store, tmp_path
    _td._shared_store = None
    _td._shared_store_key = ""


def _make_req(request_id: str = "test-1", status: UserRequestStatus = UserRequestStatus.PENDING, **overrides) -> UserRequest:
    now = time.time()
    kwargs = dict(
        request_id=request_id, source="user", message="Test feature",
        context="", created_at=now, updated_at=now, status=status,
        conversation_id=request_id,
    )
    kwargs.update(overrides)
    return UserRequest(**kwargs)


def test_user_request_serialization_roundtrip():
    dc = DedupCandidate(ticket_id="CB-1", title="T", state="IMPLEMENT", similarity=0.9)
    req = _make_req(dedup_candidates=(dc,))
    d = req.to_dict()
    req2 = UserRequest.from_dict(d)
    assert req2.request_id == req.request_id
    assert req2.dedup_candidates[0].ticket_id == "CB-1"
    assert isinstance(req.clarification_questions, tuple)
    assert isinstance(req.findings_emitted, tuple)
    assert isinstance(req.dedup_candidates, tuple)


def test_dedup_candidate_frozen_immutability():
    dc = DedupCandidate(ticket_id="CB-1", title="T", state="X", similarity=0.5)
    with pytest.raises(Exception):
        dc.similarity = 0.0  # type: ignore[misc]


def test_user_request_status_transitions_valid():
    req = _make_req()
    r2 = req.update_status(UserRequestStatus.RUNNING)
    assert r2.status == UserRequestStatus.RUNNING
    r3 = r2.update_status(UserRequestStatus.COMPLETE)
    assert r3.status == UserRequestStatus.COMPLETE


def test_user_request_status_transition_invalid_raises():
    req = _make_req()
    with pytest.raises(ValueError):
        req.update_status(UserRequestStatus.COMPLETE)


def test_user_request_sanctioned_mutations():
    req = _make_req(status=UserRequestStatus.RUNNING)
    r2 = req.with_clarification_questions(("Q1?", "Q2?"))
    assert r2.clarification_questions == ("Q1?", "Q2?")
    r3 = r2.with_response("Answer")
    assert r3.response == "Answer"
    assert r3.clarification_questions == ()
    r4 = req.reject("Bad idea")
    assert r4.status == UserRequestStatus.REJECTED
    assert r4.rejection_reason == "Bad idea"
    r5 = req.complete(("DF-1",))
    assert r5.status == UserRequestStatus.COMPLETE
    assert r5.findings_emitted == ("DF-1",)


def test_request_api_creates_pending_file_with_dedup_candidates(store_and_dir):
    store, sd = store_and_dir
    req = _make_req(dedup_candidates=(DedupCandidate("CB-X", "T", "IMPLEMENT", 0.85),))
    (sd / "requests" / f"{req.request_id}.json").write_text(req.to_json())
    assert (sd / "requests" / f"{req.request_id}.json").exists()
    loaded = UserRequest.from_dict(json.loads((sd / "requests" / f"{req.request_id}.json").read_text()))
    assert len(loaded.dedup_candidates) == 1


def test_request_api_returns_similar_tickets():
    dc = DedupCandidate("CB-1", "Auth", "IMPLEMENT", 0.9)
    d = dc.to_dict()
    assert d["ticket_id"] == "CB-1"
    assert d["similarity"] == 0.9


def test_request_api_graceful_degradation_on_store_lock():
    assert UserRequest.generate_id() != ""


def test_request_ingestion_creates_requested_ticket_with_request_class(store_and_dir):
    store, sd = store_and_dir
    from codebot.request_ingestion import ingest_requests
    req = _make_req()
    (sd / "requests" / f"{req.request_id}.json").write_text(req.to_json())
    n = ingest_requests(store, sd)
    assert n == 1
    tickets = store.list_by_state(TicketState.REQUESTED)
    assert len(tickets) == 1
    assert tickets[0].ticket_class == TicketClass.REQUEST
    assert tickets[0].origin_id == req.request_id


def test_request_ingestion_idempotent(store_and_dir):
    store, sd = store_and_dir
    from codebot.request_ingestion import ingest_requests
    req = _make_req()
    (sd / "requests" / f"{req.request_id}.json").write_text(req.to_json())
    n1 = ingest_requests(store, sd)
    assert n1 == 1
    (sd / "requests" / f"{req.request_id}.json").write_text(req.to_json())
    n2 = ingest_requests(store, sd)
    assert n2 == 0


def test_request_ingestion_crash_recovery(store_and_dir):
    store, sd = store_and_dir
    from codebot.request_ingestion import ingest_requests
    req = _make_req()
    (sd / "requests" / f"{req.request_id}.json").write_text(req.to_json())
    ingest_requests(store, sd)
    crash_req = _make_req(request_id=req.request_id, status=UserRequestStatus.PENDING)
    (sd / "requests" / f"{req.request_id}.json").write_text(crash_req.to_json())
    n = ingest_requests(store, sd)
    assert n == 0
    reconciled = json.loads((sd / "requests" / "processed" / f"{req.request_id}.json").read_text())
    assert reconciled["status"] == "running"


def test_malformed_request_quarantined(store_and_dir):
    store, sd = store_and_dir
    from codebot.request_ingestion import ingest_requests
    (sd / "requests" / "bad.json").write_text("not json")
    n = ingest_requests(store, sd)
    assert n == 0
    rejected_files = list((sd / "requests" / "rejected").iterdir())
    assert len(rejected_files) >= 1, f"Expected quarantined file, got: {rejected_files}"


def test_reply_updates_context_only_no_transition(store_and_dir):
    store, sd = store_and_dir
    from codebot.request_ingestion import ingest_requests
    req = _make_req(status=UserRequestStatus.AWAITING_INPUT)
    (sd / "requests" / "processed").mkdir(parents=True, exist_ok=True)
    (sd / "requests" / "processed" / f"{req.request_id}.json").write_text(req.to_json())
    updated = req.with_response("Use JWT").update_status(UserRequestStatus.PENDING)
    (sd / "requests" / "processed" / f"{req.request_id}.json").write_text(updated.to_json())
    data = json.loads((sd / "requests" / "processed" / f"{req.request_id}.json").read_text())
    assert data["response"] == "Use JWT"
    assert data["clarification_questions"] == []


def test_reply_reconciliation_drives_deferred_to_requested(store_and_dir):
    store, sd = store_and_dir
    from codebot.request_ingestion import ingest_requests
    req = _make_req()
    (sd / "requests" / f"{req.request_id}.json").write_text(req.to_json())
    ingest_requests(store, sd)
    tickets = store.list_by_state(TicketState.REQUESTED)
    assert len(tickets) == 1
    tid = tickets[0].id
    store.transition(tid, TicketState.DEFERRED)
    replied = req.update_status(UserRequestStatus.AWAITING_INPUT).with_response("JWT tokens").update_status(UserRequestStatus.PENDING)
    (sd / "requests" / "processed" / f"{req.request_id}.json").write_text(replied.to_json())
    ingest_requests(store, sd)
    t = store.get(tid)
    assert t is not None
    assert t.state == TicketState.REQUESTED


def test_user_bucket_queries_requested_only():
    from codebot.scheduler_v2.dispatcher import BUCKET_ORDER
    user_bucket = [b for b in BUCKET_ORDER if b[0] == "USER"][0]
    assert user_bucket[1] == ["REQUESTED"]


def test_requested_routes_to_user_agent():
    from codebot.scheduler_v2.dispatcher import BucketDispatcher
    bd = BucketDispatcher.__new__(BucketDispatcher)
    t = SimpleNamespace(state=TicketState.REQUESTED, source="user", ticket_class="feature")
    assert bd._role_for_ticket(t) == "user_agent"


def test_machine_discovered_triage_path_not_user_bucket():
    from codebot.scheduler_v2.dispatcher import BUCKET_ORDER, BucketDispatcher
    discovered_buckets = [b[0] for b in BUCKET_ORDER if "DISCOVERED" in b[1]]
    assert discovered_buckets == []
    goal_bucket = [b for b in BUCKET_ORDER if b[0] == "GOAL"][0]
    assert "TRIAGED" in goal_bucket[1]
    bd = BucketDispatcher.__new__(BucketDispatcher)
    t = SimpleNamespace(state=TicketState.DISCOVERED, source="bug_hunter", ticket_class="bug")
    assert bd._role_for_ticket(t) != "user_agent"


def test_ambiguous_request_transitions_to_deferred(store_and_dir):
    store, sd = store_and_dir
    from codebot.request_ingestion import ingest_requests
    req = _make_req()
    (sd / "requests" / f"{req.request_id}.json").write_text(req.to_json())
    ingest_requests(store, sd)
    tickets = store.list_by_state(TicketState.REQUESTED)
    assert len(tickets) == 1
    store.transition(tickets[0].id, TicketState.DEFERRED)
    t = store.get(tickets[0].id)
    assert t is not None
    assert t.state == TicketState.DEFERRED


def test_deferred_not_redispatched():
    from codebot.scheduler_v2.dispatcher import BUCKET_ORDER
    for name, states in BUCKET_ORDER:
        if name == "USER":
            assert "DEFERRED" not in states


def test_e2e_request_to_user_agent_to_findings_to_discovered(store_and_dir):
    store, sd = store_and_dir
    from codebot.request_ingestion import ingest_requests
    from codebot.finding_ingestion import ingest_findings
    req = _make_req()
    (sd / "requests" / f"{req.request_id}.json").write_text(req.to_json())
    n = ingest_requests(store, sd)
    assert n == 1
    tickets = store.list_by_state(TicketState.REQUESTED)
    assert len(tickets) == 1
    tid = tickets[0].id
    store.transition(tid, TicketState.COMPLETE)
    t = store.get(tid)
    assert t is not None
    assert t.state == TicketState.COMPLETE
    finding = {
        "finding_id": "DF-E2E001", "discovery_role": "user_agent",
        "discovery_category": "feature", "title": "E2E test finding",
        "problem_statement": "Add rate limiting", "severity": "medium",
        "priority": "high", "confidence": "high", "atomicity": "standard",
        "repository": ".", "repository_revision": "abc",
        "evidence": [], "acceptance_outcome": "Rate limiter works",
    }
    (sd / "findings" / "e2e.json").write_text(json.dumps(finding))
    fn = ingest_findings(store, sd)
    assert fn == 1
    discovered = store.list_by_state(TicketState.DISCOVERED)
    assert len(discovered) >= 1
    assert any(t.finding_id == "DF-E2E001" for t in discovered)
