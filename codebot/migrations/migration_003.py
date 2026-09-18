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
    columns = store.get_table_columns('agents')
    keep_cols = [c for c in columns if c != 'email_notifications']
    cols_csv = ', '.join(keep_cols)
    store.execute_sql(f"CREATE TABLE agents_backup AS SELECT {cols_csv} FROM agents")
    store.execute_sql("DROP TABLE agents")
    col_defs = ['id INTEGER PRIMARY KEY AUTOINCREMENT', 'name TEXT NOT NULL', "status TEXT DEFAULT 'active'", 'created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP']
    if 'company_id' in keep_cols:
        col_defs.append("company_id TEXT DEFAULT 'default'")
    store.execute_sql(f"CREATE TABLE agents ({', '.join(col_defs)})")
    store.execute_sql(f"INSERT INTO agents ({cols_csv}) SELECT {cols_csv} FROM agents_backup")
    store.execute_sql("DROP TABLE agents_backup")
