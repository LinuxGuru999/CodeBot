"""Tests for §58 Data Layer Engineering — schema validation, soft deletes, backup/recovery.

Roadmap §58 exit criteria:
  §58.A — All data structures have primary keys, appropriate indexes, no N+1 queries
  §58.B — Invalid data cannot reach DB; multi-table ops atomic; deleted records preserved for audit
  §58.C — Restore from backup within RTO; backup integrity via checksum; backup restore tested
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.quality_gate import (
    GateEvaluation,
    GateStatus,
    QualityGatePolicy,
    load_policy,
    record_gate_results,
)


# ---------------------------------------------------------------------------
# §58.A — Schema Design: validated from_dict round-trip
# ---------------------------------------------------------------------------

class TestAgentRoleFromDict:
    """§58.A — AgentRole should reconstruct from its own to_dict without data loss."""

    def test_from_dict_roundtrip(self):
        from codebot.role_registry import AgentRole, get_role
        role = get_role("bug_hunter")
        assert role is not None
        d = role.to_dict()
        role2 = AgentRole.from_dict(d)
        assert role2.name == role.name
        assert role2.category == role.category
        assert role2.description == role.description
        assert role2.incentive == role.incentive
        assert role2.tool_policy.allowed_tools == role.tool_policy.allowed_tools
        assert role2.required_model.reasoning == role.required_model.reasoning

    def test_from_dict_all_roles_roundtrip(self):
        from codebot.role_registry import AgentRole, ALL_ROLES
        for role in ALL_ROLES:
            d = role.to_dict()
            r2 = AgentRole.from_dict(d)
            assert r2.name == role.name, f"Roundtrip failed for {role.name}"

    def test_from_dict_rejects_missing_name(self):
        with pytest.raises(ValueError, match="name"):
            AgentRole.from_dict({})

    def test_from_dict_rejects_invalid_category(self):
        from codebot.role_registry import AgentRole
        d = {"name": "x", "category": "INVALID", "description": "d",
             "required_model": {"reasoning": "low", "coding": "basic", "context": "small",
                                "cost_class": "cheap", "latency": "interactive"},
             "tool_policy": {"allowed_tools": [], "allowed_commands": [],
                             "filesystem_scope": "project_root"}}
        with pytest.raises(ValueError, match="category"):
            AgentRole.from_dict(d)


class TestModelProfileFromDict:
    """§58.A — ModelProfile round-trip."""

    def test_roundtrip(self):
        from codebot.role_registry import ModelProfile, ReasoningLevel, CodingLevel, ContextSize, CostClass, LatencyClass
        mp = ModelProfile(ReasoningLevel.HIGH, CodingLevel.ADVANCED, ContextSize.LARGE,
                          CostClass.PREMIUM, LatencyClass.BACKGROUND, security_review=True)
        d = mp.to_dict()
        mp2 = ModelProfile.from_dict(d)
        assert mp2.reasoning == mp.reasoning
        assert mp2.security_review is True

    def test_from_dict_rejects_invalid_reasoning(self):
        from codebot.role_registry import ModelProfile
        with pytest.raises(ValueError, match="reasoning"):
            ModelProfile.from_dict({"reasoning": "INVALID", "coding": "basic",
                                    "context": "small", "cost_class": "cheap",
                                    "latency": "interactive"})


class TestToolPolicyFromDict:
    """§58.A — ToolPolicy round-trip."""

    def test_roundtrip(self):
        from codebot.role_registry import ToolPolicy, STANDARD_TOOLS, STANDARD_COMMANDS
        tp = ToolPolicy(STANDARD_TOOLS, STANDARD_COMMANDS, "project_root")
        d = tp.to_dict()
        tp2 = ToolPolicy.from_dict(d)
        assert tp2.allowed_tools == tp.allowed_tools
        assert tp2.filesystem_scope == "project_root"


class TestRoleRegistryLookup:
    """§58.A — RoleRegistry should provide O(1) lookup by name and by category."""

    def test_lookup_by_name(self):
        from codebot.role_registry import ROLE_REGISTRY, get_role
        assert get_role("bug_hunter") is ROLE_REGISTRY["bug_hunter"]

    def test_lookup_by_category_returns_indexed(self):
        from codebot.role_registry import roles_by_category, RoleCategory, ROLE_REGISTRY
        discovery = roles_by_category(RoleCategory.DISCOVERY)
        for r in discovery:
            assert r.category == RoleCategory.DISCOVERY
            assert r.name in ROLE_REGISTRY


# ---------------------------------------------------------------------------
# §58.B — Data Validation and Integrity: soft deletes, atomic writes
# ---------------------------------------------------------------------------

class TestGateRecordIntegrity:
    """§58.B — Gate evaluation records should have integrity checksum."""

    def test_record_includes_checksum(self, tmp_path):
        evals = [GateEvaluation("build", GateStatus.PASS, "echo ok", "ok", 1, True)]
        record_gate_results(tmp_path, "CB-TEST-1", True, evals)
        data = json.loads((tmp_path / "gate_results.jsonl").read_text().strip().split("\n")[0])
        assert "checksum" in data, "Record must include integrity checksum"
        # Verify checksum matches content
        gate_data = json.dumps(data["gates"], sort_keys=True)
        expected = hashlib.sha256(gate_data.encode("utf-8")).hexdigest()
        assert data["checksum"] == expected

    def test_corrupt_record_detected(self, tmp_path):
        """§58.B — Tampered records should be detected via checksum."""
        evals = [GateEvaluation("test", GateStatus.PASS, "pytest", "ok", 1, True)]
        record_gate_results(tmp_path, "CB-TEST-2", True, evals)
        path = tmp_path / "gate_results.jsonl"
        lines = path.read_text().strip().split("\n")
        record = json.loads(lines[0])
        record["passed"] = False  # Tamper
        path.write_text(json.dumps(record) + "\n")
        # Verify checksum mismatch
        stored = record["checksum"]
        gate_data = json.dumps(record["gates"], sort_keys=True)
        computed = hashlib.sha256(gate_data.encode("utf-8")).hexdigest()
        assert stored != computed, "Tampered record should have mismatched checksum"


class TestSoftDelete:
    """§58.B — Soft delete preserves records for audit trail."""

    def test_soft_delete_preserves_record(self, tmp_path):
        from codebot.quality_gate import record_gate_results, read_gate_results
        evals = [GateEvaluation("lint", GateStatus.PASS, "flake8", "ok", 1, True)]
        record_gate_results(tmp_path, "CB-DEL-1", True, evals)
        # Soft delete
        from codebot.quality_gate import soft_delete_gate_record
        soft_delete_gate_record(tmp_path, "CB-DEL-1")
        # Record should still be readable
        results = read_gate_results(tmp_path, ticket_id="CB-DEL-1")
        assert len(results) >= 1
        assert results[0]["deleted"] is True
        assert "deleted_at" in results[0]

    def test_soft_deleted_excluded_by_default(self, tmp_path):
        from codebot.quality_gate import record_gate_results, read_gate_results, soft_delete_gate_record
        evals = [GateEvaluation("ok", GateStatus.PASS, "true", "", 1, True)]
        record_gate_results(tmp_path, "CB-DEL-2", True, evals)
        soft_delete_gate_record(tmp_path, "CB-DEL-2")
        # Should not appear in default read
        results = read_gate_results(tmp_path, ticket_id="CB-DEL-2", include_deleted=False)
        assert len(results) == 0

    def test_soft_deleted_included_when_asked(self, tmp_path):
        from codebot.quality_gate import record_gate_results, read_gate_results, soft_delete_gate_record
        evals = [GateEvaluation("ok", GateStatus.PASS, "true", "", 1, True)]
        record_gate_results(tmp_path, "CB-DEL-3", True, evals)
        soft_delete_gate_record(tmp_path, "CB-DEL-3")
        results = read_gate_results(tmp_path, ticket_id="CB-DEL-3", include_deleted=True)
        assert len(results) >= 1
        assert results[0]["deleted"] is True


class TestReadGateResults:
    """§58.B — Paginated read of gate results."""

    def test_read_all(self, tmp_path):
        from codebot.quality_gate import record_gate_results, read_gate_results
        for i in range(5):
            evals = [GateEvaluation("g", GateStatus.PASS, "echo", "", 1, True)]
            record_gate_results(tmp_path, f"CB-{i}", True, evals)
        results = read_gate_results(tmp_path)
        assert len(results) == 5

    def test_read_by_ticket_id(self, tmp_path):
        from codebot.quality_gate import record_gate_results, read_gate_results
        evals = [GateEvaluation("g", GateStatus.PASS, "echo", "", 1, True)]
        record_gate_results(tmp_path, "CB-FILTER", True, evals)
        record_gate_results(tmp_path, "CB-OTHER", True, evals)
        results = read_gate_results(tmp_path, ticket_id="CB-FILTER")
        assert all(r["ticket_id"] == "CB-FILTER" for r in results)

    def test_read_pagination(self, tmp_path):
        from codebot.quality_gate import record_gate_results, read_gate_results
        for i in range(10):
            evals = [GateEvaluation("g", GateStatus.PASS, "echo", "", 1, True)]
            record_gate_results(tmp_path, f"CB-PAG-{i}", True, evals)
        page1 = read_gate_results(tmp_path, limit=3, offset=0)
        page2 = read_gate_results(tmp_path, limit=3, offset=3)
        assert len(page1) == 3
        assert len(page2) == 3
        assert page1[0]["ticket_id"] != page2[0]["ticket_id"]

    def test_read_empty_file(self, tmp_path):
        from codebot.quality_gate import read_gate_results
        results = read_gate_results(tmp_path)
        assert results == []


# ---------------------------------------------------------------------------
# §58.C — Backup and Recovery
# ---------------------------------------------------------------------------

class TestBackupRecovery:
    """§58.C — Backup, verify integrity, and restore gate results."""

    def test_backup_creates_timestamped_copy(self, tmp_path):
        from codebot.quality_gate import record_gate_results, backup_gate_results
        evals = [GateEvaluation("g", GateStatus.PASS, "echo", "", 1, True)]
        record_gate_results(tmp_path, "CB-BKP-1", True, evals)
        backup_path = backup_gate_results(tmp_path)
        assert backup_path is not None
        assert backup_path.exists()
        assert backup_path.name.startswith("gate_results.")
        assert backup_path.name.endswith(".jsonl")
        # Content matches original
        original = (tmp_path / "gate_results.jsonl").read_text()
        backup = backup_path.read_text()
        assert original == backup

    def test_backup_checksum_verification(self, tmp_path):
        from codebot.quality_gate import record_gate_results, backup_gate_results, verify_backup_integrity
        evals = [GateEvaluation("g", GateStatus.PASS, "echo", "", 1, True)]
        record_gate_results(tmp_path, "CB-BKP-2", True, evals)
        backup_path = backup_gate_results(tmp_path)
        assert verify_backup_integrity(backup_path) is True

    def test_backup_detects_tampering(self, tmp_path):
        from codebot.quality_gate import record_gate_results, backup_gate_results, verify_backup_integrity
        evals = [GateEvaluation("g", GateStatus.PASS, "echo", "", 1, True)]
        record_gate_results(tmp_path, "CB-BKP-3", True, evals)
        backup_path = backup_gate_results(tmp_path)
        # Tamper with backup
        backup_path.write_text("TAMPERED")
        assert verify_backup_integrity(backup_path) is False

    def test_restore_from_backup(self, tmp_path):
        from codebot.quality_gate import (
            record_gate_results, backup_gate_results,
            restore_gate_results, read_gate_results,
        )
        evals = [GateEvaluation("g", GateStatus.PASS, "echo", "", 1, True)]
        record_gate_results(tmp_path, "CB-RST-1", True, evals)
        backup_path = backup_gate_results(tmp_path)
        # Overwrite original
        (tmp_path / "gate_results.jsonl").write_text("CORRUPTED")
        # Restore
        restore_gate_results(tmp_path, backup_path)
        results = read_gate_results(tmp_path, ticket_id="CB-RST-1")
        assert len(results) == 1
        assert results[0]["ticket_id"] == "CB-RST-1"

    def test_restore_rejects_tampered_backup(self, tmp_path):
        from codebot.quality_gate import backup_gate_results, restore_gate_results
        backup_path = tmp_path / "gate_results.bak.jsonl"
        backup_path.write_text("TAMPERED DATA")
        with pytest.raises(ValueError, match="integrity"):
            restore_gate_results(tmp_path, backup_path)


# ---------------------------------------------------------------------------
# §58.A — QualityGatePolicy validation
# ---------------------------------------------------------------------------

class TestPolicyValidation:
    """§58.A — Policy loading should validate structure."""

    def test_validate_policy_rejects_empty_required(self):
        with pytest.raises(ValueError, match="required"):
            QualityGatePolicy.from_dict({"required": [], "conditional": {}})

    def test_validate_policy_rejects_missing_command(self):
        with pytest.raises(ValueError, match="command"):
            QualityGatePolicy.from_dict({"required": [{"name": "build"}], "conditional": {}})

    def test_validate_policy_rejects_missing_name(self):
        with pytest.raises(ValueError, match="name"):
            QualityGatePolicy.from_dict({"required": [{"command": "echo ok"}], "conditional": {}})

    def test_validate_policy_accepts_valid(self):
        p = QualityGatePolicy.from_dict({
            "required": [{"name": "build", "command": "echo ok"}],
            "conditional": {},
        })
        assert len(p.required) == 1
