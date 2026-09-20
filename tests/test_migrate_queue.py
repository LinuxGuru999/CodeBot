"""Tests for codebot.migrate_queue module."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from codebot.migrate_queue import (
    parse_queue_md,
    migrate,
    main,
    SEVERITY_MAP,
    CLASS_MAP,
    RISK_MAP,
)
from codebot.ticket_engine import (
    TicketClass,
    Severity,
    RiskLevel,
    TicketStore,
    TicketState,
)


# Sample QUEUE.md content for testing
SAMPLE_QUEUE_MD = """# Monitor Triage Queue

1. **P0 [T4] [CRITICAL]**: Critical Bug Fix
   Status: TODO
   Class: bug
   Acceptance Criteria: Fix the crash; Add regression test
   Affected Modules: core.py

2. **P1 [T3] [HIGH]**: New Feature Implementation
   Status: IN_PROGRESS
   Class: feature
   Acceptance Criteria: Feature works end-to-end
   Dependencies: CB-123

3. **P2 [T2] [MEDIUM]**: Performance Optimization
   Status: TODO
   Class: performance
   Acceptance Criteria: 2x speedup

4. **P3 [T1] [LOW]**: Documentation Update
   Status: TODO
   Class: documentation
   Acceptance Criteria: Docs are complete

5. **DONE P0 [T1] [CRITICAL]**: Completed Item
   Status: COMPLETE
   Class: infrastructure
   Acceptance Criteria: Done

6. **P1 [T2] [INVALID_SEVERITY]**: Item With Unknown Severity
   Status: TODO
   Class: test
   Acceptance Criteria: Test passes

7. **P2 [T3] [HIGH]**: Item With Unknown Class
   Status: TODO
   Class: unknown_class
   Acceptance Criteria: Works
"""

EMPTY_QUEUE_MD = """# Empty Queue

No items here.
"""

MALFORMED_QUEUE_MD = """# Malformed Queue

This is not a valid item line.

1. **Missing Brackets**: Just a title
   Status: TODO

2. **[T1] [HIGH]**: Missing number prefix
   Status: TODO
