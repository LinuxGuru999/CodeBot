"""Tests for dispatch_service.py — ticket transitions on bot exit."""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.dispatch_service import (
    transition_ticket_on_success,
    transition_ticket_on_error,
    batch_read_bot_statuses,
)
from codebot.ticket_dispatcher import IMPLEMENTATION_ROLE_ORDER
from codebot.ticket_engine import TicketState, TicketClass, Severity, RiskLevel, create_ticket, TicketStore
from codebot.process_manager import BotConfig, BotState


class TestTransitionTicketOnError:
    def _make_bot(self, ticket_id: str = "") -> BotState:
        config = BotConfig("test_bot", "prompt.md", 600, 1200, "default")
        bot = BotState(config=config)
        if ticket_id:
            bot._assigned_ticket_id = ticket_id
        return bot

    def test_implementer_error_returns_ticket_to_rework(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket(
            title="Test bug",
            ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM,
            source="test",
            evidence="implementation-error-evidence",
            problem_statement="problem",
            desired_state="desired",
            acceptance_criteria=["ac"],
            risk=RiskLevel.LOW,
        )
        store.add(t)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.GOAL)
        store.transition(t.id, TicketState.DECOMP)
        store.transition(t.id, TicketState.PLANNING)
        store.transition(t.id, TicketState.IMPLEMENT)
        bot = self._make_bot(t.id)
        transition_ticket_on_error(bot, {"test_bot": bot}, exit_code=1, store=store)
        updated = store.get(t.id)
        assert updated is not None
        assert updated.state == TicketState.REWORK
        assert bot._assigned_ticket_id == ""
        store.close()

    def test_error_exit_different_codes(self, tmp_path):
        for exit_code in [1, 2, 4, 127, 255]:
            store_path = tmp_path / "tickets.json"
            # Remove previous file if exists from prior iteration
            if store_path.exists():
                store_path.unlink()
            store = TicketStore(store_path)

            t = create_ticket(
                title=f"Test bug {exit_code}",
                ticket_class=TicketClass.BUG,
                severity=Severity.LOW,
                source="test",
                evidence=f"evidence-{exit_code}",
                problem_statement="problem",
                desired_state="desired",
                acceptance_criteria=["ac"],
                risk=RiskLevel.LOW,
            )
            store.add(t)
            store.transition(t.id, TicketState.TRIAGED)
            store.transition(t.id, TicketState.GOAL)
            store.transition(t.id, TicketState.DECOMP)
            store.transition(t.id, TicketState.PLANNING)
            store.transition(t.id, TicketState.IMPLEMENT)
            bot = self._make_bot(t.id)
            transition_ticket_on_error(bot, {"test_bot": bot}, exit_code=exit_code, store=store)
            updated = store.get(t.id)
            assert updated is not None
            assert updated.state == TicketState.REWORK
            store.close()

    def test_error_exit_no_assigned_ticket(self, tmp_path):
        """Error exit with no assigned ticket should be a no-op."""
        bot = self._make_bot()  # No ticket assigned
        bots = {"test_bot": bot}

        # Should not raise (no store needed when no ticket is assigned)
        transition_ticket_on_error(bot, bots, exit_code=1)
        assert getattr(bot, '_assigned_ticket_id', '') == ""

    def test_error_exit_ticket_not_in_implementing(self, tmp_path):
        """Error exit when ticket is not in IMPLEMENTING should not transition."""
        store_path = tmp_path / "tickets.json"
        store = TicketStore(store_path)

        t = create_ticket(
            title="Test bug",
            ticket_class=TicketClass.BUG,
            severity=Severity.LOW,
            source="test",
            evidence="evidence",
            problem_statement="problem",
            desired_state="desired",
            acceptance_criteria=["ac"],
            risk=RiskLevel.LOW,
        )
        store.add(t)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.GOAL)
        store.flush()

        bot = self._make_bot(t.id)
        bots = {"test_bot": bot}

        # Pass store directly (single-instance-per-tick API from CB-9165641-2750)
        transition_ticket_on_error(bot, bots, exit_code=1, store=store)

        updated = store.get(t.id)
        # Ticket should remain in GOAL state since it's not in IMPLEMENT
        assert updated.state == TicketState.GOAL
        store.close()

    def test_error_exit_cleans_claims(self, tmp_path):
        """Error exit should clean up claim files."""
        store_path = tmp_path / "tickets.json"
        store = TicketStore(store_path)

        t = create_ticket(
            title="Test bug",
            ticket_class=TicketClass.BUG,
            severity=Severity.LOW,
            source="test",
            evidence="evidence",
            problem_statement="problem",
            desired_state="desired",
            acceptance_criteria=["ac"],
            risk=RiskLevel.LOW,
        )
        store.add(t)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.GOAL)
        store.transition(t.id, TicketState.DECOMP)
        store.transition(t.id, TicketState.PLANNING)
        store.transition(t.id, TicketState.IMPLEMENT)

        # Create a claim file
        claims_dir = tmp_path / "claims"
        claims_dir.mkdir(exist_ok=True)
        claim_file = claims_dir / f"{t.id}.test_agent.json"
        claim_file.write_text('{"claim": "test"}')

        bot = self._make_bot(t.id)
        bots = {"test_bot": bot}

        # Pass store directly AND patch STATE_DIR for claims cleanup path
        import codebot.dispatch_service as ds
        with patch.object(ds, 'STATE_DIR', tmp_path):
            transition_ticket_on_error(bot, bots, exit_code=1, store=store)

        assert not claim_file.exists()
        store.close()


