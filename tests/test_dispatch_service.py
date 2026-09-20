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
)
from codebot.ticket_engine import TicketState, TicketClass, Severity, RiskLevel, create_ticket, TicketStore
from codebot.process_manager import BotConfig, BotState


class TestTransitionTicketOnError:
    """Test that error exits transition tickets back to READY."""

    def _make_bot(self, ticket_id: str = "") -> BotState:
        config = BotConfig("test_bot", "prompt.md", 600, 1200, "default")
        bot = BotState(config=config)
        if ticket_id:
            bot._assigned_ticket_id = ticket_id
        return bot

    def test_error_exit_transitions_to_ready(self, tmp_path):
        """Error exit (exit_code != 0 and != 3) should transition ticket to READY."""
        store_path = tmp_path / "tickets.json"
        store = TicketStore(store_path)
        
        # Create a ticket in IMPLEMENTING state
        t = create_ticket(
            title="Test bug",
            ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM,
            source="test",
            evidence="evidence",
            problem_statement="problem",
            desired_state="desired",
            acceptance_criteria=["ac"],
            risk=RiskLevel.LOW,
        )
        store.add(t)
        store.transition(t.id, TicketState.VALIDATING)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.READY)
        store.transition(t.id, TicketState.IMPLEMENTING)
        store.flush()
        store.close()  # Close to prevent WAL interference
        
        # Verify ticket is in IMPLEMENTING
        store_check = TicketStore(store_path)
        assert store_check.get(t.id).state == TicketState.IMPLEMENTING
        store_check.close()
        
        # Create a bot with the ticket assigned
        bot = self._make_bot(t.id)
        bots = {"test_bot": bot}
        
        # Patch STATE_DIR in dispatch_service to use tmp_path
        import codebot.dispatch_service as ds
        with patch.object(ds, 'STATE_DIR', tmp_path):
            transition_ticket_on_error(bot, bots, exit_code=1)
        
        # Reload store to see changes
        store2 = TicketStore(store_path)
        updated = store2.get(t.id)
        assert updated.state == TicketState.READY, f"Expected READY, got {updated.state}"
        
        # Verify assigned_ticket_id is cleared
        assert bot._assigned_ticket_id == ""
        store2.close()

    def test_error_exit_different_codes(self, tmp_path):
        """Various error exit codes should all transition to READY."""
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
            store.transition(t.id, TicketState.VALIDATING)
            store.transition(t.id, TicketState.TRIAGED)
            store.transition(t.id, TicketState.READY)
            store.transition(t.id, TicketState.IMPLEMENTING)
            store.flush()
            store.close()
            
            bot = self._make_bot(t.id)
            bots = {"test_bot": bot}
            
            import codebot.dispatch_service as ds
            with patch.object(ds, 'STATE_DIR', tmp_path):
                transition_ticket_on_error(bot, bots, exit_code=exit_code)
            
            store2 = TicketStore(store_path)
            updated = store2.get(t.id)
            assert updated.state == TicketState.READY, f"Exit code {exit_code}: expected READY, got {updated.state}"
            store2.close()

    def test_error_exit_no_assigned_ticket(self, tmp_path):
        """Error exit with no assigned ticket should be a no-op."""
        bot = self._make_bot()  # No ticket assigned
        bots = {"test_bot": bot}
        
        # Should not raise
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
        store.transition(t.id, TicketState.VALIDATING)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.READY)
        store.flush()
        store.close()
        
        bot = self._make_bot(t.id)
        bots = {"test_bot": bot}
        
        import codebot.dispatch_service as ds
        with patch.object(ds, 'STATE_DIR', tmp_path):
            transition_ticket_on_error(bot, bots, exit_code=1)
        
        store2 = TicketStore(store_path)
        updated = store2.get(t.id)
        assert updated.state == TicketState.READY
        store2.close()

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
        store.transition(t.id, TicketState.VALIDATING)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.READY)
        store.transition(t.id, TicketState.IMPLEMENTING)
        store.flush()
        store.close()
        
        # Create a claim file
        claims_dir = tmp_path / "claims"
        claims_dir.mkdir(exist_ok=True)
        claim_file = claims_dir / f"{t.id}.test_agent.json"
        claim_file.write_text('{"claim": "test"}')
        
        bot = self._make_bot(t.id)
        bots = {"test_bot": bot}
        
        import codebot.dispatch_service as ds
        with patch.object(ds, 'STATE_DIR', tmp_path):
            transition_ticket_on_error(bot, bots, exit_code=1)
        
        assert not claim_file.exists()


class TestTransitionTicketOnSuccess:
    """Test that clean exits transition tickets correctly."""

    def _make_bot(self, ticket_id: str = "", name: str = "implementer") -> BotState:
        config = BotConfig(name, "prompt.md", 600, 1200, "default")
        bot = BotState(config=config)
        if ticket_id:
            bot._assigned_ticket_id = ticket_id
        return bot

    def test_implementer_success_transitions_to_reviewing(self, tmp_path):
        """Implementer success should transition ticket to REVIEWING."""
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
        store.transition(t.id, TicketState.VALIDATING)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.READY)
        store.transition(t.id, TicketState.IMPLEMENTING)
        store.flush()
        store.close()
        
        bot = self._make_bot(t.id, name="general_implementer")
        bots = {"general_implementer": bot}
        
        import codebot.dispatch_service as ds
        with patch.object(ds, 'STATE_DIR', tmp_path):
            transition_ticket_on_success(bot, bots)
        
        store2 = TicketStore(store_path)
        updated = store2.get(t.id)
        assert updated.state == TicketState.REVIEWING
        store2.close()

    def test_reviewer_success_transitions_to_verifying(self, tmp_path):
        """Reviewer success should transition ticket to VERIFYING."""
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
        store.transition(t.id, TicketState.VALIDATING)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.READY)
        store.transition(t.id, TicketState.IMPLEMENTING)
        store.transition(t.id, TicketState.REVIEWING)
        store.flush()
        store.close()
        
        bot = self._make_bot(t.id, name="correctness_reviewer")
        bots = {"correctness_reviewer": bot}
        
        import codebot.dispatch_service as ds
        with patch.object(ds, 'STATE_DIR', tmp_path):
            transition_ticket_on_success(bot, bots)
        
        store2 = TicketStore(store_path)
        updated = store2.get(t.id)
        assert updated.state == TicketState.VERIFYING
        store2.close()