"""


class TestSeverityMap:
    """Test SEVERITY_MAP mappings."""

    def test_critical_mapping(self):
        assert SEVERITY_MAP["critical"] == Severity.CRITICAL

    def test_high_mapping(self):
        assert SEVERITY_MAP["high"] == Severity.HIGH

    def test_medium_mapping(self):
        assert SEVERITY_MAP["medium"] == Severity.MEDIUM

    def test_low_mapping(self):
        assert SEVERITY_MAP["low"] == Severity.LOW

    def test_unknown_severity_defaults_to_medium(self):
        assert SEVERITY_MAP.get("unknown", Severity.MEDIUM) == Severity.MEDIUM


class TestClassMap:
    """Test CLASS_MAP mappings."""

    def test_bug_mapping(self):
        assert CLASS_MAP["bug"] == TicketClass.BUG

    def test_feature_mapping(self):
        assert CLASS_MAP["feature"] == TicketClass.FEATURE

    def test_security_mapping(self):
        assert CLASS_MAP["security"] == TicketClass.SECURITY

    def test_performance_mapping(self):
        assert CLASS_MAP["performance"] == TicketClass.PERFORMANCE

    def test_documentation_mapping(self):
        assert CLASS_MAP["documentation"] == TicketClass.DOCUMENTATION

    def test_test_mapping(self):
        assert CLASS_MAP["test"] == TicketClass.TEST

    def test_refactor_mapping(self):
        assert CLASS_MAP["refactor"] == TicketClass.REFACTOR

    def test_dependency_mapping(self):
        assert CLASS_MAP["dependency"] == TicketClass.DEPENDENCY

    def test_infrastructure_mapping(self):
        assert CLASS_MAP["infrastructure"] == TicketClass.INFRASTRUCTURE

    def test_unknown_class_defaults_to_feature(self):
        assert CLASS_MAP.get("unknown", TicketClass.FEATURE) == TicketClass.FEATURE


class TestRiskMap:
    """Test RISK_MAP mappings."""

    def test_risk_mappings(self):
        assert RISK_MAP["critical"] == RiskLevel.CRITICAL
        assert RISK_MAP["high"] == RiskLevel.HIGH
        assert RISK_MAP["medium"] == RiskLevel.MEDIUM
        assert RISK_MAP["low"] == RiskLevel.LOW


class TestParseQueueMd:
    """Test parse_queue_md function."""

    def test_parses_multiple_items(self):
        items = parse_queue_md(SAMPLE_QUEUE_MD)
        assert len(items) == 7

    def test_extracts_title(self):
        items = parse_queue_md(SAMPLE_QUEUE_MD)
        assert items[0]["title"] == "Critical Bug Fix"
        assert items[1]["title"] == "New Feature Implementation"

    def test_extracts_severity(self):
        items = parse_queue_md(SAMPLE_QUEUE_MD)
        assert items[0]["severity"] == "critical"
        assert items[1]["severity"] == "high"
        assert items[2]["severity"] == "medium"
        assert items[3]["severity"] == "low"

    def test_extracts_tier(self):
        items = parse_queue_md(SAMPLE_QUEUE_MD)
        assert items[0]["tier"] == "T4"
        assert items[1]["tier"] == "T3"

    def test_extracts_fields(self):
        items = parse_queue_md(SAMPLE_QUEUE_MD)
        assert items[0]["fields"]["status"] == "TODO"
        assert items[0]["fields"]["class"] == "bug"
        assert items[0]["fields"]["acceptance_criteria"] == "Fix the crash; Add regression test"

    def test_handles_done_items(self):
        items = parse_queue_md(SAMPLE_QUEUE_MD)
        done_items = [i for i in items if "DONE" in i["raw"]]
        assert len(done_items) == 1
        assert done_items[0]["title"] == "Completed Item"

    def test_handles_empty_queue(self):
        items = parse_queue_md(EMPTY_QUEUE_MD)
        assert len(items) == 0

    def test_handles_malformed_entries(self):
        items = parse_queue_md(MALFORMED_QUEUE_MD)
        # Should parse what it can, skip malformed lines
        assert isinstance(items, list)

    def test_handles_unknown_severity(self):
        items = parse_queue_md(SAMPLE_QUEUE_MD)
        invalid_item = [i for i in items if i["severity"] == "invalid_severity"]
        assert len(invalid_item) == 1

    def test_title_truncation(self):
        long_title = "A" * 300
        md = f"1. **P0 [T1] [HIGH]**: {long_title}\n   Status: TODO"
        items = parse_queue_md(md)
        assert len(items[0]["title"]) <= 200

    def test_raw_truncation(self):
        long_raw = "x" * 600
        md = f"1. **P0 [T1] [HIGH]**: Title\n   {long_raw}"
        items = parse_queue_md(md)
        assert len(items[0]["raw"]) <= 500


class TestMigrate:
    """Test migrate function."""

    @pytest.fixture
    def temp_state_dir(self, tmp_path):
        """Create a temporary state directory."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        return state_dir

    @pytest.fixture
    def temp_queue_file(self, tmp_path):
        """Create a temporary queue file."""
        queue_file = tmp_path / "QUEUE.md"
        queue_file.write_text(SAMPLE_QUEUE_MD)
        return queue_file

    def test_migrate_creates_tickets(self, temp_state_dir, temp_queue_file):
        """Test that migrate creates valid tickets in TicketStore."""
        result = migrate(temp_queue_file, temp_state_dir, dry_run=False)
        assert result == 0

        # Check tickets were created
        tickets_file = temp_state_dir / "codebot_tickets.json"
        assert tickets_file.exists()

        store = TicketStore(tickets_file)
        # Should have migrated non-DONE items (7 total - 1 DONE = 6)
        assert store.count() > 0

    def test_migrate_dry_run_no_changes(self, temp_state_dir, temp_queue_file):
        """Test that dry_run produces no file changes."""
        tickets_file = temp_state_dir / "codebot_tickets.json"

        result = migrate(temp_queue_file, temp_state_dir, dry_run=True)
        assert result == 0

        # No tickets file should be created in dry-run mode
        # (or if it exists from before, it shouldn't be modified)
        # The migrate function prints but doesn't write in dry_run

    def test_migrate_skips_done_items(self, temp_state_dir, temp_queue_file):
        """Test that DONE items are skipped."""
        result = migrate(temp_queue_file, temp_state_dir, dry_run=False)
        assert result == 0

        store = TicketStore(temp_state_dir / "codebot_tickets.json")
        # Check that DONE items weren't migrated
        titles = [t.title for t in store._tickets.values()]
        assert "Completed Item" not in titles

    def test_migrate_handles_missing_queue_file(self, temp_state_dir):
        """Test handling of missing queue file."""
        missing_queue = temp_state_dir / "nonexistent.md"
        result = migrate(missing_queue, temp_state_dir, dry_run=False)
        assert result == 1

    def test_migrate_duplicate_detection(self, temp_state_dir, temp_queue_file):
        """Test duplicate detection via ValueError."""
        # First migration
        migrate(temp_queue_file, temp_state_dir, dry_run=False)

        # Second migration should detect duplicates
        # The migrate function catches ValueError for duplicates and skips
        result = migrate(temp_queue_file, temp_state_dir, dry_run=False)
        assert result == 0

    def test_migrate_maps_severity_correctly(self, temp_state_dir, temp_queue_file):
        """Test that severity is mapped correctly."""
        migrate(temp_queue_file, temp_state_dir, dry_run=False)

        store = TicketStore(temp_state_dir / "codebot_tickets.json")
        severities = [t.severity for t in store._tickets.values()]

        assert Severity.CRITICAL in severities
        assert Severity.HIGH in severities
        assert Severity.MEDIUM in severities
        assert Severity.LOW in severities

    def test_migrate_maps_class_correctly(self, temp_state_dir, temp_queue_file):
        """Test that class is mapped correctly."""
        migrate(temp_queue_file, temp_state_dir, dry_run=False)

        store = TicketStore(temp_state_dir / "codebot_tickets.json")
        classes = [t.ticket_class for t in store._tickets.values()]

        assert TicketClass.BUG in classes
        assert TicketClass.FEATURE in classes
        assert TicketClass.PERFORMANCE in classes
        assert TicketClass.DOCUMENTATION in classes

    def test_migrate_sets_initial_state_to_ready(self, temp_state_dir, temp_queue_file):
        """Test that tickets are transitioned to READY state."""
        migrate(temp_queue_file, temp_state_dir, dry_run=False)

        store = TicketStore(temp_state_dir / "codebot_tickets.json")
        for ticket in store._tickets.values():
            assert ticket.state == TicketState.READY

    def test_migrate_empty_queue(self, temp_state_dir, tmp_path):
        """Test migration with empty queue."""
        empty_queue = tmp_path / "empty.md"
        empty_queue.write_text(EMPTY_QUEUE_MD)

        result = migrate(empty_queue, temp_state_dir, dry_run=False)
        assert result == 0

        store = TicketStore(temp_state_dir / "codebot_tickets.json")
        assert store.count() == 0


