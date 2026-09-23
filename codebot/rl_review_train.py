#!/usr/bin/env python3
"""Review Train — offline deterministic pipeline (not in scheduler).

Purpose
-------
raw episodes → validate → join delayed → extract → snapshot → train → validate
→ test → calibrate → versioned JSON policy (weights.json, no pickle).

Invariants
----------
- Never imported in scheduler_v2. Lint forbids import.
- stdlib-only fallback path if sklearn unavailable; otherwise uses sklearn.
- Args: --dataset <version> --algorithm logreg --state-dir .codebot/state
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from codebot.rl_review_features import FEATURE_SCHEMA_VERSION
from codebot.rl_review_policy import _policies_dir

def _train_logreg_sklearn(train_rows: list[dict[str,Any]], val_rows: list[dict[str,Any]]) -> dict[str,Any]:
    """Try sklearn; if unavailable, return pure-python per-specialist baseline."""
    specialists = ("security","concurrency","data_integrity","architecture","performance")
    # Check sklearn availability without hard dependency at runtime
    try:
        import importlib
        sklearn = importlib.import_module("sklearn.linear_model")
        LogisticRegression = getattr(sklearn, "LogisticRegression")
        available = True
    except Exception:
        available = False
    if not available:
        # Fallback: produce intercept-only models with conservative thresholds
        weights: dict[str,Any] = {"feature_schema_version": FEATURE_SCHEMA_VERSION, "algorithm": "baseline_intercept", "specialists": {}}
        for s in specialists:
            weights["specialists"][s] = {"intercept": -1.0, "coefficients": {}, "threshold": 0.30 if s in ("security","data_integrity","concurrency") else 0.50}
        weights["thresholds"] = {s: {"default": 0.30 if s in ("security","data_integrity","concurrency") else 0.50} for s in specialists}
        return weights
    # Use sklearn if available: one model per specialist
    # Feature set: numeric features only
    numeric_feats: list[str] = []
    for row in train_rows[:20]:
        for k,v in row.get("features",{}).items():
            if k.startswith("_"):
                continue
            if isinstance(v, (int,float,bool)):
                if k not in numeric_feats:
                    numeric_feats.append(k)
            elif isinstance(v, (list,)):
                if k not in numeric_feats:
                    numeric_feats.append(f"{k}#len")
    specialists_weights: dict[str,Any] = {}
    thresholds: dict[str,Any] = {}
    for spec in specialists:
        # Build X,y
        X_train: list[list[float]] = []
        y_train: list[int] = []
        for row in train_rows:
            feats = row.get("features",{})
            labs = row.get("labels",{})
            y = int(labs.get(f"{spec}_blocking_found", labs.get(f"{spec}_escaped_same_domain", 0)) or 0)
            # Also include nonblocking as positive if blocking rare
            if y==0 and int(labs.get(f"{spec}_nonblocking_found",0)):
                y=1
            x = []
            for fn in numeric_feats:
                if fn.endswith("#len"):
                    base=fn[:-4]
                    v=feats.get(base,[])
                    x.append(float(len(v) if isinstance(v,list) else 0))
                else:
                    v=feats.get(fn,0)
                    if isinstance(v,bool):
                        x.append(1.0 if v else 0.0)
                    elif isinstance(v,(int,float)):
                        x.append(float(v))
                    else:
                        x.append(0.0)
            X_train.append(x)
            y_train.append(y)
        # Handle class imbalance: require at least 2 positives
        pos = sum(y_train)
        if pos < 5 or pos == len(y_train):
            specialists_weights[spec] = {"intercept": -1.2, "coefficients": {}, "threshold": 0.30 if spec in ("security","data_integrity","concurrency") else 0.50}
            thresholds[spec] = {"default": 0.30 if spec in ("security","data_integrity","concurrency") else 0.50}
            continue
        try:
            clf = LogisticRegression(max_iter=500, class_weight="balanced", solver="lbfgs")
            clf.fit(X_train, y_train)
            coeffs = {fn: float(c) for fn,c in zip(numeric_feats, clf.coef_[0])}
            # Drop near-zero
            coeffs = {k:v for k,v in coeffs.items() if abs(v) > 1e-4}
            intercept = float(clf.intercept_[0])
            # Choose threshold on val to favor recall for high-consequence
            # Simple: compute scores on val and pick threshold achieving high recall
            if val_rows:
                X_val: list[list[float]] = []
                y_val: list[int] = []
                for row in val_rows:
                    feats=row.get("features",{})
                    labs=row.get("labels",{})
                    y=int(labs.get(f"{spec}_blocking_found",0) or 0)
                    if y==0 and int(labs.get(f"{spec}_escaped_same_domain",0)):
                        y=1
                    x=[]
                    for fn in numeric_feats:
                        if fn.endswith("#len"):
                            base=fn[:-4]
                            v=feats.get(base,[])
                            x.append(float(len(v) if isinstance(v,list) else 0))
                        else:
                            v=feats.get(fn,0)
                            x.append(float(v) if isinstance(v,(int,float,bool)) else 0.0)
                    X_val.append(x); y_val.append(y)
                import math
                scores=[]
                for x in X_val:
                    logit=intercept+sum(coeffs.get(fn,0)*xv for fn,xv in zip(numeric_feats,x))
                    scores.append(1/(1+math.exp(-logit)))
                # Target recall 0.85 for high-consequence, 0.70 else
                target_recall=0.85 if spec in ("security","data_integrity","concurrency") else 0.70
                # Try thresholds 0.2..0.6
                best=0.30 if spec in ("security","data_integrity","concurrency") else 0.50
                for thr in [0.2,0.25,0.30,0.35,0.40,0.45,0.50,0.55,0.60]:
                    tp=sum(1 for s,y in zip(scores,y_val) if s>=thr and y==1)
                    fn=sum(1 for s,y in zip(scores,y_val) if s<thr and y==1)
                    recall=tp/max(1,tp+fn)
                    if recall>=target_recall:
                        best=thr
                        break
                thr=best
            else:
                thr=0.30 if spec in ("security","data_integrity","concurrency") else 0.50
            specialists_weights[spec] = {"intercept": intercept, "coefficients": coeffs, "threshold": thr}
            thresholds[spec] = {"default": thr}
        except Exception:
            specialists_weights[spec] = {"intercept": -1.0, "coefficients": {}, "threshold": 0.30}
            thresholds[spec] = {"default": 0.30}
    return {"feature_schema_version": FEATURE_SCHEMA_VERSION, "algorithm": "logreg", "specialists": specialists_weights, "thresholds": thresholds, "_numeric_feats": numeric_feats}

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Train review policy from dataset snapshot")
    ap.add_argument("--dataset", required=True, help="dataset version e.g. review-dataset-0001")
    ap.add_argument("--state-dir", default=".codebot/state", help="state dir")
    ap.add_argument("--algorithm", default="logreg", choices=["logreg"], help="algorithm")
    ap.add_argument("--output", default="", help="output policy version e.g. review-policy-0001 (auto if empty)")
    args = ap.parse_args(argv)

    state_dir = Path(args.state_dir)
    dataset_dir = state_dir / "review_datasets" / args.dataset
    if not dataset_dir.exists():
        print(f"dataset not found: {dataset_dir}", flush=True)
        return 2
    train_path = dataset_dir / "train.jsonl"
    val_path = dataset_dir / "val.jsonl"
    test_path = dataset_dir / "test.jsonl"
    manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8")) if (dataset_dir / "manifest.json").exists() else {}

    def load_rows(p: Path) -> list[dict[str,Any]]:
        if not p.exists():
            return []
        rows=[]
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    train_rows=load_rows(train_path)
    val_rows=load_rows(val_path)
    test_rows=load_rows(test_path)
    if not train_rows:
        print("no train rows; aborting", flush=True)
        return 2

    weights = _train_logreg_sklearn(train_rows, val_rows)

    # Auto version
    out_version = args.output
    if not out_version:
        base=_policies_dir(state_dir)
        existing=list(base.glob("review-policy-*")) if base.exists() else []
        nums=[]
        for p in existing:
            try:
                nums.append(int(p.name.replace("review-policy-","")))
            except ValueError:
                pass
        nxt=(max(nums)+1) if nums else 1
        out_version=f"review-policy-{nxt:04d}"

    out_dir = _policies_dir(state_dir) / out_version
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact: dict[str,Any] = {
        "policy_version": out_version,
        "dataset_version": args.dataset,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "algorithm": weights.get("algorithm","logreg"),
        "trained_at": time.time(),
        "sample_count": len(train_rows)+len(val_rows)+len(test_rows),
        "train_count": len(train_rows),
        "val_count": len(val_rows),
        "test_count": len(test_rows),
        "dataset_manifest": manifest,
    }
    (out_dir / "artifact.json").write_text(json.dumps(artifact, sort_keys=True, indent=2), encoding="utf-8")
    (out_dir / "weights.json").write_text(json.dumps(weights, sort_keys=True, indent=2), encoding="utf-8")
    print(f"wrote {out_dir} ({artifact['sample_count']} samples)", flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
