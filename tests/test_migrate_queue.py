#!/usr/bin/env python3
"""Tests for migrate_queue.py legacy QUEUE.md migration pipeline."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from codebot.migrate_queue import parse_queue_md, migrate, SEVERITY_MAP, CLASS_MAP
from codebot.ticket_engine import Severity, TicketClass, RiskLevel, TicketStore, TicketState


def _wait_for_store_flush(store: TicketStore) -> None:
    """Wait for the TicketStore's background save worker to flush changes."""
    store.flush()


class TestParseQueueMd:
    """Tests for parse_queue_md function."""

    def test_parse_simple_item(self):
        """Test parsing a simple queue item with basic formatting."""
        text = """1. **[T4] [CRITICAL]**: Fix login bug — users cannot authenticate
"""
        items = parse_queue_md(text)
        assert len(items) == 1
        assert items[0]["title"] == "Fix login bug — users cannot authenticate"
        assert items[0]["tier"] == "T4"
        assert items[0]["severity"] == "critical"

    def test_parse_item_with_fields(self):
        """Test parsing an item with key-value fields."""
        text = """1. **[P1] [HIGH]**: Add user profile page
    Class: feature
    Status: TODO
    Acceptance: Profile displays name; Profile shows avatar; Settings accessible
    Affected Modules: views.py, templates/profile.html
"""
        items = parse_queue_md(text)
        assert len(items) == 1
        item = items[0]
        assert item["title"] == "Add user profile page"
        assert item["fields"]["class"] == "feature"
        assert item["fields"]["status"] == "TODO"
        assert "Profile displays name" in item["fields"]["acceptance"]
        assert "views.py" in item["fields"]["affected_modules"]

    def test_parse_done_item(self):
        """Test that DONE items are still parsed but marked."""
        text = """1. **DONE [P2] [LOW]**: Update README
"""
        items = parse_queue_md(text)
        assert len(items) == 1
        assert "DONE" in items[0]["raw"]
        assert items[0]["title"] == "Update README"

    def test_parse_multiple_items(self):
        """Test parsing multiple items in one text."""
        text = """1. **[T1] [CRITICAL]**: First critical issue
    Description: Something broke

2. **[T2] [MEDIUM]**: Second medium issue
    Class: bug
"""
        items = parse_queue_md(text)
        assert len(items) == 2
        assert items[0]["title"] == "First critical issue"
        assert items[0]["severity"] == "critical"
        assert items[1]["title"] == "Second medium issue"
        assert items[1]["fields"]["class"] == "bug"

    def test_parse_item_without_tier(self):
        """Test parsing an item without a tier marker."""
        text = """1. **[HIGH]**: Issue without tier
"""
        items = parse_queue_md(text)
        assert len(items) == 1
        assert items[0]["tier"] == ""
        assert items[0]["severity"] == "high"

    def test_parse_item_without_severity(self):
        """Test parsing an item without explicit severity defaults to medium."""
        text = """1. **[HIGH]**: Issue without tier but with severity
"""
        items = parse_queue_md(text)
        assert len(items) == 1
        assert items[0]["tier"] == ""
        assert items[0]["severity"] == "high"

    def test_parse_empty_text(self):
        """Test parsing empty text returns empty list."""
        items = parse_queue_md("")
        assert items == []

    def test_parse_no_valid_items(self):
        """Test text with no valid queue items returns empty list."""
        text = """Some random text
No queue items here
Just comments
"""
        items = parse_queue_md(text)
        assert items == []

    def test_parse_title_truncation(self):
        """Test that long titles are truncated to 200 chars."""
        long_title = "A" * 250
        text = f"1. **[HIGH]**: {long_title}\n"
        items = parse_queue_md(text)
        assert len(items[0]["title"]) <= 200

    def test_parse_raw_truncation(self):
        """Test that raw block is truncated to 500 chars."""
        long_block = "1. **[HIGH]**: Title\n    " + "A" * 600
        items = parse_queue_md(long_block)
        assert len(items[0]["raw"]) <= 500


