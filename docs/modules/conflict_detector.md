# conflict_detector.py

Detects when two tickets would modify overlapping files or modules, preventing the scheduler from assigning them to concurrent workers. Tracks worktree isolation so each implementation runs in its own branch (§22).

## Key Exports
- `TaskOverlap`: Class
- `ConflictEdge`: Class
- `ConflictMatrix`: Class
- `WorktreeRegistry`: Class
- `detect_module_conflicts()`: Function
- `detect_file_conflicts()`: Function
- `build_conflict_matrix()`: Function
- `filter_non_conflicting()`: Function
- `conflicts_with()`: Function

## Invariants
- stdlib-only (dataclasses)
- Pure functions: no I/O, no subprocess, no git operations
- Conflict is symmetric: if A conflicts with B, B conflicts with A
- Module-level overlap is a heuristic; exact file overlap is authoritative
- Worktree assignments are tracked but not created here
