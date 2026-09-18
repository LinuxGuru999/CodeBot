"""Migration 003: Add email notification preferences to agents.

This migration adds an email_notifications JSON field to the agents table
to control which events trigger email notifications. Default preferences
enable all notification types.
"""

import json

DEFAULT_EMAIL_PREFS = json.dumps({
    'task_assigned': True,
    'task_completed': True,
    'system_alerts': True
})


def forward(store):
    """Add email_notifications column to agents table."""
    # Check if column already exists to ensure idempotency
    columns = store.get_table_columns('agents')
    if 'email_notifications' not in columns:
        store.execute_sql(
            f"ALTER TABLE agents ADD COLUMN email_notifications TEXT DEFAULT '{DEFAULT_EMAIL_PREFS}'"
        )
        
        # Update existing records that might have NULL values
        store.execute_sql(
            f"UPDATE agents SET email_notifications = '{DEFAULT_EMAIL_PREFS}' WHERE email_notifications IS NULL"
        )


def rollback(store):
    """Remove email_notifications column from agents table."""
    # SQLite doesn't support DROP COLUMN directly in older versions
    # We need to recreate the table without the column
    
    # Create backup of current data (excluding email_notifications)
    store.execute_sql("""
        CREATE TABLE agents_backup AS 
        SELECT id, name, status, created_at, company_id FROM agents
    """)
    
    # Drop the original table
    store.execute_sql("DROP TABLE agents")
    
    # Recreate the table without email_notifications
    # Check if company_id exists to determine schema
    backup_columns = store.get_table_columns('agents_backup')
    
    if 'company_id' in backup_columns:
        store.execute_sql("""
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                company_id TEXT DEFAULT 'default'
            )
        """)
    else:
        store.execute_sql("""
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    
    # Restore data from backup
    if 'company_id' in backup_columns:
        store.execute_sql("""
            INSERT INTO agents (id, name, status, created_at, company_id)
            SELECT id, name, status, created_at, company_id FROM agents_backup
        """)
    else:
        store.execute_sql("""
            INSERT INTO agents (id, name, status, created_at)
            SELECT id, name, status, created_at FROM agents_backup
        """)
    
    # Drop the backup table
    store.execute_sql("DROP TABLE agents_backup")
