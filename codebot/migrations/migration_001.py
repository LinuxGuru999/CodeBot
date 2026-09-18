"""Migration 001: Create base agents table."""


def forward(store):
    store.execute_sql("""
        CREATE TABLE IF NOT EXISTS agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def rollback(store):
    store.execute_sql("DROP TABLE IF EXISTS agents")
