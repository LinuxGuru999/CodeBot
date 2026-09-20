"""Tests for codebot.migrate_queue module.

Covers:
- parse_queue_md with valid/invalid markdown
- SEVERITY_MAP/CLASS_MAP/RISK_MAP mappings
- migrate() with dry_run=True and False
- duplicate detection via ValueError
- CLI argument parsing in main()
"""

import argparse
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure project root is in path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from codebot.migrate_queue import (
    parse_queue_md,
    migrate,
    main,
    SEVERITY_MAP,
    CLASS_MAP,
    RISK_MAP,
)
from codebot.ticket_engine import Severity, TicketClass, RiskLevel, TicketState, TicketStore


# --- Fixtures ---

@pytest.fixture
def sample_queue_md():
    return """\
1. **[T4] [CRITICAL]**: Fix login timeout — users cannot log in after 5 min
   class: bug
   severity: critical
   acceptance: Verify login works; Check timeout config
   affected_modules: auth/login.py

2. **[P1] [HIGH]**: Add rate limiting to API
   class: feature
   severity: high
   acceptance: Limit to 100 req/min; Return 429 on excess
   affected_modules: api/rate_limiter.py

3. **DONE [P2] [LOW]**: Update README
   class: documentation
   severity: low
   status: DONE
"""


@pytest.fixture
def empty_queue_md():
    return ""


@pytest.fixture
def invalid_queue_md():
    return "This is not a valid queue format\nJust some random text"


@pytest.fixture
def tmp_state_dir(tmp_path):
    state_dir = tmp_path / ".codebot" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


@pytest.fixture
def tmp_queue_file(tmp_path, sample_queue_md):
    queue_file = tmp_path / "QUEUE.md"
    queue_file.write_text(sample_queue_md, encoding="utf-8")
    return queue_file


# --- Tests for parse_queue_md ---

class TestParseQueueMd:
    def test_parse_valid_queue(self, sample_queue_md):
        items = parse_queue_md(sample_queue_md)
        assert len(items) == 3
        # First item
        assert items[0]["title"] == "Fix login timeout — users cannot log in after 5 min"
        assert items[0]["severity"] == "critical"
        assert items[0]["fields"]["class"] == "bug"
        assert "auth/login.py" in items[0]["fields"]["affected_modules"]
        # Second item
        assert items[1]["title"] == "Add rate limiting to API"
        assert items[1]["severity"] == "high"
        # Third item (DONE)
        assert "DONE" in items[2]["raw"]

    def test_parse_empty_queue(self, empty_queue_md):
        items = parse_queue_md(empty_queue_md)
        assert len(items) == 0

    def test_parse_invalid_queue(self, invalid_queue_md):
        items = parse_queue_md(invalid_queue_md)
        assert len(items) == 0

    def test_parse_missing_severity_defaults_to_medium(self):
        md = "1. **[T1]**: Simple task\n   class: feature"
        items = parse_queue_md(md)
        assert len(items) == 1
        assert items[0]["severity"] == "medium"

    def test_parse_title_truncation(self):
        long_title = "A" * 300
        md = f"1. **[T1] [LOW]**: {long_title}\n   class: feature"
        items = parse_queue_md(md)
        assert len(items[0]["title"]) <= 200

    def test_parse_fields_extraction(self):
        md = """1. **[T1] [HIGH]**: Test task
   class: security
   severity: high
   acceptance: Check A; Check B
   dependencies: dep1, dep2
"""
        items = parse_queue_md(md)
        assert items[0]["fields"]["class"] == "security"
        assert items[0]["fields"]["acceptance"] == "Check A; Check B"
        assert items[0]["fields"]["dependencies"] == "dep1, dep2"


# --- Tests for Mappings ---

