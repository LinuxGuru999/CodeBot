# coverage_bridge.py

Consumes a CoverageReport and generates prioritized TicketStore entries for modules below the coverage threshold. Each ticket targets a specific module's uncovered lines, assigned to the test_implementer role.

## Key Exports
- `generate_coverage_tickets()`: Function
- `coverage_delta_score()`: Function

## Invariants
- stdlib-only
- Never creates duplicate tickets (dedup via evidence hash)
- Tickets reference specific uncovered line ranges, not entire modules
- Priority scales inversely with coverage (lower coverage = higher priority)
- Respects constitution-protected paths (never creates tickets for protected files)