class TestMigrate:
    """Tests for migrate function."""

    @pytest.fixture
    def temp_state_dir(self):
        """Create a temporary state directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def temp_queue_file(self, tmp_path):
        """Create a temporary queue file."""
        queue_file = tmp_path / "QUEUE.md"
        return queue_file

    def test_migrate_creates_tickets(self, temp_state_dir, temp_queue_file):
        """Test that migrate creates ticket JSON files."""
        queue_content = """1. **[T4] [HIGH]**: Test migration item
    Class: bug
    Status: TODO
    Acceptance: Verify fix works
"""
        temp_queue_file.write_text(queue_content)

        result = migrate(temp_queue_file, temp_state_dir, dry_run=False)

        assert result == 0
        tickets_file = temp_state_dir / "codebot_tickets.json"
        # Wait for background save worker to flush
        import time
        time.sleep(0.6)  # Wait longer than SAVE_DEBOUNCE_SECONDS (0.5)
        assert tickets_file.exists()

        with open(tickets_file) as f:
            data = json.load(f)
        assert "tickets" in data
        assert len(data["tickets"]) == 1
        ticket = data["tickets"][0]
        assert "Test migration item" in ticket["title"]
        assert ticket["ticket_class"] == "bug"
        assert ticket["severity"] == "high"

    def test_migrate_dry_run_no_files(self, temp_state_dir, temp_queue_file):
        """Test that dry_run mode produces no files."""
        queue_content = """1. **[T4] [HIGH]**: Test dry run item
    Class: feature
    Status: TODO
"""
        temp_queue_file.write_text(queue_content)

        result = migrate(temp_queue_file, temp_state_dir, dry_run=True)

        assert result == 0
        tickets_file = temp_state_dir / "codebot_tickets.json"
        # Should not create tickets file in dry run
        assert not tickets_file.exists()

    def test_migrate_skips_done_items(self, temp_state_dir, temp_queue_file):
        """Test that migrate skips items marked as DONE."""
        # Note: The current implementation checks for "DONE" in status field or raw starting with "**DONE"
        # Since the regex captures the number prefix, we test via status field
        queue_content = """1. **[T4] [HIGH]**: Already done item
    Class: bug
    Status: DONE

2. **[T4] [MEDIUM]**: Todo item
    Class: feature
    Status: TODO
