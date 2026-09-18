# scheduler_config

## Purpose
Centralizes all configurable parameters for the adaptive scheduler. Allows projects to tune concurrency, budgets, and watermarks without changing code.

## Key Components
- `SchedulerConfig`: Top-level frozen dataclass aggregating all sub-configs.
- Sub-configs: `BacklogConfig`, `DiscoveryConfig`, `ImplementationConfig`, `ReviewConfig`, `VerificationConfig`, `WorkerConfig`, `CostConfig`, `HysteresisConfig`, `PriorityAgingConfig`, `IntegrationConfig`.
- `load_scheduler_config()`: Loads config from `.codebot/scheduler.yaml` or `.json`, falling back to defaults.
- `_parse_simple_yaml()`: Minimal stdlib YAML parser to avoid PyYAML dependency.

## Dependencies
- None (stdlib-only).

## Invariants
- Frozen dataclasses ensure immutability during scheduler runs.
- All values validated at construction time.
- No third-party dependencies.