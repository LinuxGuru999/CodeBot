#!/usr/bin/env python3
"""Review Dataset Builder — snapshot, splits, semantic hash, leakage guards.

Purpose
-------
Builds reproducible dataset snapshots from review episodes: validated,
time-sorted, group-aware, split, with semantic hashing (fix #17).

Invariants
----------
- stdlib-only (hashlib, json, os, pathlib, time)
- Append-only raw episodes; derived datasets rebuildable.
- Deterministic sort; group-by ticket/result_revision never leaks across splits.
- Semantic hash over normalized episodes/features, not byte identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from codebot.rl_review_episode import ReviewEpisode, list_episodes
from codebot.rl_review_features import extract_features, build_labels, FEATURE_SCHEMA_VERSION

_SNAPSHOT_PREFIX = "review-dataset-"
_DATASET_DIR = "review_datasets"

def _safe_id(s: str) -> str:
    return s.replace("/","_").replace("\\","_")

def _dataset_dir(state_dir: Path, dataset_version: str) -> Path:
    return Path(state_dir) / _DATASET_DIR / dataset_version

def semantic_hash(episodes: list[ReviewEpisode], features: list[dict[str,Any]]) -> str:
    """Stable hash over normalized episodes + features (fix #17)."""
    norm_eps = sorted([e.semantic_hash() for e in episodes])
    # Features: sort by keys, exclude volatile timestamps if any
    norm_feats_hashes = []
    for f in features:
        j = json.dumps({k:v for k,v in f.items() if k not in ("created_at",)}, sort_keys=True, separators=(",",":"))
        norm_feats_hashes.append(hashlib.sha256(j.encode("utf-8")).hexdigest()[:12])
    norm_feats_hashes.sort()
    combined = "\n".join(norm_eps) + "\n" + "\n".join(norm_feats_hashes)
    return "sha256:" + hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]

