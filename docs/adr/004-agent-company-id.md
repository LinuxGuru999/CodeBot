# ADR 004: Add Company ID to Agent Records

## Status
Accepted

## Context
Agent records in the database were missing the `company_id` field required for multi-tenancy support in schema v3. Legacy agents from v2 did not have this field, causing failures in tenant-scoped queries.

## Decision
Add `company_id` field to all agent records. Legacy agents without a `company_id` are assigned the default value 'default-company'. Agents that already have a `company_id` are left unchanged.

Migration 004 (`migration_004_add_company_id.py`) implements this change with:
- Forward migration: adds `company_id` to agents missing it
- Rollback migration: sets `company_id` to None for all agents
- Backup creation and verification before applying changes
- Idempotent execution: running multiple times produces same result

## Consequences
- All agents now have a `company_id` for multi-tenancy support
- Backward compatible: legacy agents get default company ID
- Rollback supported via migration_004_add_company_id.py
- Data integrity preserved: other fields unchanged
- Backup verified before migration applies