class TestTransitionTicketOnSuccess:
    """Test that clean exits transition tickets correctly."""

    def _make_bot(self, ticket_id: str = "", name: str = "implementer") -> BotState:
        config = BotConfig(name, "prompt.md", 600, 1200, "default")
        bot = BotState(config=config)
        if ticket_id:
            bot._assigned_ticket_id = ticket_id
        return bot

    def test_implementer_success_requires_every_role_before_review(self, tmp_path):
        """A ticket enters review only after every implementation role approves."""
        store_path = tmp_path / "tickets.json"
        store = TicketStore(store_path)

        t = create_ticket(
            title="Test bug",
            ticket_class=TicketClass.BUG,
            severity=Severity.LOW,
            source="test",
            evidence="evidence",
            problem_statement="problem",
            desired_state="desired",
            acceptance_criteria=["ac"],
            risk=RiskLevel.LOW,
        )
        store.add(t)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.GOAL)
        store.transition(t.id, TicketState.DECOMP)
        store.transition(t.id, TicketState.PLANNING)
        store.transition(t.id, TicketState.IMPLEMENT)

        for role in IMPLEMENTATION_ROLE_ORDER[:-1]:
            bot = self._make_bot(t.id, name=role)
            transition_ticket_on_success(bot, {role: bot}, store=store)
            updated = store.get(t.id)
            assert updated is not None
            assert updated.state == TicketState.IMPLEMENT
            assert updated.implementation_approvals[-1] == role

        last_role = IMPLEMENTATION_ROLE_ORDER[-1]
        bot = self._make_bot(t.id, name=last_role)
        transition_ticket_on_success(bot, {last_role: bot}, store=store)

        updated = store.get(t.id)
        assert updated is not None
        assert updated.state == TicketState.REVIEW
        assert updated.implementation_approvals == list(IMPLEMENTATION_ROLE_ORDER)
        store.close()

    def test_planner_success_moves_ticket_to_implement(self, tmp_path):
        """PLANNING tickets enter IMPLEMENT only after planner completion."""
        store = TicketStore(tmp_path / "tickets.json")
        ticket = create_ticket(
            title="Test bug",
            ticket_class=TicketClass.BUG,
            severity=Severity.LOW,
            source="test",
            evidence="planner-evidence",
            problem_statement="problem",
            desired_state="desired",
            acceptance_criteria=["ac"],
            risk=RiskLevel.LOW,
        )
        store.add(ticket)
        store.transition(ticket.id, TicketState.TRIAGED)
        store.transition(ticket.id, TicketState.GOAL)
        store.transition(ticket.id, TicketState.DECOMP)
        store.transition(ticket.id, TicketState.PLANNING)

        bot = self._make_bot(ticket.id, name="planner")
        transition_ticket_on_success(bot, {"planner": bot}, store=store)

        updated = store.get(ticket.id)
        assert updated is not None
        assert updated.state == TicketState.IMPLEMENT
        store.close()

    def test_reviewer_success_does_not_transition_prematurely(self, tmp_path):
        """Reviewer success should NOT transition ticket — advance_reviewed_tickets owns that decision."""
        store_path = tmp_path / "tickets.json"
        store = TicketStore(store_path)

        t = create_ticket(
            title="Test bug",
            ticket_class=TicketClass.BUG,
            severity=Severity.LOW,
            source="test",
            evidence="evidence",
            problem_statement="problem",
            desired_state="desired",
            acceptance_criteria=["ac"],
            risk=RiskLevel.LOW,
        )
        store.add(t)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.GOAL)
        store.transition(t.id, TicketState.DECOMP)
        store.transition(t.id, TicketState.PLANNING)
        store.transition(t.id, TicketState.IMPLEMENT)
        store.transition(t.id, TicketState.REVIEW)
        store.flush()

        bot = self._make_bot(t.id, name="correctness_reviewer")
        bots = {"correctness_reviewer": bot}

        # Pass store directly (single-instance-per-tick API from CB-9165641-2750)
        transition_ticket_on_success(bot, bots, store=store)

        updated = store.get(t.id)
        assert updated.state == TicketState.REVIEW
        store.close()


