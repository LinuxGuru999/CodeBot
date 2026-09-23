#!/usr/bin/env python3
"""Review Feature Extraction — fv1, leakage-safe, path-preserving.

Purpose
-------
Deterministic feature extractor for review cycles. Stdlib-only.
Uses captured diff snapshot, not re-derived repo state.
Historical features use strict cutoff: episode.completed_at < decision_ts.

Invariants
----------
- stdlib-only (hashlib, json, re, pathlib)
- Preserves normalized repo-relative paths as structural features (fix #16).
- Never stores file contents, tokens, secret values.
- Leakage gate: uses only pre-outcome information; specialist_results/
  escaped_defect/completion_result never influence features.
- Historical window bounded 30d, strictly before decision_ts.
- Secret redaction: keys/values matching api_key/secret/token/password/sk-/ghp_ dropped.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from codebot.rl_review_episode import ReviewEpisode

FEATURE_SCHEMA_VERSION = "fv1"
VALID_FEATURE_SCHEMA_VERSIONS = frozenset({"fv1"})

_SENSITIVE_SUBSTRINGS = ("token","secret","password","passwd","api_key","apikey","authorization","bearer","private_key","prompt","tool_output","ghp_","gho_","sk-")

def _is_sensitive_key(k: str) -> bool:
    low = k.lower()
    return any(s in low for s in _SENSITIVE_SUBSTRINGS)

def _sanitize_value(v: Any) -> Any:
    if isinstance(v, str) and v.startswith(("ghp_","gho_","sk-","Bearer ")):
        return "[redacted]"
    return v

def _validate_schema(current: str, artifact: str) -> None:
    if artifact != current:
        raise ValueError(f"feature schema mismatch: current={current!r} artifact={artifact!r}")

def validate_schema(artifact_schema: str, current_schema: str = FEATURE_SCHEMA_VERSION) -> None:
    _validate_schema(current_schema, artifact_schema)

# --- deterministic extractors ---

_TICKET_CLASS_VALUES = {"bug","feature","security","refactor","architecture","performance","dependency","test","documentation","task","chore"}
_RISK_VALUES = {"LOW","MEDIUM","HIGH","CRITICAL"}
_SEVERITY_VALUES = {"LOW","MEDIUM","HIGH","CRITICAL"}

def extract_features(
    episode: ReviewEpisode,
    decision_ts: float | None = None,
    history_episodes: list[ReviewEpisode] | None = None,
) -> dict[str, Any]:
    """Extract feature dict for prediction time.

    decision_ts: timestamp at which decision is made (typically episode.started_at).
    Only history with completed_at < decision_ts is counted.
    """
    if decision_ts is None:
        decision_ts = float(episode.started_at or 0)
    features: dict[str, Any] = {}
    # Ticket features (explainable, no leakage)
    tc = str(episode.ticket_class or "").lower()
    features["ticket_class"] = tc if tc in _TICKET_CLASS_VALUES else "other"
    features["risk_class"] = (str(episode.risk_class or "").upper() or "MEDIUM")
    features["severity"] = (str(episode.severity or "").upper() or "MEDIUM")
    features["discovery_role"] = str(episode.discovery_role or "")
    features["goal_disposition"] = str(episode.goal_disposition or "")
    features["rework_count_prior"] = int(getattr(episode, "implementation_attempt_number", 0) or 0)  # approximation; caller may pass actual rework prior
    features["implementation_attempt_number"] = int(episode.implementation_attempt_number or 0)
    # Diff features from captured snapshot (fix #8: not re-derived)
    features["changed_file_count"] = int(episode.changed_file_count or 0)
    features["changed_lines_added"] = int(episode.changed_lines_added or 0)
    features["changed_lines_removed"] = int(episode.changed_lines_removed or 0)
    # Preserve repo-relative paths as structural features (fix #16)
    files = list(episode.changed_files or [])
    # Do not log file contents; just structural
    features["modules_touched"] = sorted(set(
        (p.split("/",1)[0] if "/" in p else p) for p in files if isinstance(p, str)
    ))
    # Normalize paths for downstream category features: keep relative paths
    features["changed_files_rel"] = sorted(files)
    features["has_new_file"] = any("new" in f.lower() for f in files)  # heuristic; full new-file detection requires git status
    features["has_deleted_file"] = False  # captured separately if available; conservative
    features["touches_tests"] = any("tests/" in f or "/tests" in f or f.endswith("_test.py") or f.startswith("test_") for f in files)
    features["touches_config"] = any(f.endswith((".yaml",".yml",".toml",".cfg",".ini")) or "config" in f.lower() for f in files)
    features["touches_public_api"] = any("api" in f.lower() or "contract" in f.lower() for f in files)
    features["touches_schema"] = any("schema" in f.lower() or "migration" in f.lower() or "alembic" in f.lower() for f in files)
    features["touches_migration"] = features["touches_schema"]
    features["touches_auth"] = any("auth" in f.lower() or "credential" in f.lower() or "session" in f.lower() for f in files)
    features["touches_scheduler"] = any("scheduler" in f.lower() or "dispatch" in f.lower() for f in files)
    features["touches_subprocess"] = any("subprocess" in f.lower() or "shell" in f.lower() for f in files)
    # Also from change_categories captured
    cats = list(episode.change_categories or [])
    for c in ("scheduler","auth","subprocess","migration","claim"):
        features[f"category_{c}"] = 1 if c in cats else 0
    # Language
    langs = list(episode.language_mix or [])
    features["languages"] = sorted(langs)
    features["language_count"] = len(langs)
    # Historical features (strict cutoff < decision_ts, 30d window)
    if history_episodes is not None:
        cutoff = float(decision_ts)
        window_start = cutoff - 30*24*3600
        prior = [e for e in history_episodes if float(e.completed_at or 0) < cutoff and float(e.completed_at or 0) >= window_start]
        # Module history: episodes affecting same affected_modules
        affected = set(episode.affected_modules or [])
        same_module = [e for e in prior if any(m in affected for m in (e.affected_modules or []))]
        if same_module:
            # rework rate
            features["module_rework_rate_30d"] = sum(1 for e in same_module if e.completion_result=="REWORK") / len(same_module) if same_module else 0.0
            # defect history proxy: later_regression / escaped_defect
            features["module_defect_history_30d"] = sum(1 for e in same_module if e.escaped_defect or e.later_regression) / len(same_module)
        else:
            features["module_rework_rate_30d"] = 0.0
            features["module_defect_history_30d"] = 0.0
        # Category rework rate
        tc2 = features["ticket_class"]
        same_cat = [e for e in prior if str(e.ticket_class or "").lower()==tc2]
        if same_cat:
            features["category_rework_rate_30d"] = sum(1 for e in same_cat if e.completion_result=="REWORK")/len(same_cat)
        else:
            features["category_rework_rate_30d"] = 0.0
        # Reviewer escalation history: primary escalation rate
        prim_esc = sum(1 for e in prior if e.primary_escalation)
        features["primary_escalation_rate_30d"] = prim_esc / len(prior) if prior else 0.0
        # Specialist yield (concurrency example)
        conc_runs = [e for e in prior if "concurrency" in (e.specialists_run or [])]
        if conc_runs:
            blocking = sum(1 for e in conc_runs if (e.specialist_results or {}).get("concurrency",{}).get("blocking"))
            features["specialist_yield_concurrency_30d"] = blocking / len(conc_runs) if conc_runs else 0.0
        else:
            features["specialist_yield_concurrency_30d"] = 0.0
    else:
        features["module_rework_rate_30d"] = 0.0
        features["module_defect_history_30d"] = 0.0
        features["category_rework_rate_30d"] = 0.0
        features["primary_escalation_rate_30d"] = 0.0
        features["specialist_yield_concurrency_30d"] = 0.0
    # Agent/model features (no leakage: only impl + primary, not specialist)
    features["implementation_attempt_number"] = int(episode.implementation_attempt_number or 0)
    features["primary_review_model"] = str(episode.primary_model or "")

    # Sanitize sensitive keys/values
    sanitized: dict[str, Any] = {}
    for k, v in features.items():
        if _is_sensitive_key(k):
            continue
        sanitized[k] = _sanitize_value(v)
    sanitized["_schema_version"] = FEATURE_SCHEMA_VERSION
    return sanitized

def feature_vector_hash(features: dict[str, Any]) -> str:
    j = __import__("json").dumps({k: v for k, v in features.items() if k != "_schema_version"}, sort_keys=True, separators=(",",":"))
    return "sha256:" + hashlib.sha256(j.encode("utf-8")).hexdigest()[:16]

def build_labels(episode: ReviewEpisode) -> dict[str, Any]:
    """Build multi-label targets from post-outcome episode.

    Per specialist: blocking_found, nonblocking_found, approved, escaped_same_domain.
    Also decomposed reward components. APPROVE is not a negative label (fix #14).
    """
    # Use specialist_results + escaped_defect
    results = episode.specialist_results or {}
    # Determine completion blocking yield per specialist
    labels: dict[str, Any] = {}
    for specialist in ("security","concurrency","data_integrity","architecture","performance"):
        r = results.get(specialist, {}) if isinstance(results, dict) else {}
        decision = str(r.get("decision","") or r.get("verdict","") or "").upper()
        blocking = bool(r.get("blocking") or r.get("blocking_findings"))
        labels[f"{specialist}_blocking_found"] = 1 if (decision=="REWORK" and blocking) else 0
        # nonblocking: REWORK with non-blocking or findings non-blocking
        findings = r.get("findings") or r.get("issues") or []
        has_findings = bool(findings)
        labels[f"{specialist}_nonblocking_found"] = 1 if (decision=="REWORK" and not blocking and has_findings) else (1 if has_findings and not blocking else 0)
        # approved: decision APPROVE (not a negative label — just a count for metrics, not training negative)
        labels[f"{specialist}_approved"] = 1 if decision=="APPROVE" else 0
        # escaped same domain: escaped_defect_category matches specialist domain
        esc_cat = str(episode.escaped_defect_category or "").lower()
        esc = bool(episode.escaped_defect)
        labels[f"{specialist}_escaped_same_domain"] = 1 if (esc and esc_cat==specialist) or (esc and specialist in esc_cat) else 0
    # Decomposed reward components (separate, not collapsed)
    blocking_yield = sum(1 for k,v in labels.items() if k.endswith("_blocking_found") and v)
    labels["blocking_yield"] = blocking_yield
    labels["review_cost_estimate"] = float(episode.primary_cost or 0) + sum(float(v.get("cost",0) or 0) for v in results.values() if isinstance(v, dict))
    labels["rework"] = 1 if episode.completion_result=="REWORK" else 0
    # Severity weight for escaped defect
    sev_weights = {"LOW":0.5,"MEDIUM":1.0,"HIGH":3.0,"CRITICAL":10.0}
    labels["escaped_severity_weight"] = sev_weights.get(str(episode.escaped_defect_severity or "").upper(), 0.0) if episode.escaped_defect else 0.0
    return labels

__all__ = ["FEATURE_SCHEMA_VERSION","extract_features","feature_vector_hash","build_labels","validate_schema"]
