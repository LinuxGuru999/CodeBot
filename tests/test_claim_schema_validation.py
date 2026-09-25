"""Test claim JSON schema validation to prevent type confusion attacks.

This test verifies that malformed claim files with invalid schema are properly
rejected and do not crash the orchestrator.
"""
import json
import tempfile
from pathlib import Path

import pytest

from codebot.ticket_dispatcher import _validate_claim_schema


class TestClaimSchemaValidation:
    """Test suite for claim JSON schema validation."""

    def test_valid_claim_with_worker_and_at(self, tmp_path):
        """Valid claim with worker (string) and at (float) should pass."""
        claim_path = tmp_path / "valid.json"
        data = {
            "worker": "implementer-1",
            "at": 1790348492.7040808,
            "ticket_id": "CB-123"
        }
        assert _validate_claim_schema(data, claim_path) is True

    def test_valid_claim_with_int_at(self, tmp_path):
        """Valid claim with at as int should pass (int is numeric)."""
        claim_path = tmp_path / "valid_int.json"
        data = {
            "worker": "implementer-1",
            "at": 1790348492,
            "ticket_id": "CB-123"
        }
        assert _validate_claim_schema(data, claim_path) is True

    def test_valid_claim_without_worker(self, tmp_path):
        """Claim without worker field should pass (field is optional)."""
        claim_path = tmp_path / "no_worker.json"
        data = {
            "at": 1790348492.7040808,
            "ticket_id": "CB-123"
        }
        assert _validate_claim_schema(data, claim_path) is True

    def test_valid_claim_without_at(self, tmp_path):
        """Claim without at field should pass (field is optional)."""
        claim_path = tmp_path / "no_at.json"
        data = {
            "worker": "implementer-1",
            "ticket_id": "CB-123"
        }
        assert _validate_claim_schema(data, claim_path) is True

    def test_invalid_worker_as_dict(self, tmp_path):
        """Claim with worker as dict should fail (type confusion attack)."""
        claim_path = tmp_path / "worker_dict.json"
        data = {
            "worker": {"name": "implementer-1"},
            "at": 1790348492.7040808
        }
        assert _validate_claim_schema(data, claim_path) is False

    def test_invalid_worker_as_list(self, tmp_path):
        """Claim with worker as list should fail."""
        claim_path = tmp_path / "worker_list.json"
        data = {
            "worker": ["implementer-1"],
            "at": 1790348492.7040808
        }
        assert _validate_claim_schema(data, claim_path) is False

    def test_invalid_worker_as_int(self, tmp_path):
        """Claim with worker as int should fail."""
        claim_path = tmp_path / "worker_int.json"
        data = {
            "worker": 123,
            "at": 1790348492.7040808
        }
        assert _validate_claim_schema(data, claim_path) is False

    def test_invalid_at_as_dict(self, tmp_path):
        """Claim with at as dict should fail (type confusion attack)."""
        claim_path = tmp_path / "at_dict.json"
        data = {
            "worker": "implementer-1",
            "at": {"timestamp": 1790348492.7040808}
        }
        assert _validate_claim_schema(data, claim_path) is False

    def test_invalid_at_as_string(self, tmp_path):
        """Claim with at as string should fail."""
        claim_path = tmp_path / "at_string.json"
        data = {
            "worker": "implementer-1",
            "at": "1790348492.7040808"
        }
        assert _validate_claim_schema(data, claim_path) is False

    def test_invalid_at_as_list(self, tmp_path):
        """Claim with at as list should fail."""
        claim_path = tmp_path / "at_list.json"
        data = {
            "worker": "implementer-1",
            "at": [1790348492.7040808]
        }
        assert _validate_claim_schema(data, claim_path) is False

    def test_invalid_root_not_dict(self, tmp_path):
        """Claim with non-dict root should fail."""
        claim_path = tmp_path / "root_list.json"
        data = ["worker", "implementer-1"]
        assert _validate_claim_schema(data, claim_path) is False

    def test_invalid_root_is_string(self, tmp_path):
        """Claim with string root should fail."""
        claim_path = tmp_path / "root_string.json"
        data = "not a dict"
        assert _validate_claim_schema(data, claim_path) is False

    def test_both_fields_invalid(self, tmp_path):
        """Claim with both fields invalid should fail."""
        claim_path = tmp_path / "both_invalid.json"
        data = {
            "worker": {"nested": "dict"},
            "at": [1, 2, 3]
        }
        assert _validate_claim_schema(data, claim_path) is False

    def test_empty_dict_is_valid(self, tmp_path):
        """Empty dict should pass (all fields optional)."""
        claim_path = tmp_path / "empty.json"
        data = {}
        assert _validate_claim_schema(data, claim_path) is True


