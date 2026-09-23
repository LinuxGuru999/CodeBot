"""Token ledger — decoupled from scheduling.

MAX_CONCURRENCY is the sole scheduling capacity limit. This module only
records token usage for reporting; no scheduling gate should consult it
for admission decisions.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codebot.file_lock import flock, LOCK_EX, LOCK_UN, LOCK_NB

CAP = 4_000_000_000
LOCK_TIMEOUT = 5.0

_adapter_instance: object | None = None


def set_project_adapter(adapter: object) -> None:
    global _adapter_instance
    _adapter_instance = adapter


def get_adapter() -> object | None:
    return _adapter_instance


def _ledger_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path is not None:
        return Path(path)
    if _adapter_instance is not None:
        try:
            return _adapter_instance.paths().state_dir / "token_ledger.json"  # type: ignore[union-attr]
        except Exception:
            pass
    return Path(__file__).parent / "state" / "token_ledger.json"


def _valid_ledger(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("day_utc"), str) and isinstance(data.get("by_model"), dict)


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid token ledger: {path}") from exc
    if not _valid_ledger(data):
        raise ValueError(f"invalid token ledger: {path}")
    return data


def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = Path(str(path) + ".lock")
    fp = lock.open("a+", encoding="utf-8")
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            flock(fp.fileno(), LOCK_EX | LOCK_NB)
            return fp
        except BlockingIOError:
            if time.monotonic() >= deadline:
                fp.close()
                raise TimeoutError("budget-unknown")
            time.sleep(0.05)


def _write(path: Path, data: dict[str, Any]) -> None:
    tmp = Path(str(path) + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _new_ledger(day_utc: str) -> dict[str, Any]:
    return {"day_utc": day_utc, "by_model": {}, "total_actual": 0}


_thread_locks: dict[Path, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def _thread_lock(path: Path) -> threading.Lock:
    with _thread_locks_guard:
        return _thread_locks.setdefault(path, threading.Lock())


def record_usage(
    day_utc: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    path: str | os.PathLike[str] | None = None,
    prompt_estimated: int = 0,
    completion_estimated: int = 0,
) -> dict[str, Any]:
    """Record token usage with proper file locking.
    
    All writes are protected by both threading and file locks to prevent
    race conditions when multiple subprocesses write concurrently.
    """
    return record_usage_locked(
        day_utc,
        model,
        prompt_tokens,
        completion_tokens,
        path=path,
        prompt_estimated=prompt_estimated,
        completion_estimated=completion_estimated,
    )


def record_usage_locked(
    day_utc: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    path: str | os.PathLike[str] | None = None,
    prompt_estimated: int = 0,
    completion_estimated: int = 0,
) -> dict[str, Any]:
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("actual token counts must be non-negative")
    ledger_path = _ledger_path(path)
    with _thread_lock(ledger_path):
        lock = _locked(ledger_path)
        try:
            if ledger_path.exists():
                ledger = _read(ledger_path)
                if ledger["day_utc"] != day_utc:
                    ledger = _new_ledger(day_utc)
            else:
                ledger = _new_ledger(day_utc)
            row = ledger["by_model"].setdefault(model, {})
            for key in ("prompt_actual", "completion_actual", "prompt_estimated", "completion_estimated"):
                row[key] = int(row.get(key, 0))
            row["prompt_actual"] += prompt_tokens
            row["completion_actual"] += completion_tokens
            row["prompt_estimated"] += max(0, int(prompt_estimated))
            row["completion_estimated"] += max(0, int(completion_estimated))
            ledger["total_actual"] = sum(
                int(values.get("prompt_actual", 0)) + int(values.get("completion_actual", 0))
                for values in ledger["by_model"].values()
            )
            _write(ledger_path, ledger)
            return ledger
        finally:
            flock(lock.fileno(), LOCK_UN)
            lock.close()


def day_total(day_utc: str, *, path: str | os.PathLike[str] | None = None) -> int:
    ledger_path = _ledger_path(path)
    if not ledger_path.exists():
        return 0
    try:
        ledger = _read(ledger_path)
    except ValueError:
        return CAP
    if ledger["day_utc"] != day_utc:
        return 0
    return sum(int(v.get("prompt_actual", 0)) + int(v.get("completion_actual", 0)) for v in ledger["by_model"].values())


def get_budget_state(total: int, cap: int = CAP) -> str:
    if total >= cap:
        return "stop"
    if total >= cap * 0.9:
        return "shed_tier3"
    if total >= cap * 0.8:
        return "warn"
    return "ok"


def current_day_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()
