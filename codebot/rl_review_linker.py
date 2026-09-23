#!/usr/bin/env python3
"""Review Linker — delayed outcome attribution.

Purpose
-------
Connects later discovery findings / reverts to historical review cycles
with attribution strengths CONFIRMED/LIKELY/POSSIBLE/UNRELATED.

Invariants
----------
- Never mutates review_episodes/*.json; appends to review_delayed_outcomes/.
- Attribution strengths: only CONFIRMED/LIKELY affect training labels materially.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from codebot.rl_review_episode import record_delayed_outcome, _episodes_dir

VALID_STRENGTHS = frozenset({"CONFIRMED","LIKELY","POSSIBLE","UNRELATED"})
SEVERITY_WEIGHTS = {"LOW":0.5,"MEDIUM":1.0,"HIGH":3.0,"CRITICAL":10.0}

def _strength_for_finding(finding: dict[str,Any], episode: Any) -> str:
    """Heuristic strength: CONFIRMED if revision matches exactly, else LIKELY/POSSIBLE."""
    finding_rev = str(finding.get("affected_revision") or finding.get("result_revision") or finding.get("revision") or "")
    ep_rev = str(getattr(episode, "result_revision","") or "")
    if finding_rev and ep_rev and finding_rev==ep_rev:
        return "CONFIRMED"
    # If finding mentions diff_hash or ticket_id
    if str(finding.get("ticket_id",""))==str(getattr(episode,"ticket_id","") or ""):
        return "LIKELY"
    # File overlap heuristic
    finding_files = set(finding.get("changed_files",[]) or finding.get("affected_files",[]) or [])
    ep_files = set(getattr(episode,"changed_files",[]) or [])
    if finding_files and ep_files and finding_files.intersection(ep_files):
        return "POSSIBLE"
    return "POSSIBLE"

def link_discovery_to_cycle(
    finding: dict[str,Any],
    state_dir: Path | str,
    *,
    min_strength: str = "POSSIBLE",
    severity: str | None = None,
    category: str | None = None,
) -> dict[str,Any] | None:
    """Find best matching episode for finding and record delayed outcome.

    Returns the delayed outcome path info or None if no match / below threshold.
    """
    from codebot.rl_review_episode import list_episodes
    state_path = Path(state_dir)
    episodes = list_episodes(state_path)
    if not episodes:
        return None
    # Rank by strength
    rank = {"CONFIRMED":3,"LIKELY":2,"POSSIBLE":1,"UNRELATED":0}
    min_rank = rank.get(min_strength.upper(),1)
    candidates: list[tuple[int, Any]] = []
    for ep in episodes:
        s = _strength_for_finding(finding, ep)
        r = rank.get(s,0)
        if r >= min_rank:
            candidates.append((r, ep))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], float(x[1].started_at or 0)), reverse=True)
    best_ep = candidates[0][1]
    strength = _strength_for_finding(finding, best_ep)
    # Delay
    created_at = float(best_ep.completed_at or best_ep.started_at or time.time())
    delay = time.time() - created_at
    # Severity from finding
    sev = severity or str(finding.get("severity","") or finding.get("escaped_defect_severity","") or "MEDIUM").upper()
    cat = category or str(finding.get("category","") or finding.get("domain","") or "bug")
    details = dict(finding)
    # Only CONFIRMED/LIKELY materially affect labels (fix #14/18)
    weight = SEVERITY_WEIGHTS.get(sev.upper(),1.0) if strength in ("CONFIRMED","LIKELY") else 0.5
    rec = record_delayed_outcome(
        best_ep.review_cycle_id, "escaped_defect", state_path,
        attribution=strength, severity=sev, category=cat, delay_s=delay,
        details={"finding": details, "severity_weight": weight, "matched_episode": best_ep.review_cycle_id},
    )
    # Also update episode's escaped_defect markers via separate delayed file; episode itself not mutated
    return {"delayed_path": str(rec), "review_cycle_id": best_ep.review_cycle_id, "strength": strength, "delay_s": delay}
