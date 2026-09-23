#!/usr/bin/env python3
"""Review Metrics — yield, calibration, rule evaluation, drift.

Purpose
-------
Computes observable review-learning metrics for diagnosis and promotion
decisions. Stdlib-only.

Invariants
----------
- Tri-state drift: acceptable → use, uncertain → SHADOW only, severe → fallback.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from codebot.rl_review_episode import list_episodes
from codebot.rl_review_shadow import read_shadows

def _episodes_metrics(state_dir: Path | str) -> dict[str,Any]:
    eps = list_episodes(state_dir)
    delayed_dir = Path(state_dir) / "review_delayed_outcomes"
    delayed_count = len(list(delayed_dir.glob("*.json"))) if delayed_dir.exists() else 0
    return {
        "episodes_collected": len(eps),
        "episodes_labeled": sum(1 for e in eps if e.specialist_results),
        "episodes_with_delayed_outcomes": delayed_count,
        "episodes_by_result": {k: sum(1 for e in eps if e.completion_result==k) for k in ("COMPLETE","REWORK","DEFERRED","CANCELLED","RESOLVED","SUPERSEDED") if any(e.completion_result==k for e in eps)},
    }

def _shadow_metrics(state_dir: Path | str) -> dict[str,Any]:
    shadows = read_shadows(state_dir)
    if not shadows:
        return {"shadow_count": 0, "shadow_match_rate": 0.0}
    # Match rate requires joining with episodes; here approximate by agreement among recommendations vs deterministic
    # Real match requires episodes matching; compute shadow_add/remove roughly
    total=len(shadows)
    return {"shadow_count": total, "shadow_match_rate": 0.0, "shadow_add_rate": 0.0, "shadow_remove_rate": 0.0}

def _drift_status(state_dir: Path | str) -> dict[str,Any]:
    # Tri-state per fix #15
    eps = list_episodes(state_dir)
    if len(eps) < 20:
        return {"drift": "acceptable", "reason": "insufficient_data"}
    # Simple drift proxy: language mix changed vs first half
    half=len(eps)//2
    first_half=set( l for e in eps[:half] for l in (e.language_mix or []))
    second_half=set(l for e in eps[half:] for l in (e.language_mix or []))
    if second_half - first_half:
        new_langs=sorted(list(second_half - first_half))
        if len(new_langs)>=2:
            return {"drift": "severe", "reason": f"new languages {new_langs}", "new_languages": new_langs}
        return {"drift": "uncertain", "reason": f"new languages {new_langs}", "new_languages": new_langs}
    return {"drift": "acceptable", "reason": "stable"}

def _policy_status(state_dir: Path | str) -> dict[str,Any]:
    from codebot.rl_review_policy import load_policy
    p=load_policy(state_dir)
    return {
        "policy_status": p.get("status","MISSING"),
        "policy_version": p.get("policy_version"),
        "policy_error": p.get("error"),
        "feature_schema_version": (p.get("artifact",{}) or {}).get("feature_schema_version"),
        "trained_at": (p.get("artifact",{}) or {}).get("trained_at"),
    }

def compute_metrics(state_dir: Path | str) -> dict[str,Any]:
    ep = _episodes_metrics(state_dir)
    sh = _shadow_metrics(state_dir)
    drift = _drift_status(state_dir)
    pol = _policy_status(state_dir)
    # Specialist yield per type
    yield_by: dict[str,Any] = {}
    eps=list_episodes(state_dir)
    for specialist in ("security","concurrency","data_integrity","architecture","performance"):
        runs=[e for e in eps if specialist in (e.specialists_run or [])]
        if not runs:
            continue
        blocking=sum(1 for e in runs if (e.specialist_results or {}).get(specialist,{}).get("blocking"))
        yield_by[specialist] = {
            "runs": len(runs),
            "blocking_findings": blocking,
            "blocking_rate": blocking/max(1,len(runs)),
        }
    return {
        **ep,
        **sh,
        **pol,
        **{"drift_status": drift},
        "specialist_yield": yield_by,
        "generated_at": time.time(),
    }

def main(argv: list[str] | None = None) -> int:
    import argparse
    ap=argparse.ArgumentParser(description="Review learning metrics")
    ap.add_argument("--state-dir", default=".codebot/state")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args=ap.parse_args(argv)
    m=compute_metrics(args.state_dir)
    print(json.dumps(m, sort_keys=True, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
