# freeze_detector.py

Freeze detection for CodeBot agents. Observes agent activity signals and
classifies liveness beyond simple heartbeat age (which `health_monitor`
already covers).

## Key Exports
- `AgentObservation`: Class
- `AgentState`: Class
- `FreezeDetector`: Class

## Invariants
- Detection only; restart decisions belong to the orchestrator
- Fail-open: inconclusive observations never declare a freeze
