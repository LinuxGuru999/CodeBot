"""Persist atomic queue-work leases outside the Markdown queue.

Purpose
-------
Provides owner-checked lease acquisition, renewal, release, retry, and
dead-letter operations for manifest-scheduled work.

Why
---
Queue rows are a human-facing contract. Keeping lease state in a separately
locked JSON file makes concurrent claims recoverable without mutating that
format.

Invariants
----------
- One advisory lock serializes every lease state transition.
- Writes use a pid-unique temporary file and atomic replacement.
- Dead-letter records are unique by work-item id.
"""

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from codebot.file_lock import flock, LOCK_EX, LOCK_UN


@contextmanager
def _locked_state(state_dir: Path) -> Iterator[dict]:
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "leases.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        flock(lock.fileno(), LOCK_EX)
        try:
            state_path = state_dir / "leases.json"
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))
            else:
                state = {"leases": {}, "attempts": {}, "dead_letters": []}
            yield state
            temporary_path = state_path.with_name(f"{state_path.name}.{os.getpid()}.tmp")
            temporary_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            temporary_path.replace(state_path)
        finally:
            flock(lock.fileno(), LOCK_UN)


def acquire(state_dir: Path, item_id: str, owner: str, *, now: float, lease_seconds: int, max_attempts: int = 3) -> dict:
    with _locked_state(state_dir) as state:
        lease = state["leases"].get(item_id)
        if lease and float(lease["expires_at"]) > now:
            return {"status": "busy", "owner": lease["owner"]}
        attempt = int(state["attempts"].get(item_id, 0)) + 1
        if attempt > max_attempts:
            return {"status": "dead-letter"}
        state["attempts"][item_id] = attempt
        state["leases"][item_id] = {"owner": owner, "expires_at": now + lease_seconds, "attempt": attempt}
        return {"status": "acquired", "attempt": attempt}


def renew(state_dir: Path, item_id: str, owner: str, *, now: float, lease_seconds: int) -> dict:
    with _locked_state(state_dir) as state:
        lease = state["leases"].get(item_id)
        if not lease or lease["owner"] != owner:
            return {"status": "not-owner"}
        lease["expires_at"] = now + lease_seconds
        return {"status": "renewed"}


def release(state_dir: Path, item_id: str, owner: str) -> dict:
    with _locked_state(state_dir) as state:
        lease = state["leases"].get(item_id)
        if not lease or lease["owner"] != owner:
            return {"status": "not-owner"}
        del state["leases"][item_id]
        state["attempts"].pop(item_id, None)
        return {"status": "released"}


def fail(state_dir: Path, item_id: str, owner: str, reason: str, *, max_attempts: int = 3) -> dict:
    with _locked_state(state_dir) as state:
        lease = state["leases"].get(item_id)
        if not lease or lease["owner"] != owner:
            return {"status": "not-owner"}
        del state["leases"][item_id]
        if int(lease["attempt"]) < max_attempts:
            return {"status": "retry"}
        if not any(letter["id"] == item_id for letter in state["dead_letters"]):
            state["dead_letters"].append({"id": item_id, "reason": reason})
        return {"status": "dead-letter"}


def dead_letters(state_dir: Path) -> list[dict]:
    with _locked_state(state_dir) as state:
        return list(state["dead_letters"])


def retry_dead_letter(state_dir: Path, item_id: str) -> dict:
    with _locked_state(state_dir) as state:
        for index, letter in enumerate(state["dead_letters"]):
            if letter["id"] == item_id:
                del state["dead_letters"][index]
                state["attempts"].pop(item_id, None)
                return {"status": "retried", "id": item_id}
        return {"status": "not-found", "id": item_id}
