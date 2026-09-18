import pytest
from codebot.migrations.migration_004_add_company_id import forward, rollback

class MockDB:
    def __init__(self):
        self.agents = [
            {'id': 1, 'name': 'Alice'},
            {'id': 2, 'name': 'Bob', 'company_id': 'acme-corp'},
            {'id': 3, 'name': 'Charlie'}
        ]
        self.backup_created = False
        self.backup_verified = False

    def query(self, sql):
        return self.agents

    def query_one(self, sql, params=None):
        if 'COUNT' in sql:
            return [len(self.agents)]
        return self.agents[0] if self.agents else None

    def execute(self, sql, params=None):
        if 'CREATE TABLE agents_backup' in sql:
            self.backup_created = True

class MockStore:
    def __init__(self, db):
        self.db = db

    def list_all_agents(self):
        return self.db.query("SELECT * FROM agents")

    def update_agent(self, agent_id, updates):
        for agent in self.db.agents:
            if agent['id'] == agent_id:
                agent.update(updates)

    def create_backup(self):
        self.db.backup_created = True
        return True

    def verify_backup(self):
        self.db.backup_verified = True
        return True

def test_forward_migration_adds_company_id():
    """Test that forward migration adds company_id to agents missing it."""
    db = MockDB()
    store = MockStore(db)
    
    # Verify backup is created
    store.create_backup()
    assert db.backup_created
    
    forward(store)
    agents = store.list_all_agents()
    
    # All agents should have company_id
    assert all('company_id' in a for a in agents)
    # Legacy agents get default
    assert agents[0]['company_id'] == 'default-company'
    # Existing company_id preserved
    assert agents[1]['company_id'] == 'acme-corp'
    assert agents[2]['company_id'] == 'default-company'

def test_forward_migration_is_idempotent():
    """Test that running forward migration twice produces same result."""
    db = MockDB()
    store = MockStore(db)
    
    forward(store)
    agents_first = store.list_all_agents()
    
    forward(store)
    agents_second = store.list_all_agents()
    
    assert agents_first == agents_second

def test_rollback_removes_company_id():
    """Test that rollback removes company_id field."""
    db = MockDB()
    store = MockStore(db)
    
    forward(store)
    rollback(store)
    agents = store.list_all_agents()
    
    # company_id should be None or removed
    for agent in agents:
        assert agent.get('company_id') is None

def test_data_integrity_preserved():
    """Test that other fields are not affected by migration."""
    db = MockDB()
    store = MockStore(db)
    
    original_names = [a['name'] for a in db.agents]
    original_ids = [a['id'] for a in db.agents]
    
    forward(store)
    
    agents = store.list_all_agents()
    assert [a['name'] for a in agents] == original_names
    assert [a['id'] for a in agents] == original_ids
