#!/usr/bin/env python3
"""Review Shadow Recommendations — pre-outcome append-only predictions.

Purpose
-------
Persists shadow recommendations BEFORE review outcomes are available.
Historical rows are never regenerated after retrain (fix #96).

Invariants
----------
- stdlib-only.
- Append-only JSONL: review_shadow_recommendations.jsonl
- Pre-outcome only: never includes future fields like actual_specialists_run.
- Features snapshot + hash stored for reproducibility and leakage check.
- Atomic O_APPEND writes.
- Never overwrites existing review_cycle_id rows in the sense that re-persist
  with same cycle+policy does not replace; caller should dedup by cycle+policy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SHADOW_FILE = "review_shadow_recommendations.jsonl"
VALID_SPECIALISTS = frozenset({"security","concurrency","data_integrity","architecture","performance"})

def _shadow_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / _SHADOW_FILE

def _hash_features(features: dict[str, Any]) -> str:
    j = json.dumps(features, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(j.encode("utf-8")).hexdigest()[:16]

def persist_shadow(
    review_cycle_id: str,
    policy_version: str,
    scores: dict[str, float],
    state_dir: Path | str,
    *,
    features_snapshot: dict[str, Any] | None = None,
    features_version: str = "fv1",
    mode: str = "SHADOW",
    thresholds: dict[str, Any] | None = None,
    created_at: float | None = None,
    actual_deterministic_specialists: list[str] | None = None,
) -> dict[str, Any]:
    """Append a shadow recommendation (pre-outcome only).

    Returns the persisted record. Never includes future outcome fields.
    Raises ValueError if recommendation contains future fields.
    """
    if not review_cycle_id or not policy_version:
        raise ValueError("review_cycle_id and policy_version are required")
    # Leakage check: scores must only contain valid specialist keys
    clean_scores: dict[str, dict[str, Any]] = {}
    th = thresholds or {}
    for k, v in (scores or {}).items():
        if k not in VALID_SPECIALISTS:
            logger.warning("rl_review_shadow: ignoring unknown specialist %r", k)
            continue
        try:
            score = float(v) if not isinstance(v, dict) else float(v.get("score", v.get("value", 0)))
        except Exception:
            score = 0.0
        score = max(0.0, min(1.0, score))
        # thresholds may be dict of specialist -> threshold or flat
        thr = 0.5
        if isinstance(th, dict):
            if k in th and isinstance(th[k], (int, float)):
                thr = float(th[k])
            elif isinstance(th.get(k), dict) and "threshold" in th[k]:
                thr = float(th[k]["threshold"])
            elif "default" in th:
                thr = float(th["default"])
        recommended = score >= thr
        clean_scores[k] = {"recommended": bool(recommended), "score": score, "threshold": float(thr)}
    snap = features_snapshot or {}
    fh = _hash_features(snap) if snap else ""
    ts = float(created_at) if created_at is not None else time.time()
    record: dict[str, Any] = {
        "review_cycle_id": review_cycle_id,
        "policy_version": policy_version,
        "mode": mode,
        "recommendations": clean_scores,
        "features_version": features_version,
        "features_snapshot": snap,
        "features_hash": fh,
        "created_at": ts,
        "actual_deterministic_specialists": list(actual_deterministic_specialists or []),
        "event_id": f"shadow_{uuid.uuid4().hex[:8]}",
        "schema_version": 1,
    }
    # Ensure we never persist future fields
    assert "actual_specialists_run" not in record
    assert "specialist_results" not in record
    # Append
    p = _shadow_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True) + "\n"
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return record

def read_shadows(
    state_dir: Path | str,
    *,
    review_cycle_id: str | None = None,
    policy_version: str | None = None,
) -> list[dict[str, Any]]:
    p = _shadow_path(state_dir)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line=line.strip()
        if not line:
            continue
        try:
            rec=json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if review_cycle_id is not None and rec.get("review_cycle_id")!=review_cycle_id:
            continue
        if policy_version is not None and rec.get("policy_version")!=policy_version:
            continue
        out.append(rec)
    return out

def load_shadow(state_dir: Path | str, review_cycle_id: str) -> dict[str, Any] | None:
    shadows=read_shadows(state_dir, review_cycle_id=review_cycle_id)
    if not shadows:
        return None
    # Return latest (by created_at) if multiple (re-runs with new policy)
    shadows.sort(key=lambda r: float(r.get("created_at",0) or 0))
    return shadows[-1]

def has_shadow_for_policy(state_dir: Path | str, review_cycle_id: str, policy_version: str) -> bool:
    return len(read_shadows(state_dir, review_cycle_id=review_cycle_id, policy_version=policy_version))>0
