# completion_commit.py

Per-ticket scoped git commit at the COMPLETE lifecycle stage. Commits exactly
the files a completed ticket touched, with a `[CB-xxx]` message, and returns
the SHA for ticket traceability. Called AFTER the VERIFYING → COMPLETE
transition succeeds — never before, never at agent exit.

Supersedes the old `api_runner._auto_commit`, which ran at IMPLEMENTING exit
(unverified work), in a subprocess without adapter/paths (always fail-closed),
matched hardcoded Monitor repo names (always empty on CodeBot), and used
`git add -A` (would bundle concurrent tickets' work).

## Key Exports
- `commit_ticket_files()`: Function — stage + commit scoped files, return (ok, sha)
- `build_commit_message()`: Function — format `[ticket_id] title` message

## Invariants
- stdlib-only (subprocess, pathlib)
- Fail-open: any git error returns (False, "") and never raises — a commit
  failure must not undo a COMPLETE transition or crash the tick
- Scoped: only files listed in affected_modules are staged; never -A
- No push: push stays batched/periodic, commit is per-ticket synchronous
- "Nothing to commit" is success with current HEAD SHA (already in tree)

## Callers
- `gatekeeper._commit_completed_ticket()` — single-ticket path after transition
- `ticket_dispatcher.gatekeeper_verify_tickets()` Phase 4 — batch path after
  batch_transition; records SHA via `TicketStore.record_commit()`