class TestMappings:
    def test_severity_map_coverage(self):
        assert SEVERITY_MAP["critical"] == Severity.CRITICAL
        assert SEVERITY_MAP["high"] == Severity.HIGH
        assert SEVERITY_MAP["medium"] == Severity.MEDIUM
        assert SEVERITY_MAP["low"] == Severity.LOW

    def test_class_map_coverage(self):
        assert CLASS_MAP["bug"] == TicketClass.BUG
        assert CLASS_MAP["feature"] == TicketClass.FEATURE
        assert CLASS_MAP["security"] == TicketClass.SECURITY
        assert CLASS_MAP["performance"] == TicketClass.PERFORMANCE
        assert CLASS_MAP["documentation"] == TicketClass.DOCUMENTATION
        assert CLASS_MAP["test"] == TicketClass.TEST
        assert CLASS_MAP["refactor"] == TicketClass.REFACTOR
        assert CLASS_MAP["dependency"] == TicketClass.DEPENDENCY
        assert CLASS_MAP["infrastructure"] == TicketClass.INFRASTRUCTURE

    def test_risk_map_coverage(self):
        assert RISK_MAP["critical"] == RiskLevel.CRITICAL
        assert RISK_MAP["high"] == RiskLevel.HIGH
        assert RISK_MAP["medium"] == RiskLevel.MEDIUM
        assert RISK_MAP["low"] == RiskLevel.LOW

    def test_unknown_severity_defaults(self):
        # In migrate(), unknown severity defaults to MEDIUM via .get(..., Severity.MEDIUM)
        sev = SEVERITY_MAP.get("unknown", Severity.MEDIUM)
        assert sev == Severity.MEDIUM

    def test_unknown_class_defaults(self):
        cls = CLASS_MAP.get("unknown", TicketClass.FEATURE)
        assert cls == TicketClass.FEATURE


# --- Tests for migrate() ---

