# dependency_graph.py

Maintains a directed acyclic graph of ticket dependencies and produces a valid execution order via topological sort. Prevents out-of-order execution where a ticket is implemented before its prerequisites.

## Key Exports
- `CyclicDependencyError`: Class
- `DependencyGraph`: Class
- `add_ticket()`: Function
- `add_dependency()`: Function
- `remove_ticket()`: Function
- `get_dependencies()`: Function
- `get_dependents()`: Function

## Invariants
- stdlib-only (collections.deque)
- Graph must remain acyclic; add_edge raises ValueError on cycle detection
- Topological sort is deterministic (sorted by ticket_id within each level)
- Ticket IDs are validated format: CB-* prefix
