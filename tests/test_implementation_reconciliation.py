from pathlib import Path

from codebot.ticket_engine import (
    RiskLevel,
    Severity,
    TicketClass,
    TicketState,
    TicketStore,
    create_ticket,
)
from codebot.ticket_dispatcher import reconcile_implementation_backlog


def _implementing_store(state_dir: Path) -> tuple[TicketStore, str]:
    store = TicketStore(state_dir / "tickets.json")
    ticket = create_ticket(
        "recover stranded implementation",
        TicketClass.BUG,
        Severity.MEDIUM,
        "test",
        "evidence",
        "problem",
        "desired",
        ["recover"],
        risk=RiskLevel.LOW,
    )
    store.add(ticket)
    store.transition(ticket.id, TicketState.VALIDATING)
    store.transition(ticket.id, TicketState.TRIAGED)
    store.transition(ticket.id, TicketState.READY)
    store.transition(ticket.id, TicketState.IMPLEMENTATION_READY)
    store.transition(ticket.id, TicketState.IMPLEMENTING)
    return store, ticket.id


def test_reconciliation_releases_dead_implementation_claim(tmp_path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store, ticket_id = _implementing_store(state_dir)
    claims = state_dir / "claims"
    claims.mkdir()
    claim = claims / f"{ticket_id}.general_implementer-1.json"
    claim.write_text(
        '{"ticket_id": "' + ticket_id + '", "worker": "general_implementer-1"}',
        encoding="utf-8",
    )
    monkeypatch.setattr("codebot.ticket_dispatcher.STATE_DIR", state_dir)
    monkeypatch.setattr("codebot.ticket_dispatcher.write_implementation_packet", lambda ticket: tmp_path)

    result = reconcile_implementation_backlog(store, {})

    assert result.released_claims == 1
    assert not claim.exists()
    assert store.get(ticket_id).state == TicketState.IMPLEMENTATION_READY