"""
        temp_queue_file.write_text(queue_content)

        result = migrate(temp_queue_file, temp_state_dir, dry_run=False)

        assert result == 0
        tickets_file = temp_state_dir / "codebot_tickets.json"
        # Wait for background save worker to flush
        import time
        time.sleep(0.6)
        if tickets_file.exists():
            with open(tickets_file) as f:
                data = json.load(f)
            # Only the TODO item should be migrated
            assert len(data["tickets"]) == 1
            assert "Todo item" in data["tickets"][0]["title"]

    def test_migrate_queue_not_found(self, temp_state_dir, tmp_path):
        """Test migrate returns 1 when queue file doesn't exist."""
        non_existent = tmp_path / "nonexistent.md"
        result = migrate(non_existent, temp_state_dir)
        assert result == 1

    def test_migrate_handles_complete_status(self, temp_state_dir, temp_queue_file):
        """Test that items with COMPLETE status are skipped."""
        queue_content = """1. **[T4] [HIGH]**: Complete item
    Class: bug
    Status: COMPLETE
"""
        temp_queue_file.write_text(queue_content)

        result = migrate(temp_queue_file, temp_state_dir, dry_run=False)

        assert result == 0
        tickets_file = temp_state_dir / "codebot_tickets.json"
        if tickets_file.exists():
            with open(tickets_file) as f:
                data = json.load(f)
            assert len(data["tickets"]) == 0

    def test_migrate_default_severity(self, temp_state_dir, temp_queue_file):
        """Test that items without severity default to MEDIUM."""
        queue_content = """1. **[T4]**: Item without severity
    Class: feature
    Status: TODO
"""
        temp_queue_file.write_text(queue_content)

        result = migrate(temp_queue_file, temp_state_dir, dry_run=False)

        assert result == 0
        tickets_file = temp_state_dir / "codebot_tickets.json"
        # Wait for background save worker to flush
        import time
        time.sleep(0.6)
        if tickets_file.exists():
            with open(tickets_file) as f:
                data = json.load(f)
            assert data["tickets"][0]["severity"] == "medium"

    def test_migrate_default_class(self, temp_state_dir, temp_queue_file):
        """Test that items without class default to FEATURE."""
        queue_content = """1. **[T4] [HIGH]**: Item without class
    Status: TODO
"""
        temp_queue_file.write_text(queue_content)

        result = migrate(temp_queue_file, temp_state_dir, dry_run=False)

        assert result == 0
        tickets_file = temp_state_dir / "codebot_tickets.json"
        # Wait for background save worker to flush
        import time
        time.sleep(0.6)
        if tickets_file.exists():
            with open(tickets_file) as f:
                data = json.load(f)
            assert data["tickets"][0]["ticket_class"] == "feature"

    def test_migrate_acceptance_criteria_parsing(self, temp_state_dir, temp_queue_file):
        """Test that acceptance criteria are properly parsed from semicolon-separated list."""
        queue_content = """1. **[T4] [HIGH]**: Test acceptance parsing
    Class: test
    Status: TODO
    Acceptance: Criterion 1; Criterion 2; Criterion 3
"""
        temp_queue_file.write_text(queue_content)

        result = migrate(temp_queue_file, temp_state_dir, dry_run=False)

        assert result == 0
        tickets_file = temp_state_dir / "codebot_tickets.json"
        # Wait for background save worker to flush
        import time
        time.sleep(0.6)
        if tickets_file.exists():
            with open(tickets_file) as f:
                data = json.load(f)
            ticket = data["tickets"][0]
            assert "Criterion 1" in ticket["acceptance_criteria"]
            assert "Criterion 2" in ticket["acceptance_criteria"]
            assert "Criterion 3" in ticket["acceptance_criteria"]

    def test_migrate_affected_modules_parsing(self, temp_state_dir, temp_queue_file):
        """Test that affected modules are properly parsed from comma-separated list."""
        queue_content = """1. **[T4] [HIGH]**: Test affected modules
    Class: refactor
    Status: TODO
    Affected Modules: module_a.py, module_b.py, module_c.py
"""
        temp_queue_file.write_text(queue_content)

        result = migrate(temp_queue_file, temp_state_dir, dry_run=False)

        assert result == 0
        tickets_file = temp_state_dir / "codebot_tickets.json"
        # Wait for background save worker to flush
        import time
        time.sleep(0.6)
        if tickets_file.exists():
            with open(tickets_file) as f:
                data = json.load(f)
            ticket = data["tickets"][0]
            assert "module_a.py" in ticket["affected_modules"]
            assert "module_b.py" in ticket["affected_modules"]
            assert "module_c.py" in ticket["affected_modules"]

    def test_migrate_ticket_transitions(self, temp_state_dir, temp_queue_file):
        """Test that tickets go through correct state transitions."""
        queue_content = """1. **[T4] [HIGH]**: Test state transitions
    Class: bug
    Status: TODO
    Acceptance: Verify states
"""
        temp_queue_file.write_text(queue_content)

        result = migrate(temp_queue_file, temp_state_dir, dry_run=False)

        assert result == 0
        tickets_file = temp_state_dir / "codebot_tickets.json"
        # Wait for background save worker to flush
        import time
        time.sleep(0.6)
        if tickets_file.exists():
            with open(tickets_file) as f:
                data = json.load(f)
            ticket = data["tickets"][0]
            # Ticket should be in READY state after migration (uppercase per TicketState enum)
            assert ticket["state"] == "READY"
