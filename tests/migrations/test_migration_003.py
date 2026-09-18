"""Tests for Migration 003: Add email notification preferences to agents."""

import pytest
import sqlite3
import os
import json
from codebot.migrations import migration_003


@pytest.fixture
def temp_db(tmp_path):
    """Create a temporary SQLite database for testing."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    yield conn
    conn.close()
    if os.path.exists(str(db_path)):
        os.remove(str(db_path))


class MockStore:
    """Mock store for testing migrations."""
    def __init__(self, conn):
        self.conn = conn
    
    def execute_sql(self, sql):
        self.conn.execute(sql)
        self.conn.commit()
    
    def get_table_columns(self, table_name):
        cursor = self.conn.execute(f"PRAGMA table_info({table_name})")
        return [row[1] for row in cursor.fetchall()]


def test_forward_adds_email_notifications_column(temp_db):
    """Test that forward migration adds email_notifications column."""
    store = MockStore(temp_db)
    
    # First run migration 001 to create the agents table
    from codebot.migrations import migration_001
    migration_001.forward(store)
    
    # Now run migration 003
    migration_003.forward(store)
    
    # Check that email_notifications column exists
    columns = store.get_table_columns('agents')
    assert 'email_notifications' in columns


def test_forward_sets_default_json_value(temp_db):
    """Test that existing records get default email notification preferences."""
    store = MockStore(temp_db)
    
    # First run migration 001
    from codebot.migrations import migration_001
    migration_001.forward(store)
    
    # Insert some test data
    store.execute_sql("INSERT INTO agents (name, status) VALUES ('Agent1', 'active')")
    store.execute_sql("INSERT INTO agents (name, status) VALUES ('Agent2', 'inactive')")
    
    # Run migration 003
    migration_003.forward(store)
    
    # Check that existing records have default JSON value
    cursor = temp_db.execute("SELECT email_notifications FROM agents")
    rows = cursor.fetchall()
    
    assert len(rows) == 2
    for row in rows:
        prefs = json.loads(row[0])
        assert 'task_assigned' in prefs
        assert 'task_completed' in prefs
        assert 'system_alerts' in prefs
        assert isinstance(prefs['task_assigned'], bool)
        assert isinstance(prefs['task_completed'], bool)
        assert isinstance(prefs['system_alerts'], bool)


def test_rollback_removes_email_notifications(temp_db):
    """Test that rollback removes email_notifications column and preserves data."""
    store = MockStore(temp_db)
    
    # Run migration 001
    from codebot.migrations import migration_001
    migration_001.forward(store)
    
    # Insert test data
    store.execute_sql("INSERT INTO agents (name, status) VALUES ('Agent1', 'active')")
    store.execute_sql("INSERT INTO agents (name, status) VALUES ('Agent2', 'inactive')")
    
    # Run migration 003
    migration_003.forward(store)
    
    # Verify column exists
    columns = store.get_table_columns('agents')
    assert 'email_notifications' in columns
    
    # Rollback migration 003
    migration_003.rollback(store)
    
    # Check that email_notifications column is removed
    columns = store.get_table_columns('agents')
    assert 'email_notifications' not in columns
    
    # Check that original data is preserved
    cursor = temp_db.execute("SELECT name, status FROM agents")
    rows = cursor.fetchall()
    assert len(rows) == 2
    names = [row[0] for row in rows]
    assert 'Agent1' in names
    assert 'Agent2' in names


def test_forward_is_idempotent(temp_db):
    """Test that running forward twice doesn't cause errors."""
    store = MockStore(temp_db)
    
    # Run migration 001
    from codebot.migrations import migration_001
    migration_001.forward(store)
    
    # Run migration 003 twice
    migration_003.forward(store)
    migration_003.forward(store)  # Should not raise
    
    # Column should still exist
    columns = store.get_table_columns('agents')
    assert 'email_notifications' in columns


def test_default_preferences_structure(temp_db):
    """Test that default preferences have correct structure and types."""
    store = MockStore(temp_db)
    
    # Run migration 001
    from codebot.migrations import migration_001
    migration_001.forward(store)
    
    # Run migration 003
    migration_003.forward(store)
    
    # Insert a new agent after migration
    store.execute_sql("INSERT INTO agents (name, status) VALUES ('NewAgent', 'active')")
    
    # Check the default value
    cursor = temp_db.execute("SELECT email_notifications FROM agents WHERE name = 'NewAgent'")
    row = cursor.fetchone()
    
    prefs = json.loads(row[0])
    assert prefs == {
        'task_assigned': True,
        'task_completed': True,
        'system_alerts': True
    }