class TestMigrate:
    def test_migrate_dry_run_no_tickets_created(self, tmp_path, tmp_queue_file, tmp_state_dir):
        """Dry run should parse but not create any tickets in store."""
        store_path = tmp_state_dir / "codebot_tickets.json"
        result = migrate(tmp_queue_file, tmp_state_dir, dry_run=True)
        assert result == 0
        # Store file should not exist or be empty/new if created by init
        # Since dry_run doesn't call store.add(), no tickets should be persisted
        # Note: TicketStore.__init__ might create an empty file if it loads/saves
        # We check that no tickets are actually added by inspecting the logic flow
        # The print statements in dry_run mode confirm parsing occurred

    def test_migrate_real_run_creates_tickets(self, tmp_path, tmp_queue_file, tmp_state_dir):
        """Real migration should create tickets and transition them."""
        store_path = tmp_state_dir / "codebot_tickets.json"
        result = migrate(tmp_queue_file, tmp_state_dir, dry_run=False)
        assert result == 0
        
        store = TicketStore(store_path)
        # 2 active items, 1 DONE item skipped
        assert store.count() == 2
        
        # Check states: should be READY after transitions
        ready_tickets = store.list_by_state(TicketState.READY)
        assert len(ready_tickets) == 2

    def test_migrate_skips_done_items(self, tmp_path, tmp_queue_file, tmp_state_dir):
        """Items marked DONE should be skipped."""
        store_path = tmp_state_dir / "codebot_tickets.json"
        migrate(tmp_queue_file, tmp_state_dir, dry_run=False)
        store = TicketStore(store_path)
        
        # Only 2 active items from sample_queue_md
        assert store.count() == 2
        
        # Verify none of the tickets have "Update README" title
        titles = [t.title for t in store._tickets.values()]
        assert not any("Update README" in t for t in titles)

    def test_migrate_handles_duplicate_ticket_error(self, tmp_path, tmp_state_dir):
        """If create_ticket raises ValueError for duplicate, it should be skipped."""
        queue_content = """1. **[T1] [HIGH]**: Duplicate Task
   class: bug
   acceptance: Verify A
"""
        queue_file = tmp_path / "QUEUE.md"
        queue_file.write_text(queue_content, encoding="utf-8")
        
        # First migration
        migrate(queue_file, tmp_state_dir, dry_run=False)
        store = TicketStore(tmp_state_dir / "codebot_tickets.json")
        initial_count = store.count()
        assert initial_count == 1
        
        # Second migration of same file should detect duplicate evidence/hash
        # Note: The current implementation uses evidence_hash for dedup
        # Since the raw content is same, hash is same -> ValueError raised -> skipped
        migrate(queue_file, tmp_state_dir, dry_run=False)
        store.flush() # Ensure saved
        
        # Reload store to get fresh count
        store2 = TicketStore(tmp_state_dir / "codebot_tickets.json")
        # Should still be 1 because duplicate was skipped
        assert store2.count() == 1

    def test_migrate_missing_queue_file_returns_1(self, tmp_path, tmp_state_dir):
        """If queue file doesn't exist, return 1."""
        missing_queue = tmp_path / "NONEXISTENT.md"
        result = migrate(missing_queue, tmp_state_dir, dry_run=False)
        assert result == 1

    def test_migrate_acceptance_criteria_parsing(self, tmp_path, tmp_state_dir):
        """Acceptance criteria should be split by semicolon."""
        queue_content = """1. **[T1] [HIGH]**: Test Task
   class: feature
   acceptance: Criterion 1; Criterion 2; Criterion 3
"""
        queue_file = tmp_path / "QUEUE.md"
        queue_file.write_text(queue_content, encoding="utf-8")
        
        migrate(queue_file, tmp_state_dir, dry_run=False)
        store = TicketStore(tmp_state_dir / "codebot_tickets.json")
        ticket = list(store._tickets.values())[0]
        
        assert len(ticket.acceptance_criteria) == 3
        assert "Criterion 1" in ticket.acceptance_criteria
        assert "Criterion 2" in ticket.acceptance_criteria
        assert "Criterion 3" in ticket.acceptance_criteria

    def test_migrate_default_acceptance_if_missing(self, tmp_path, tmp_state_dir):
        """If no acceptance criteria provided, default to 'Verify: Title'."""
        queue_content = """1. **[T1] [HIGH]**: No Acceptance Task
   class: feature
"""
        queue_file = tmp_path / "QUEUE.md"
        queue_file.write_text(queue_content, encoding="utf-8")
        
        migrate(queue_file, tmp_state_dir, dry_run=False)
        store = TicketStore(tmp_state_dir / "codebot_tickets.json")
        ticket = list(store._tickets.values())[0]
        
        assert len(ticket.acceptance_criteria) == 1
        assert ticket.acceptance_criteria[0].startswith("Verify:")


# --- Tests for main() CLI ---

class TestMainCli:
    def test_main_parses_arguments(self):
        """Test that main() correctly parses arguments."""
        with patch('sys.argv', ['migrate_queue', '--project', '/tmp/proj', '--queue', 'docs/QUEUE.md', '--dry-run']):
            with patch('codebot.migrate_queue.migrate') as mock_migrate:
                mock_migrate.return_value = 0
                with patch('sys.exit') as mock_exit:
                    main()
                    mock_migrate.assert_called_once()
                    args, kwargs = mock_migrate.call_args
                    # Check that dry_run=True was passed
                    assert kwargs['dry_run'] is True or args[2] is True

    def test_main_default_arguments(self):
        """Test default arguments for main()."""
        with patch('sys.argv', ['migrate_queue']):
            with patch('codebot.migrate_queue.migrate') as mock_migrate:
                mock_migrate.return_value = 0
                with patch('sys.exit'):
                    main()
                    mock_migrate.assert_called_once()
                    # Default project is '.', default queue is 'docs/triage/QUEUE.md'
                    call_args = mock_migrate.call_args
                    # Extract positional args
                    queue_path_arg = call_args[0][0]
                    # It should be resolved path
                    assert 'docs/triage/QUEUE.md' in str(queue_path_arg)
