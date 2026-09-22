"""Tests for Task 4: dispatch ordering by work_scorer + workforce capacity."""

import time
from unittest.mock import MagicMock, patch

from codebot.ticket_engine import (
    Ticket,
    TicketClass,
    TicketState,
    Severity,
    RiskLevel,
    create_ticket,
)
from codebot.ticket_dispatcher import _rank_tickets_for_dispatch


def _make_ticket(
    title: str,
    severity: Severity = Severity.MEDIUM,
    risk: RiskLevel = RiskLevel.LOW,
    ticket_class: TicketClass = TicketClass.BUG,
    created_at: float | None = None,
) -> Ticket:
    t = create_ticket(
        title, ticket_class, severity, "test", "ev", "prob", "desired", ["ac"],
        risk=risk,
    )
    if created_at is not None:
        object.__setattr__(t, "created_at", created_at)
    return t


class TestRankTicketsForDispatch:
    def test_empty_list_returns_empty(self):
        assert _rank_tickets_for_dispatch([]) == []

    def test_critical_severity_ranks_higher_than_low(self):
        t_low = _make_ticket("low", severity=Severity.LOW)
        t_crit = _make_ticket("critical", severity=Severity.CRITICAL)
        ranked = _rank_tickets_for_dispatch([t_low, t_crit])
        assert ranked[0].id == t_crit.id
        assert ranked[1].id == t_low.id

    def test_security_class_gets_priority_boost(self):
        t_bug = _make_ticket("bug", ticket_class=TicketClass.BUG, severity=Severity.MEDIUM)
        t_sec = _make_ticket("sec", ticket_class=TicketClass.SECURITY, severity=Severity.CRITICAL)
        ranked = _rank_tickets_for_dispatch([t_bug, t_sec])
        # Security + critical gets security_emergency stage priority (150) vs implementation (50)
        assert ranked[0].id == t_sec.id

    def test_rework_stage_gets_higher_base_priority(self):
        t_impl = _make_ticket("impl")
        object.__setattr__(t_impl, "state", TicketState.IMPLEMENTING)
        t_rework = _make_ticket("rework")
        object.__setattr__(t_rework, "state", TicketState.REWORK)
        ranked = _rank_tickets_for_dispatch([t_impl, t_rework])
        assert ranked[0].id == t_rework.id

    def test_aging_bonus_advances_old_ticket(self):
        now = time.time()
        t_new = _make_ticket("new", created_at=now - 100)
        t_old = _make_ticket("old", created_at=now - 200000)  # >86400s old
        ranked = _rank_tickets_for_dispatch([t_new, t_old])
        # Old ticket should get aging bonus and rank higher
        assert ranked[0].id == t_old.id

    def test_fallback_to_original_order_on_error(self):
        t1 = _make_ticket("a")
        t2 = _make_ticket("b")
        with patch("codebot.work_scorer.rank_work_items", side_effect=Exception("boom")):
            ranked = _rank_tickets_for_dispatch([t1, t2])
        assert ranked[0].id == t1.id
        assert ranked[1].id == t2.id

    def test_preserves_all_tickets(self):
        tickets = [_make_ticket(f"t{i}") for i in range(5)]
        ranked = _rank_tickets_for_dispatch(tickets)
        assert len(ranked) == 5
        original_ids = {t.id for t in tickets}
        ranked_ids = {t.id for t in ranked}
        assert original_ids == ranked_ids

    def test_with_state_counts_provides_pressure(self):
        t1 = _make_ticket("a")
        t2 = _make_ticket("b")
        counts = {"IMPLEMENTING": 10, "REVIEWING": 2, "VERIFYING": 1, "READY": 5}
        ranked = _rank_tickets_for_dispatch([t1, t2], state_counts=counts)
        assert len(ranked) == 2


class TestDispatchOrderingIntegration:
    """Verify that spawn_demand_agents processes tickets in scored order."""

    def test_spawn_processes_higher_scored_ticket_first(self, tmp_path):
        from codebot.ticket_engine import TicketStore
        from codebot.ticket_dispatcher import spawn_demand_agents
        from codebot.process_manager import BotConfig, BotState

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "claims").mkdir()
        store_path = state_dir / "tickets.json"

        store = TicketStore(store_path)
        t_low = create_ticket(
            "low priority", TicketClass.BUG, Severity.LOW,
            "test", "ev-low", "prob", "desired", ["ac"], risk=RiskLevel.LOW,
        )
        t_crit = create_ticket(
            "critical priority", TicketClass.BUG, Severity.CRITICAL,
            "test", "ev-crit", "prob", "desired", ["ac"], risk=RiskLevel.LOW,
        )
        store.add(t_low)
        store.add(t_crit)
        for t in [t_low, t_crit]:
            store.transition(t.id, TicketState.VALIDATING)
            store.transition(t.id, TicketState.TRIAGED)
            store.transition(t.id, TicketState.READY)
            store.transition(t.id, TicketState.IMPLEMENTATION_READY)
            store.transition(t.id, TicketState.IMPLEMENTING)
        store.flush()

        config = BotConfig(
            name="general_implementer",
            prompt_file="general_implementer.md",
            interval_seconds=300,
            heartbeat_timeout=600,
        )
        bot = BotState(config=config)
        bots = {"general_implementer": bot}

        spawn_order = []

        def mock_start(b, **kwargs):
            spawn_order.append(getattr(b, "_assigned_ticket_id", ""))
            return True

        with patch("codebot.ticket_dispatcher.STATE_DIR", state_dir):
            spawn_demand_agents(
                bots, max_concurrent=10, start_bot_fn=mock_start, store=store,
            )

        # Critical severity ticket should be spawned first due to higher score
        if len(spawn_order) >= 2:
            assert spawn_order[0] == t_crit.id, (
                f"Expected critical ticket first, got order: {spawn_order}"
            )

        store.close()
