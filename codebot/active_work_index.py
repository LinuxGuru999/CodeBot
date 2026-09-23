#!/usr/bin/env python3
"""Active pipeline work index for front-running duplicate suppression.

Purpose
-------
Front-running mechanism per docs/ticket_redesign_mapping.md §Front-Running.
Keeps an in-memory + persisted dict fingerprint → {ticket_id, modules, state}
for tickets currently active in the pipeline. Checked before create_ticket()
to suppress findings already in flight. Persisted to
.codebot/state/active_work.json for crash recovery.

Why
---
With thousands of DISCOVERED tickets entering the pipeline, many overlap.
Front-running prevents duplicate work from consuming triage tokens: a new
finding whose fingerprint matches an active ticket is SUPERSEDED without
an LLM call.

Invariants
----------
- stdlib-only (json, pathlib, threading, time, os)
- Thread-safe via threading.RLock (same contract as TicketStore._lock)
- Atomic persistence via tmp + os.replace under flock
- Rebuild from TicketStore on startup for consistency
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from codebot.file_lock import LOCK_EX, LOCK_UN, flock

logger = logging.getLogger(__name__)

# States considered "active" for the front-running index.
# These tickets are in-flight and should suppress duplicate findings.
ACTIVE_STATES: frozenset[str] = frozenset({
    "GOAL",
    "DECOMP",
    "DECOMPOSE",
    "PLANNING",
    "IMPLEMENT",
    "IMPLEMENTING",
    "REVIEW",
    "REVIEWING",
    "REWORK",
})


class ActiveWorkIndex:
    """In-memory + persisted index of active pipeline tickets.

    Backed by ``{state_dir}/active_work.json`` with shape::

        {
          "fingerprints": {
            "abc123": {"ticket_id": "CB-xxx", "modules": ["codebot/x.py"], "state": "GOAL"},
          },
          "updated_at": 0.0
        }
    """

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = Path(state_dir)
        self._path = self._state_dir / "active_work.json"
        self._lock = threading.RLock()
        self._index: dict[str, dict[str, Any]] = {}
        self._load()

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            self._index = {}
            return
        try:
            lock_path = self._path.with_suffix(".lock")
            lock_path.touch(exist_ok=True)
            with open(lock_path, "a+") as lock_fd:
                flock(lock_fd, LOCK_EX)
                try:
                    raw = self._path.read_text(encoding="utf-8")
                    data = json.loads(raw) if raw.strip() else {}
                finally:
                    flock(lock_fd, LOCK_UN)
            fps = data.get("fingerprints", {})
            if isinstance(fps, dict):
                self._index = {
                    str(k): dict(v) for k, v in fps.items() if isinstance(v, dict)
                }
            else:
                self._index = {}
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning("ActiveWorkIndex load failed, starting empty: %s", exc)
            self._index = {}

    def _persist_locked(self) -> None:
        """Persist index atomically. Caller must hold self._lock."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "fingerprints": self._index,
            "updated_at": time.time(),
        }
        lock_path = self._path.with_suffix(".lock")
        lock_path.touch(exist_ok=True)
        try:
            with open(lock_path, "a+") as lock_fd:
                flock(lock_fd, LOCK_EX)
                try:
                    tmp = self._path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
                    os.replace(tmp, self._path)
                finally:
                    flock(lock_fd, LOCK_UN)
        except OSError as exc:
            logger.error("ActiveWorkIndex persist failed: %s", exc, exc_info=True)
            raise

    # -- public API ------------------------------------------------------

    def add(self, ticket_id: str, fingerprint: str, modules: list[str] | None = None, state: str = "GOAL") -> None:
        """Add a fingerprint entry.

        Args:
            ticket_id: Owning ticket id.
            fingerprint: Stable finding fingerprint (non-empty).
            modules: Affected modules list (may be empty).
            state: Current ticket state (default GOAL). Stored for debugging.
        """
        if not fingerprint or not fingerprint.strip():
            raise ValueError("fingerprint is required")
        if not ticket_id or not ticket_id.strip():
            raise ValueError("ticket_id is required")
        mods = list(modules) if modules is not None else []
        with self._lock:
            self._index[fingerprint.strip()] = {
                "ticket_id": ticket_id.strip(),
                "modules": mods,
                "state": state.strip().upper() if state else "GOAL",
            }
            self._persist_locked()

    def remove(self, ticket_id: str) -> int:
        """Remove all entries owned by *ticket_id*.

        Returns number of entries removed. Persists if any change.
        """
        if not ticket_id:
            return 0
        tid = ticket_id.strip()
        removed = 0
        with self._lock:
            to_delete = [fp for fp, entry in self._index.items() if entry.get("ticket_id") == tid]
            for fp in to_delete:
                self._index.pop(fp, None)
                removed += 1
            if removed:
                self._persist_locked()
        return removed

    def check(self, fingerprint: str) -> str | None:
        """Return active ticket_id for *fingerprint* or None."""
        if not fingerprint:
            return None
        with self._lock:
            entry = self._index.get(fingerprint.strip())
            if entry is None:
                return None
            tid = entry.get("ticket_id")
            return str(tid) if isinstance(tid, str) and tid else None

    def rebuild_from_store(self, store: Any) -> int:
        """Scan *store* for active tickets and rebuild the in-memory index.

        Active states: GOAL, DECOMP/DECOMPOSE, PLANNING, IMPLEMENT/IMPLEMENTING,
        REVIEW/REVIEWING, REWORK (per task spec; see ACTIVE_STATES).

        Called on startup to reconcile persisted index with ground truth in
        TicketStore. Persists the rebuilt index.

        Returns number of fingerprints indexed.
        """
        new_index: dict[str, dict[str, Any]] = {}
        try:
            tickets_iter = self._iter_store_tickets(store)
            for ticket in tickets_iter:
                # Extract state as string (handle Enum or str)
                state_val: str
                raw_state = getattr(ticket, "state", "")
                if hasattr(raw_state, "value"):
                    state_val = str(raw_state.value).upper()
                else:
                    state_val = str(raw_state).upper()
                if state_val not in ACTIVE_STATES:
                    continue
                # Extract fingerprint
                fp = getattr(ticket, "fingerprint", "") or ""
                if not isinstance(fp, str) or not fp.strip():
                    continue
                fp = fp.strip()
                tid = getattr(ticket, "id", "") or getattr(ticket, "ticket_id", "") or ""
                if not tid:
                    continue
                modules = getattr(ticket, "affected_modules", []) or []
                if not isinstance(modules, list):
                    modules = [str(modules)]
                new_index[fp] = {
                    "ticket_id": str(tid),
                    "modules": [str(m) for m in modules],
                    "state": state_val,
                }
        except Exception as exc:
            logger.warning("ActiveWorkIndex rebuild_from_store failed: %s", exc, exc_info=True)
            raise

        with self._lock:
            self._index = new_index
            self._persist_locked()
        return len(new_index)

    def _iter_store_tickets(self, store: Any) -> Any:
        """Yield Ticket objects from *store* via best-available access."""
        # Prefer internal dict access (fastest, always complete)
        tickets_dict = getattr(store, "_tickets", None)
        if isinstance(tickets_dict, dict):
            yield from tickets_dict.values()
            return
        # Fallback: public summary + list_by_state enumeration
        summary_fn = getattr(store, "summary", None)
        list_by_state_fn = getattr(store, "list_by_state", None)
        all_fn = getattr(store, "all", None) or getattr(store, "list_all", None) or getattr(store, "tickets", None)
        if callable(all_fn):
            try:
                result = all_fn()
                if isinstance(result, dict):
                    yield from result.values()
                elif isinstance(result, list):
                    yield from result
                return
            except Exception:
                pass
        if callable(summary_fn) and callable(list_by_state_fn):
            try:
                summary = summary_fn()
                states = list(summary.keys()) if isinstance(summary, dict) else []
                for s in states:
                    try:
                        # list_by_state may accept str or TicketState
                        batch = list_by_state_fn(s)  # type: ignore[call-arg]
                        if isinstance(batch, list):
                            yield from batch
                    except Exception:
                        continue
                return
            except Exception:
                pass
        # Last resort: getattr tickets attribute if it's list
        tickets_attr = getattr(store, "tickets", None)
        if isinstance(tickets_attr, list):
            yield from tickets_attr
        elif isinstance(tickets_attr, dict):
            yield from tickets_attr.values()

    # -- helpers --------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._index)

    def fingerprints(self) -> dict[str, dict[str, Any]]:
        """Return shallow copy of all fingerprints (for debugging)."""
        with self._lock:
            return {k: dict(v) for k, v in self._index.items()}
