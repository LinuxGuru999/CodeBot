# manifest_schema.py

Provides strict validation for agent manifests (.codebot/manifests/<name>.json) and loading helpers used by the orchestrator and batch scheduler. Consumers call validate_manifest, load_manifest, or load_all_manifests.

## Key Exports
- `validate_manifest()`: Function
- `load_manifest()`: Function
- `load_all_manifests()`: Function

## Invariants
- stdlib json only, no yaml
- validate_manifest returns (ok, errors) without raising for schema violations
- load_manifest raises ValueError with path + field on invalid manifests
- load_all_manifests raises ValueError on first invalid manifest
