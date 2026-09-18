"""Maintain the daily actual token ledger used by the bot scheduler.

Purpose
-------
Provides atomic, UTC-day token accounting and pure budget-state decisions for
the scheduler.

Why
---
The budget must measure provider-reported actuals, not prompt-size estimates.
An exclusive lock and replace-based writes prevent concurrent runners from
overwriting one another, while malformed data fails closed to avoid overspend.
"""

import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CAP = 4_000_000_000
LOCK_TIMEOUT = 5.0

# T4.3 incremental adapter seam
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
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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


_FLUSH_AFTER_WRITES = max(1, int(os.getenv("CODEBOT_LEDGER_FLUSH_EVERY", "1")))
_pending_writes = 0


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
    """Add provider-reported actual usage for one model and UTC day."""
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("actual token counts must be non-negative")
    ledger_path = _ledger_path(path)
    global _pending_writes
    _pending_writes += 1
    if _pending_writes < _FLUSH_AFTER_WRITES:
        ledger: dict[str, Any] = _new_ledger(day_utc)
        try:
            ledger = _read(ledger_path) if ledger_path.exists() else _new_ledger(day_utc)
            if ledger.get("day_utc") != day_utc:
                ledger = _new_ledger(day_utc)
            row = ledger["by_model"].setdefault(model, {})
            row["prompt_actual"] = int(row.get("prompt_actual", 0)) + prompt_tokens
            row["completion_actual"] = int(row.get("completion_actual", 0)) + completion_tokens
            row["prompt_estimated"] = int(row.get("prompt_estimated", 0)) + max(0, int(prompt_estimated))
            row["completion_estimated"] = int(row.get("completion_estimated", 0)) + max(0, int(completion_estimated))
            ledger["total_actual"] = int(ledger.get("total_actual", 0)) + prompt_tokens + completion_tokens
            _write(ledger_path, ledger)
        except Exception:
            pass
        return ledger
    _pending_writes = 0
    return record_usage_locked(day_utc, model, prompt_tokens, completion_tokens, path=path, prompt_estimated=prompt_estimated, completion_estimated=completion_estimated)


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
    """Locked ledger write; merges the accumulated pending in-memory delta."""
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("actual token counts must be non-negative")
    ledger_path = _ledger_path(path)
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
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def day_total(day_utc: str, *, path: str | os.PathLike[str] | None = None) -> int:
    """Return actual usage for a UTC day, failing closed on corruption."""
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
    """Return ok, warn, shed_tier3, or stop for actual usage total."""
    if total >= cap:
        return "stop"
    if total >= cap * 0.9:
        return "shed_tier3"
    if total >= cap * 0.8:
        return "warn"
    return "ok"


def current_day_utc() -> str:
    """Return the current UTC calendar date in ISO format."""
    return datetime.now(timezone.utc).date().isoformat()