# -----------------------------------------------------------------------
# batch_read_bot_statuses
# -----------------------------------------------------------------------
class TestBatchReadBotStatuses:
    """Tests for batch_read_bot_statuses() — single-pass status reading."""

    def test_reads_valid_status_json(self, tmp_path):
        """Returns parsed dict for bots with valid .status.json files."""
        for name in ["bot-a", "bot-b"]:
            data = {"status": "running", "current_task": "coding", "iteration": 3}
            (tmp_path / f"{name}.status.json").write_text(json.dumps(data))
        import codebot.dispatch_service as ds
        with patch.object(ds, "STATE_DIR", tmp_path):
            result = batch_read_bot_statuses(["bot-a", "bot-b"])
        assert result["bot-a"]["status"] == "running"
        assert result["bot-a"]["current_task"] == "coding"
        assert result["bot-b"]["iteration"] == 3

    def test_missing_file_returns_none(self, tmp_path):
        """Missing .status.json files produce None entries."""
        import codebot.dispatch_service as ds
        with patch.object(ds, "STATE_DIR", tmp_path):
            result = batch_read_bot_statuses(["no-such-bot"])
        assert result["no-such-bot"] is None

    def test_corrupt_json_returns_none(self, tmp_path):
        """Corrupt JSON files produce None entries."""
        (tmp_path / "bad-bot.status.json").write_text("not json")
        import codebot.dispatch_service as ds
        with patch.object(ds, "STATE_DIR", tmp_path):
            result = batch_read_bot_statuses(["bad-bot"])
        assert result["bad-bot"] is None

    def test_empty_list_returns_empty_dict(self, tmp_path):
        """Empty input returns empty dict."""
        import codebot.dispatch_service as ds
        with patch.object(ds, "STATE_DIR", tmp_path):
            result = batch_read_bot_statuses([])
        assert result == {}

    def test_non_dict_json_returns_none(self, tmp_path):
        """Non-dict JSON (e.g., a list) produces None."""
        (tmp_path / "list-bot.status.json").write_text('[1, 2, 3]')
        import codebot.dispatch_service as ds
        with patch.object(ds, "STATE_DIR", tmp_path):
            result = batch_read_bot_statuses(["list-bot"])
        assert result["list-bot"] is None

    def test_mixed_valid_and_missing(self, tmp_path):
        """Mix of existing and missing status files handled correctly."""
        data = {"status": "running"}
        (tmp_path / "exists.status.json").write_text(json.dumps(data))
        import codebot.dispatch_service as ds
        with patch.object(ds, "STATE_DIR", tmp_path):
            result = batch_read_bot_statuses(["exists", "missing"])
        assert result["exists"]["status"] == "running"
        assert result["missing"] is None

    def test_batch_read_bot_statuses_with_sample_size(self, tmp_path):
        """batch_read_bot_statuses respects sample_size parameter."""
        # Create 10 status files
        for i in range(10):
            data = {"current_task": f"task-{i}", "iteration": i}
            (tmp_path / f"bot-{i}.status.json").write_text(json.dumps(data))
        
        bot_names = [f"bot-{i}" for i in range(10)]
        import codebot.dispatch_service as ds
        with patch.object(ds, "STATE_DIR", tmp_path):
            # Sample only 3 bots
            result = batch_read_bot_statuses(bot_names, sample_size=3)
        
        # Should only read 3 bots
        assert len(result) == 3
        # All results should be valid
        for name, data in result.items():
            assert data is not None
            assert "current_task" in data

    def test_batch_read_bot_statuses_sample_size_none_reads_all(self, tmp_path):
        """batch_read_bot_statuses with sample_size=None reads all bots."""
        # Create 5 status files
        for i in range(5):
            data = {"current_task": f"task-{i}"}
            (tmp_path / f"bot-{i}.status.json").write_text(json.dumps(data))
        
        bot_names = [f"bot-{i}" for i in range(5)]
        import codebot.dispatch_service as ds
        with patch.object(ds, "STATE_DIR", tmp_path):
            result = batch_read_bot_statuses(bot_names, sample_size=None)
        
        # Should read all 5 bots
        assert len(result) == 5
