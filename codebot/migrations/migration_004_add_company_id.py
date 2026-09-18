def forward(store):
    """Add company_id field to agent records missing it.
    
    Legacy agents from v2 schema do not have company_id.
    This migration assigns 'default-company' to agents missing the field.
    Agents that already have company_id are left unchanged.
    
    The migration is idempotent: running it multiple times produces the same result.
    A backup is created and verified before applying changes.
    """
    # Create and verify backup
    store.create_backup()
    if not store.verify_backup():
        raise RuntimeError("Backup verification failed. Aborting migration.")
    
    # Add company_id to agents missing it
    for agent in store.list_all_agents():
        if 'company_id' not in agent or agent['company_id'] is None:
            store.update_agent(agent['id'], {'company_id': 'default-company'})

def rollback(store):
    """Remove company_id field from agent records.
    
    This rollback sets company_id to None for all agents.
    Note: This is a soft rollback as we cannot determine which agents
    originally had no company_id vs those that were assigned default.
    """
    for agent in store.list_all_agents():
        if 'company_id' in agent:
            store.update_agent(agent['id'], {'company_id': None})