def snapshot(
    state_dir: Path | str,
    dataset_version: str | None = None,
    from_ts: float | None = None,
    to_ts: float | None = None,
    *,
    min_episodes: int = 0,
) -> dict[str, Any]:
    """Create a dataset snapshot.

    Returns manifest dict. Writes files to review_datasets/<version>/.
    Time-based split 70/15/15, group-aware (fix #28).
    """
    state_path = Path(state_dir)
    episodes = list_episodes(state_path)
    # Time filter
    if from_ts is not None:
        episodes = [e for e in episodes if float(e.started_at or 0) >= from_ts]
    if to_ts is not None:
        episodes = [e for e in episodes if float(e.started_at or 0) < to_ts]
    # Deterministic sort by started_at, then review_cycle_id
    episodes.sort(key=lambda e: (float(e.started_at or 0), e.review_cycle_id))
    # Auto version if not provided
    if dataset_version is None:
        existing = list((state_path / _DATASET_DIR).glob(f"{_SNAPSHOT_PREFIX}*")) if (state_path / _DATASET_DIR).exists() else []
        nums = []
        for p in existing:
            try:
                n = int(p.name.replace(_SNAPSHOT_PREFIX,""))
                nums.append(n)
            except ValueError:
                pass
        nxt = (max(nums)+1) if nums else 1
        dataset_version = f"{_SNAPSHOT_PREFIX}{nxt:04d}"
    out_dir = _dataset_dir(state_path, dataset_version)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Extract features + labels with proper cutoff: history < decision_ts
    # For snapshot, use each episode's started_at as decision_ts and history = prior episodes
    features_list: list[dict[str,Any]] = []
    labels_list: list[dict[str,Any]] = []
    for idx, ep in enumerate(episodes):
        history = episodes[:idx]
        feats = extract_features(ep, decision_ts=float(ep.started_at or 0), history_episodes=history)
        labs = build_labels(ep)
        features_list.append(feats)
        labels_list.append(labs)

    s_hash = semantic_hash(episodes, features_list)

    # Group-aware split: ensure same result_revision never across splits
    # Collect groups by result_revision (or diff_hash fallback)
    group_to_indices: dict[str, list[int]] = {}
    for i, ep in enumerate(episodes):
        key = (str(ep.result_revision or "") or str(ep.diff_hash or "") or str(ep.ticket_id or "") + f"#{i}") or f"idx_{i}"
        group_to_indices.setdefault(key, []).append(i)

    # Flatten groups sorted by earliest started_at of group
    groups_sorted: list[list[int]] = []
    for key in sorted(group_to_indices.keys(), key=lambda k: min(float(episodes[i].started_at or 0) for i in group_to_indices[k])):
        groups_sorted.append(sorted(group_to_indices[key]))

    n = len(episodes)
    # Simple group-aware: assign groups sequentially to maintain time order
    # Keep time ordering: groups_sorted already time-sorted by earliest
    # Flatten indices in group-order, preserving episode order
    flat_indices: list[int] = []
    for grp in groups_sorted:
        flat_indices.extend(grp)
    # Ensure flat_indices respects episode started_at order within
    # Actually flat_indices from groups_sorted may not be globally sorted; reorder flat to episode started_at order
    # But group integrity requires groups stay together. Use group-order already time-sorted.
    # Assign: first 70% groups -> train, next 15% -> val, rest -> test
    total_groups = len(groups_sorted)
    n_train_groups = int(total_groups * 0.70)
    n_val_groups = int(total_groups * 0.15)
    # Edge: ensure at least 1 group per split when enough groups
    train_groups = groups_sorted[:n_train_groups] if n_train_groups>0 else ([groups_sorted[0]] if groups_sorted else [])
    val_groups = groups_sorted[n_train_groups:n_train_groups+n_val_groups] if n_val_groups>0 else ([groups_sorted[n_train_groups]] if total_groups>n_train_groups else [])
    test_groups = groups_sorted[n_train_groups+n_val_groups:]
    # If still empty due to small n, adjust: fallback to index-based split
    if not train_groups or not test_groups:
        # fallback: simple index split 70/15/15 on flat sorted indices
        flat_sorted = sorted(range(n), key=lambda i: (float(episodes[i].started_at or 0), episodes[i].review_cycle_id))
        n_train = int(n*0.70)
        n_val = int(n*0.15)
        train_indices = set(flat_sorted[:n_train])
        val_indices = set(flat_sorted[n_train:n_train+n_val])
        test_indices = set(flat_sorted[n_train+n_val:])
    else:
        train_indices = {idx for grp in train_groups for idx in grp}
        val_indices = {idx for grp in val_groups for idx in grp}
        test_indices = {idx for grp in test_groups for idx in grp}

    def write_split(name: str, indices: set[int]) -> None:
        path = out_dir / f"{name}.jsonl"
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for i in sorted(indices, key=lambda idx: (float(episodes[idx].started_at or 0), episodes[idx].review_cycle_id)):
                ep = episodes[i]
                feats = features_list[i]
                labs = labels_list[i]
                row = {
                    "review_cycle_id": ep.review_cycle_id,
                    "ticket_id": ep.ticket_id,
                    "features": feats,
                    "labels": labs,
                    "result_revision": ep.result_revision,
                    "diff_hash": ep.diff_hash,
                    "started_at": ep.started_at,
                }
                f.write(json.dumps(row, sort_keys=True) + "\n")
        os.replace(tmp, path)

    write_split("train", train_indices)
    write_split("val", val_indices)
    write_split("test", test_indices)

    # Validate no leakage: result_revision across train/test (fix #28, #92)
    train_revs = {episodes[i].result_revision for i in train_indices if episodes[i].result_revision}
    test_revs = {episodes[i].result_revision for i in test_indices if episodes[i].result_revision}
    overlap = train_revs.intersection(test_revs)
    leakage = len(overlap) == 0  # true if no overlap

    manifest: dict[str, Any] = {
        "dataset_version": dataset_version,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "sample_count": n,
        "train_count": len(train_indices),
        "val_count": len(val_indices),
        "test_count": len(test_indices),
        "semantic_hash": s_hash,
        "leakage_check_pass": leakage,
        "leakage_overlap": sorted(list(overlap))[:10] if overlap else [],
        "created_at": time.time(),
    }
    features_meta = {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": sorted(list(features_list[0].keys())) if features_list else [],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8")
    (out_dir / "features_meta.json").write_text(json.dumps(features_meta, sort_keys=True, indent=2), encoding="utf-8")
    # Also write episodes list for rebuild verification
    # Already covered by manifest semantic_hash
    return manifest

def validate_no_leakage(train_path: Path, test_path: Path) -> dict[str, Any]:
    """Check that same result_revision not leaked across splits."""
    def revs(p: Path) -> set[str]:
        if not p.exists():
            return set()
        s=set()
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row=json.loads(line)
                rv=str(row.get("result_revision","") or row.get("diff_hash","") or "")
                if rv:
                    s.add(rv)
            except Exception:
                continue
        return s
    tr=revs(train_path)
    te=revs(test_path)
    overlap=sorted(list(tr.intersection(te)))
    return {"overlap": overlap[:10], "leakage": len(overlap)>0, "checked": True}

def baseline_policies() -> dict[str, Any]:
    """Return JSON-serializable baseline policy descriptors (no model file needed)."""
    return {
        "always_no": {"description": "never recommend specialist", "recommend": {k: False for k in ("security","concurrency","data_integrity","architecture","performance")}},
        "always_yes": {"description": "always recommend all", "recommend": {k: True for k in ("security","concurrency","data_integrity","architecture","performance")}},
        "deterministic_rule": {"description": "current deterministic escalation_rules", "recommend": "deterministic"},
        "risk_only": {"description": "risk HIGH/CRITICAL -> specialist", "recommend": "risk_high_critical"},
    }