class TestClaimSchemaIntegration:
    """Integration tests for claim schema validation in real workflows."""

    def test_reap_expired_claims_skips_malformed(self, tmp_path, monkeypatch):
        """Malformed claims should be skipped, not crash the reaper."""
        from codebot.ticket_dispatcher import _reap_expired_claims
        
        # Setup: create claims directory with malformed claim
        claims_dir = tmp_path / "claims"
        claims_dir.mkdir()
        
        # Valid claim (expired)
        valid_claim = claims_dir / "CB-123.implementer-1.json"
        valid_data = {
            "worker": "implementer-1",
            "at": 1000000000.0,  # Very old timestamp
            "ticket_id": "CB-123"
        }
        valid_claim.write_text(json.dumps(valid_data))
        
        # Malformed claim (worker is dict - type confusion attack)
        malformed_claim = claims_dir / "CB-456.implementer-1.json"
        malformed_data = {
            "worker": {"name": "implementer-1"},  # Invalid: dict instead of string
            "at": 1000000000.0,
            "ticket_id": "CB-456"
        }
        malformed_claim.write_text(json.dumps(malformed_data))
        
        # Mock STATE_DIR to use temp directory
        import codebot.ticket_dispatcher as td
        original_state_dir = td.STATE_DIR
        td.STATE_DIR = tmp_path
        
        try:
            # Should not crash and should reap valid expired claim
            reaped = _reap_expired_claims("implementer-1")
            
            # Valid claim should be reaped (deleted)
            assert not valid_claim.exists(), "Valid expired claim should be deleted"
            
            # Malformed claim should still exist (skipped, not processed)
            assert malformed_claim.exists(), "Malformed claim should be skipped, not deleted"
            
            # Only 1 claim should be reaped (the valid one)
            assert reaped == 1
        finally:
            td.STATE_DIR = original_state_dir

    def test_malformed_claim_does_not_crash_dispatcher(self, tmp_path, monkeypatch):
        """Malformed claims should not crash the dispatcher loop."""
        import codebot.dispatch_service as ds
        
        # Setup: create claims directory with malformed claim
        claims_dir = tmp_path / "claims"
        claims_dir.mkdir()
        
        # Malformed claim with nested dict for 'at' field
        malformed_claim = claims_dir / "CB-789.implementer-1.json"
        malformed_data = {
            "worker": "implementer-1",
            "at": {"nested": "dict"},  # Invalid: dict instead of float
            "ticket_id": "CB-789"
        }
        malformed_claim.write_text(json.dumps(malformed_data))
        
        # Mock STATE_DIR
        original_state_dir = ds.STATE_DIR
        ds.STATE_DIR = tmp_path
        
        try:
            # Create a mock bot
            class MockBot:
                def __init__(self):
                    self.config = type('obj', (object,), {'name': 'implementer-1'})()
                    self._assigned_ticket_id = ""
                    self._agent_id = "agent-1"
            
            bot = MockBot()
            bots = {"implementer-1": bot}
            
            # Should not crash when processing malformed claim
            # This would previously cause TypeError when accessing 'at' as float
            ds.transition_ticket_on_success(bot, bots)
            
            # Bot should still have no assigned ticket (malformed claim skipped)
            assert bot._assigned_ticket_id == ""
        finally:
            ds.STATE_DIR = original_state_dir


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
