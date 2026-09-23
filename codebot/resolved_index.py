#!/usr/bin/env python3
"""Resolved fingerprint index for RESOLVED/SUPERSEDED/CANCELLED tickets.

Purpose
-------
Back-feeding mechanism per docs/ticket_redesign_mapping.md §Back-Feeding.
Provides O(1) fingerprint lookup to suppress findings that have already
been resolved, superseded, or cancelled. Persisted atomically to
.codebot/state/resolved_index.json with crash-safe tmp→replace.

Why
---
Without back-feeding, discovery agents rediscover fixed issues and waste
triage tokens. The index flows resolution knowledge backward into the
discovery pipeline: before create_ticket() is called the fingerprint is
checked here. Matches suppress or annotate the finding without an LLM call.

Invariants
----------
- stdlib-only (json, pathlib, threading, time, os)
- Thread-safe via threading.RLock (same contract as TicketStore._lock)
- Atomic persistence via tmp + os.replace under flock
- Size cap 10000 entries; oldest evicted first
- TTL-based compaction removes stale entries (default 30 days)
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

DEFAULT_TTL_SECONDS: float = 30 * 24 * 3600  # 30 days
MAX_ENTRIES: int = 10000


class ResolvedIndex:
    """Fingerprint index for RESOLVED / SUPERSEDED / CANCELLED tickets.

    Backed by ``{state_dir}/resolved_index.json`` with shape::

        {
          "fingerprints": {
            "abc123": {"ticket_id": "CB-xxx", "state": "RESOLVED", "at": 0.0, ...},
          },
          "updated_at": 0.0
        }
    """

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = Path(state_dir)
        self._path = self._state_dir / "resolved_index.json"
        self._lock = threading.RLock()
        self._fingerprints: dict[str, dict[str, Any]] = {}
        self._load()

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            self._fingerprints = {}
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
                # Keep only dict values keyed by string fingerprint
                self._fingerprints = {
                    str(k): dict(v) for k, v in fps.items() if isinstance(v, dict)
                }
            else:
                self._fingerprints = {}
            # Enforce size cap on load (evict oldest if oversized file)
            if len(self._fingerprints) > MAX_ENTRIES:
                self._evict_oldest_locked(len(self._fingerprints) - MAX_ENTRIES)
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning("ResolvedIndex load failed, starting empty: %s", exc)
            self._fingerprints = {}

    def _persist_locked(self) -> None:
        """Persist fingerprints atomically. Caller must hold self._lock."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "fingerprints": self._fingerprints,
            "updated_at": time.time(),
        }
        lock_path = self._path.with_suffix(".lock")
        lock_path.touch(exist_ok=True)
        # Use flock for cross-process safety, then tmp→replace for atomicity.
        try:
            with open(lock_path, "a+") as lock_fd:
                flock(lock_fd, LOCK_EX)
                try:
                    tmp = self._path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
                    # os.replace is atomic on POSIX
                    os.replace(tmp, self._path)
                finally:
                    flock(lock_fd, LOCK_UN)
        except OSError as exc:
            logger.error("ResolvedIndex persist failed: %s", exc, exc_info=True)
            raise

    def _evict_oldest_locked(self, count: int) -> None:
        """Evict *count* oldest entries by timestamp. Caller must hold lock."""
        if count <= 0:
            return

        def _ts(item: tuple[str, dict[str, Any]]) -> float:
            v = item[1]
            # Prefer canonical "at", fallback to "resolved_at" / "updated_at"
            for key in ("at", "resolved_at", "updated_at"):
                val = v.get(key)
                if isinstance(val, (int, float)):
                    return float(val)
            return 0.0

        sorted_keys = [k for k, _ in sorted(self._fingerprints.items(), key=_ts)]
        for k in sorted_keys[:count]:
            self._fingerprints.pop(k, None)

    # -- public API ------------------------------------------------------

    def add(self, ticket_id: str, fingerprint: str, state: str, metadata: dict[str, Any] | None = None) -> None:
        """Append or update an entry and persist atomically.

        Args:
            ticket_id: Ticket that produced the resolution.
            fingerprint: Stable finding fingerprint (non-empty).
            state: RESOLVED | SUPERSEDED | CANCELLED (upper-case string).
            metadata: Extra fields (modules, resolved_at, superseded_by, reason, by, etc.).
        """
        if not fingerprint or not fingerprint.strip():
            raise ValueError("fingerprint is required")
        if not ticket_id or not ticket_id.strip():
            raise ValueError("ticket_id is required")
        if not state or not state.strip():
            raise ValueError("state is required")
        meta = dict(metadata) if metadata else {}
        with self._lock:
            entry: dict[str, Any] = {
                "ticket_id": ticket_id.strip(),
                "state": state.strip().upper(),
                "at": time.time(),
            }
            # Metadata wins for overlapping keys except ticket_id/state which stay canonical;
            # but allow metadata to override "at" if it provides explicit timestamp.
            # Keep at from metadata if present, else use fresh time.
            if "at" in meta or "resolved_at" in meta or "updated_at" in meta:
                # Prefer explicit at from metadata if supplied
                explicit = meta.get("at", meta.get("resolved_at", meta.get("updated_at")))
                if isinstance(explicit, (int, float)):
                    entry["at"] = float(explicit)
            entry.update({k: v for k, v in meta.items() if k not in ("ticket_id", "state")})
            # Ensure ticket_id/state reflect the authoritative arguments
            entry["ticket_id"] = ticket_id.strip()
            entry["state"] = state.strip().upper()
            self._fingerprints[fingerprint.strip()] = entry
            # Enforce size cap
            if len(self._fingerprints) > MAX_ENTRIES:
                self._evict_oldest_locked(len(self._fingerprints) - MAX_ENTRIES)
            self._persist_locked()

    def check(self, fingerprint: str) -> dict[str, Any] | None:
        """Return match entry for *fingerprint* or None.

        Returned dict is a shallow copy so callers cannot mutate internals.
        """
        if not fingerprint:
            return None
        with self._lock:
            entry = self._fingerprints.get(fingerprint.strip())
            if entry is None:
                return None
            return dict(entry)

    def compact(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> int:
        """Dedup and remove entries older than TTL.

        Returns number of entries removed. Persists if any change occurred.
        """
        now = time.time()
        removed = 0
        with self._lock:
            # Remove stale entries
            to_delete: list[str] = []
            for fp, entry in self._fingerprints.items():
                at_val: float | None = None
                for key in ("at", "resolved_at", "updated_at"):
                    val = entry.get(key)
                    if isinstance(val, (int, float)):
                        at_val = float(val)
                        break
                if at_val is None:
                    continue
                if now - at_val > ttl_seconds:
                    to_delete.append(fp)
            for fp in to_delete:
                self._fingerprints.pop(fp, None)
                removed += 1

            # Dedup is inherent to dict keying; no extra work needed.
            # Enforce size cap after TTL pruning as well.
            if len(self._fingerprints) > MAX_ENTRIES:
                extra = len(self._fingerprints) - MAX_ENTRIES
                self._evict_oldest_locked(extra)
                removed += extra

            if removed > 0 or to_delete:
                self._persist_locked()
        return removed

    # -- helpers for inspection -----------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._fingerprints)

    def fingerprints(self) -> dict[str, dict[str, Any]]:
        """Return shallow copy of all fingerprints (for debugging)."""
        with self._lock:
            return {k: dict(v) for k, v in self._fingerprints.items()}
