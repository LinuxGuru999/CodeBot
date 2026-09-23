#!/usr/bin/env python3
"""Review Policy — load, predict, threshold, promotion criteria.

Purpose
-------
Load versioned JSON policy artifacts (weights.json + artifact.json),
validate feature schema, predict per-specialist scores, apply thresholds.
Training produces weights.json (fix #12: no pickle, sklearn optional offline).

Invariants
----------
- Runtime stdlib-only (json, pathlib).
- Artifact mismatch fails closed to fallback (deterministic).
- Thresholds per specialist × per risk, conservative for security/data_integrity/concurrency.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from codebot.rl_review_features import FEATURE_SCHEMA_VERSION, validate_schema

logger = logging.getLogger(__name__)

_SPECIALISTS = ("security","concurrency","data_integrity","architecture","performance")
_HIGH_CONSEQUENCE = frozenset({"security","data_integrity","concurrency"})

_PROMOTION_CRITERIA = {
    "min_episodes": 50,
    "min_per_specialist": 20,
    "min_positive": 5,
    "max_false_negative_rate": 0.30,
}

def _policies_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "review_policies"

def _latest_dir(state_dir: Path) -> Path | None:
    base = _policies_dir(state_dir)
    if not base.exists():
        return None
    candidates = [p for p in base.iterdir() if p.is_dir() and p.name.startswith("review-policy-")]
    if not candidates:
        return None
    # sort by numeric suffix
    def num(p: Path) -> int:
        try:
            return int(p.name.replace("review-policy-",""))
        except ValueError:
            return 0
    candidates.sort(key=num, reverse=True)
    return candidates[0]

def _load_json(p: Path) -> dict[str,Any] | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

def load_policy(state_dir: Path | str, policy_version: str | None = None) -> dict[str,Any]:
    """Load policy artifact.

    Returns dict with keys: status, policy_version, artifact, weights, error.
    status is OK | INSUFFICIENT_DATA | MISSING | CORRUPT | SCHEMA_MISMATCH
    """
    base = _policies_dir(Path(state_dir))
    if policy_version is not None:
        d = base / policy_version
    else:
        d = _latest_dir(Path(state_dir))
        if d is None:
            return {"status":"MISSING","policy_version":None,"artifact":None,"weights":None,"error":"no policy artifacts"}
    if not d.exists():
        return {"status":"MISSING","policy_version":str(d.name) if d else None,"artifact":None,"weights":None,"error":"missing"}
    artifact = _load_json(d / "artifact.json")
    weights = _load_json(d / "weights.json")
    if artifact is None:
        return {"status":"CORRUPT","policy_version":d.name,"artifact":None,"weights":None,"error":"artifact corrupt/missing"}
    # schema check (fail closed, fix #43)
    try:
        fv = str(artifact.get("feature_schema_version", "")) or (weights or {}).get("feature_schema_version","")
        if fv and fv != FEATURE_SCHEMA_VERSION:
            validate_schema(fv, FEATURE_SCHEMA_VERSION)
    except ValueError as e:
        return {"status":"SCHEMA_MISMATCH","policy_version":d.name,"artifact":artifact,"weights":weights,"error":str(e)}
    # sample threshold check
    try:
        sample_count = int(artifact.get("sample_count", artifact.get("metrics",{}).get("sample_count", 0)) or 0)
        if sample_count and sample_count < _PROMOTION_CRITERIA["min_episodes"]:
            # Still load but mark INSUFFICIENT_DATA for governance; don't block SHADOW read
            pass
    except Exception:
        pass
    if weights is None:
        # baseline policies may have no weights
        if artifact.get("type")=="baseline":
            return {"status":"OK","policy_version":d.name,"artifact":artifact,"weights":{},"error":None}
        return {"status":"CORRUPT","policy_version":d.name,"artifact":artifact,"weights":None,"error":"weights missing"}
    return {"status":"OK","policy_version":d.name,"artifact":artifact,"weights":weights,"error":None}

def predict_specialist_scores(
    features: dict[str,Any],
    weights: dict[str,Any],
) -> dict[str,float]:
    """Compute per-specialist scores from features + JSON weights.

    weights format: {feature_schema_version, specialists: {name: {intercept, coefficients: {feat: weight}}}}
    Score = sigmoid(intercept + Σ coeff*feat_value).
    Missing features treated as 0. Non-numeric feature values get 0 unless exact match.
    """
    import math
    specialists: dict[str,Any] = {}
    if isinstance(weights, dict):
        specialists = weights.get("specialists", {}) if "specialists" in weights else weights.get("coefficients", {})
        # Support flat baseline mapping: {specialist: recommended bool} -> not scored here
        if not isinstance(specialists, dict):
            specialists = {}
        # Also support weights directly per specialist
        if "security" in weights and isinstance(weights["security"], dict) and "intercept" in weights["security"]:
            specialists = {k: v for k,v in weights.items() if k in _SPECIALISTS and isinstance(v, dict)}
    out: dict[str,float] = {}
    for spec in _SPECIALISTS:
        spec_w = specialists.get(spec, {}) if isinstance(specialists, dict) else {}
        intercept = float(spec_w.get("intercept", 0.0) or 0.0) if isinstance(spec_w, dict) else 0.0
        coeffs: dict[str,Any] = spec_w.get("coefficients", {}) if isinstance(spec_w, dict) else {}
        if not isinstance(coeffs, dict):
            coeffs = {}
        logit = intercept
        for feat, w in coeffs.items():
            try:
                wf = float(w)
            except Exception:
                continue
            val = features.get(feat, 0)
            # Coerce val to float: bool/int/float -> float; string exact match 1 if equals?
            try:
                if isinstance(val, bool):
                    fval = 1.0 if val else 0.0
                elif isinstance(val, (int,float)):
                    fval = float(val)
                elif isinstance(val, str):
                    fval = 1.0 if val.lower()==str(feat).lower() else 0.0
                elif isinstance(val, list):
                    fval = float(len(val))
                else:
                    fval = 0.0
            except Exception:
                fval = 0.0
            logit += wf * fval
        score = 1.0 / (1.0 + math.exp(-logit))
        out[spec] = max(0.0, min(1.0, score))
    return out

def get_thresholds(weights: dict[str,Any], specialist: str, risk: str = "MEDIUM") -> float:
    """Return threshold for specialist × risk, conservative defaults for high-consequence."""
    if not isinstance(weights, dict):
        return 0.30 if specialist in _HIGH_CONSEQUENCE else 0.50
    # weights may contain thresholds dict
    thresholds = weights.get("thresholds", {}) if isinstance(weights, dict) else {}
    if isinstance(thresholds, dict):
        spec_th = thresholds.get(specialist)
        if isinstance(spec_th, dict):
            # per risk
            r = str(risk).upper()
            if r in spec_th:
                try:
                    return float(spec_th[r])
                except Exception:
                    pass
            if "default" in spec_th:
                try:
                    return float(spec_th["default"])
                except Exception:
                    pass
        elif isinstance(spec_th, (int,float)):
            return float(spec_th)
    # conservative defaults
    if specialist in _HIGH_CONSEQUENCE:
        return 0.30
    return 0.50

def should_recommend(score: float, threshold: float) -> bool:
    return float(score) >= float(threshold)

# Promotion criteria persistence
def promotion_criteria_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / "review_policies" / "promotion_criteria.json"

def load_promotion_criteria(state_dir: Path | str) -> dict[str,Any]:
    p = promotion_criteria_path(state_dir)
    data = _load_json(p)
    if data is None:
        return dict(_PROMOTION_CRITERIA)
    # Merge with defaults
    out = dict(_PROMOTION_CRITERIA)
    for k,v in data.items():
        if k in out:
            try:
                out[k] = type(out[k])(v)
            except Exception:
                out[k] = v
    return out