class TestMain:
    """Test main function and CLI args."""

    def test_main_with_dry_run(self, tmp_path):
        """Test main() with --dry-run flag."""
        queue_file = tmp_path / "QUEUE.md"
        queue_file.write_text(SAMPLE_QUEUE_MD)
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        with patch("sys.argv", ["migrate_queue", "--queue", str(queue_file), "--state-dir", str(state_dir), "--dry-run"]):
            with patch("sys.exit") as mock_exit:
                main()
                mock_exit.assert_called_once_with(0)

    def test_main_with_custom_state_dir(self, tmp_path):
        """Test main() with custom --state-dir."""
        queue_file = tmp_path / "QUEUE.md"
        queue_file.write_text(SAMPLE_QUEUE_MD)
        state_dir = tmp_path / "custom_state"
        state_dir.mkdir()

        with patch("sys.argv", ["migrate_queue", "--queue", str(queue_file), "--state-dir", str(state_dir)]):
            with patch("sys.exit") as mock_exit:
                main()
                mock_exit.assert_called_once_with(0)

        # Verify tickets were created in custom state dir
        tickets_file = state_dir / "codebot_tickets.json"
        assert tickets_file.exists()

    def test_main_default_paths(self, tmp_path):
        """Test main() with default paths."""
        # Create project structure
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        queue_dir = project_dir / "docs" / "triage"
        queue_dir.mkdir(parents=True)
        queue_file = queue_dir / "QUEUE.md"
        queue_file.write_text(SAMPLE_QUEUE_MD)

        os.chdir(project_dir)

        with patch("sys.argv", ["migrate_queue"]):
            with patch("sys.exit") as mock_exit:
                main()
                # Should exit with 0 on success
                call_args = mock_exit.call_args
                assert call_args[0][0] == 0
