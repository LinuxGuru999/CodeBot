# telemetry

## Purpose
Provides an HTTP ingestion endpoint for production telemetry signals (errors, performance degradation, etc.). Creates ticket candidates in `DISCOVERED` state for human triage.

## Key Components
- `TelemetryHandler`: HTTP request handler accepting POST `/telemetry` with Bearer token auth.
- `_validate_signal()`: Validates incoming signal structure against allowed types and severity.
- `_create_ticket_from_signal()`: Maps telemetry signals to `TicketClass` and creates tickets via `ticket_engine`.

## Dependencies
- `ticket_engine`: For ticket creation.
- `os`: For environment variable access (token, state dir).

## Invariants
- Stdlib-only (`http.server`).
- Bounded I/O (max 64KB request body).
- Requires Bearer token authentication.
- Tickets created in `DISCOVERED` state proceeding through normal autonomous pipeline.