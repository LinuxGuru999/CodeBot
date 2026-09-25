#!/usr/bin/env python3
"""Review Episode — derived materialized view for one review cycle.

Purpose
-------
Canonical persistent structure for one implementation → review cycle.
Derived from append-only raw events; may be rebuilt atomically from
raw events. Delayed outcomes are separate immutable events referencing
the episode, never in-place mutation (fix #5).

Why
---
Learning needs immutable implementation identity (base/result/diff_hash)
and version-scoped attempt/cycle isolation. This module owns the schema
and rebuild logic.

Invariants
----------
- stdlib-only.
- RAW EVENTS append-only; REVIEW EPISODE derived, rebuildable tmp→replace.
- DELAYED OUTCOMES append-only in review_delayed_outcomes/.
- Cycle identity is review_cycle_id (rc_...), execution is review_execution_id (re_...).
- Schema bug fixed: failure_origin != rework_target, gate_result is PASS|FAIL|BLOCKED.
- Diff snapshot captured at REVIEW entry, not at training time.
- Repo-relative paths preserved as structural metadata, not truncated to basename.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

REVIEW_EPISODE_SCHEMA_VERSION = 1
_EPISODES_DIR = "review_episodes"
_DELAYED_DIR = "review_delayed_outcomes"
_OFFSET_NAME = ".rl_review_offset"

VALID_GATE_RESULTS = frozenset({"PASS", "FAIL", "BLOCKED"})
VALID_REWORK_TARGETS = frozenset({"IMPLEMENT", "PLANNING", "DECOMP", "TRIAGE", "NONE"})
VALID_FAILURE_ORIGINS = frozenset(
    {"DISCOVERY_ERROR","GOAL_ERROR","TRIAGE_ERROR","DECOMPOSITION_ERROR","PLANNING_ERROR","IMPLEMENTATION_ERROR","REVIEW_ERROR","ENVIRONMENT_ERROR","DEPENDENCY_CHANGE","MODEL_FAILURE","UNKNOWN"}
)

def _episodes_dir(state_dir: Path) -> Path:
    return Path(state_dir) / _EPISODES_DIR

def _delayed_dir(state_dir: Path) -> Path:
    return Path(state_dir) / _DELAYED_DIR

def _offset_path(state_dir: Path) -> Path:
    return Path(state_dir) / _OFFSET_NAME

def _safe_id(cid: str) -> str:
    return cid.replace("/", "_").replace("\\", "_")

def _capture_snapshot(ticket: Any, state_dir: Path | None = None) -> dict[str, Any]:
    """Capture diff snapshot at REVIEW entry (fix #8).

    Returns dict with base_revision, result_revision, diff_hash, changed_files,
    lines stats. Falls back to ticket.affected_modules when git unavailable.
    Never raises; never leaks secret values (only paths, not contents).
    """
    try:
        import subprocess
        affected = list(getattr(ticket, "affected_modules", []) or [])
        repo: Path | None = None
        # Walk up from state_dir or cwd to find .git
        candidates = []
        if state_dir is not None:
            candidates.append(Path(state_dir))
            candidates.extend(list(Path(state_dir).parents))
        candidates.append(Path.cwd())
        for cand in candidates:
            try:
                p = cand.resolve() if cand.exists() else cand.absolute()
                for parent in [p] + list(p.parents):
                    if (parent / ".git").exists():
                        repo = parent
                        break
                if repo is not None:
                    break
            except OSError:
                continue
        base_rev = str(getattr(ticket, "base_revision", "") or "")
        result_rev = str(getattr(ticket, "result_revision", "") or "") or str(getattr(ticket, "repo_revision", "") or "") or str(getattr(ticket, "commit_sha", "") or "")
        diff_hash = str(getattr(ticket, "diff_hash", "") or "")
        changed_files: list[str] = list(affected)
        added = 0
        removed = 0
        has_explicit_revision = bool(
            str(getattr(ticket, "base_revision", "") or "").strip()
            or str(getattr(ticket, "result_revision", "") or "").strip()
            or str(getattr(ticket, "repo_revision", "") or "").strip()
            or str(getattr(ticket, "commit_sha", "") or "").strip()
            or str(getattr(ticket, "diff_hash", "") or "").strip()
        )
        if repo is not None and has_explicit_revision:
            try:
                # Try to get HEAD and base
                head_proc = subprocess.run(["git","-C",str(repo),"rev-parse","HEAD"], capture_output=True, text=True, timeout=5)
                head = head_proc.stdout.strip() if head_proc.returncode==0 else ""
                base_proc = subprocess.run(["git","-C",str(repo),"rev-parse","HEAD~1"], capture_output=True, text=True, timeout=5)
                base = base_proc.stdout.strip() if base_proc.returncode==0 else ""
                if head:
                    result_rev = result_rev or head
                if base:
                    base_rev = base_rev or base
                # Changed files via git diff --name-only if both exist
                if base_rev and result_rev and base_rev != result_rev:
                    try:
                        diff_proc = subprocess.run(["git","-C",str(repo),"diff","--name-only", f"{base_rev}..{result_rev}"], capture_output=True, text=True, timeout=5)
                        if diff_proc.returncode==0 and diff_proc.stdout.strip():
                            files = [ln.strip() for ln in diff_proc.stdout.splitlines() if ln.strip()]
                            # Preserve normalized repo-relative paths (fix #16)
                            changed_files = files
                    except Exception:
                        pass
                    try:
                        stat_proc = subprocess.run(["git","-C",str(repo),"diff","--numstat", f"{base_rev}..{result_rev}"], capture_output=True, text=True, timeout=5)
                        if stat_proc.returncode==0:
                            for ln in stat_proc.stdout.splitlines():
                                parts = ln.split()
                                if len(parts)>=2:
                                    try:
                                        a = int(parts[0]) if parts[0]!="-" else 0
                                        r = int(parts[1]) if parts[1]!="-" else 0
                                        added += a
                                        removed += r
                                    except ValueError:
                                        pass
                    except Exception:
                        pass
            except Exception:
                pass
        # diff_hash from sorted changed_files if not already set
        if not diff_hash:
            h = hashlib.sha256("\n".join(sorted(changed_files)).encode("utf-8")).hexdigest()[:16]
            diff_hash = h
        else:
            # ensure hash is string
            diff_hash = str(diff_hash)
        return {
            "base_revision": base_rev,
            "result_revision": result_rev,
            "diff_hash": diff_hash,
            "changed_files": changed_files,
            "changed_lines_added": added,
            "changed_lines_removed": removed,
        }
    except Exception:
        affected = list(getattr(ticket, "affected_modules", []) or [])
        h = hashlib.sha256("\n".join(sorted(affected)).encode("utf-8")).hexdigest()[:16]
        return {
            "base_revision": str(getattr(ticket, "base_revision", "") or ""),
            "result_revision": str(getattr(ticket, "repo_revision", "") or getattr(ticket, "commit_sha", "") or ""),
            "diff_hash": h,
            "changed_files": affected,
            "changed_lines_added": 0,
            "changed_lines_removed": 0,
        }

@dataclass
class ReviewEpisode:
    review_cycle_id: str = ""
    review_execution_ids: list[str] = field(default_factory=list)
    ticket_id: str = ""
    implementation_attempt_id: str = ""
    implementation_attempt_number: int = 0
    base_revision: str = ""
    result_revision: str = ""
    diff_hash: str = ""
    started_at: float = 0.0
    completed_at: float = 0.0
    ticket_class: str = ""
    risk_class: str = ""
    severity: str = ""
    discovery_role: str = ""
    goal_disposition: str = ""
    changed_files: list[str] = field(default_factory=list)
    changed_file_count: int = 0
    changed_lines_added: int = 0
    changed_lines_removed: int = 0
    affected_modules: list[str] = field(default_factory=list)
    language_mix: list[str] = field(default_factory=list)
    change_categories: list[str] = field(default_factory=list)
    primary_model: str = ""
    primary_decision: str = ""
    primary_escalation: str = ""
    primary_duration_s: float = 0.0
    primary_tokens: int = 0
    primary_cost: float = 0.0
    deterministic_specialists: list[str] = field(default_factory=list)
    primary_requested_specialists: list[str] = field(default_factory=list)
    specialists_run: list[str] = field(default_factory=list)
    specialist_results: dict[str, Any] = field(default_factory=dict)
    completion_result: str = ""
    failure_origin: str = ""
    rework_target: str = ""
    gate_result: str = ""
    escaped_defect: bool = False
    escaped_defect_category: str | None = None
    escaped_defect_severity: str | None = None
    escaped_defect_delay_s: float | None = None
    escaped_defect_attribution: str | None = None
    later_reopened: bool = False
    later_regression: bool = False
    later_revert: bool = False
    human_override: Any | None = None
    features_version: str = "fv1"
    policy_version: str = ""
    schema_version: int = REVIEW_EPISODE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewEpisode":
        if not isinstance(data, dict):
            return cls()
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid}
        # coerce gate_result / rework_target to valid values (fix #7)
        if "gate_result" in filtered:
            gr = str(filtered["gate_result"]).upper()
            if gr not in VALID_GATE_RESULTS:
                # Map legacy REWORK -> FAIL, others -> BLOCKED/PASS heuristic
                if gr == "REWORK":
                    gr = "FAIL"
                elif gr in ("APPROVE","COMPLETE","PASS"):
                    gr = "PASS"
                else:
                    gr = "FAIL"
            filtered["gate_result"] = gr
        if "rework_target" in filtered:
            rt = str(filtered["rework_target"]).upper()
            if rt not in VALID_REWORK_TARGETS:
                filtered["rework_target"] = "NONE" if not rt else ("IMPLEMENT" if rt=="IMPLEMENTATION_ERROR" else rt)
                if filtered["rework_target"] not in VALID_REWORK_TARGETS:
                    filtered["rework_target"] = "IMPLEMENT"
        try:
            return cls(**filtered)
        except Exception:
            return cls(review_cycle_id=str(data.get("review_cycle_id","")))

    def semantic_hash(self) -> str:
        """Canonical semantic hash over normalized fields (fix #17).

        Excludes volatile timestamps like completed_at creation time of file
        itself; includes episode IDs and normalized content.
        """
        # Normalize: sort lists, lower-case where appropriate, exclude file mtime
        norm = {
            "review_cycle_id": self.review_cycle_id,
            "ticket_id": self.ticket_id,
            "implementation_attempt_id": self.implementation_attempt_id,
            "base_revision": self.base_revision,
            "result_revision": self.result_revision,
            "diff_hash": self.diff_hash,
            "ticket_class": self.ticket_class,
            "risk_class": self.risk_class,
            "severity": self.severity,
            "changed_files": sorted(self.changed_files),
            "affected_modules": sorted(self.affected_modules),
            "primary_decision": self.primary_decision,
            "completion_result": self.completion_result,
            "failure_origin": self.failure_origin,
            "rework_target": self.rework_target,
            "gate_result": self.gate_result,
            "specialists_run": sorted(self.specialists_run),
            "schema_version": self.schema_version,
            "features_version": self.features_version,
        }
        j = json.dumps(norm, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(j.encode("utf-8")).hexdigest()[:16]

def episode_path(state_dir: Path | str, review_cycle_id: str) -> Path:
    return _episodes_dir(Path(state_dir)) / f"{_safe_id(review_cycle_id)}.json"

def save_episode(state_dir: Path | str, episode: ReviewEpisode) -> Path:
    d = _episodes_dir(Path(state_dir))
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_safe_id(episode.review_cycle_id)}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(episode.to_dict(), sort_keys=True, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path

def load_episode(state_dir: Path | str, review_cycle_id: str) -> ReviewEpisode | None:
    path = episode_path(state_dir, review_cycle_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return ReviewEpisode.from_dict(data)
    except (json.JSONDecodeError, OSError):
        return None

def list_episodes(state_dir: Path | str) -> list[ReviewEpisode]:
    d = _episodes_dir(Path(state_dir))
    if not d.exists():
        return []
    out: list[ReviewEpisode] = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out.append(ReviewEpisode.from_dict(data))
        except Exception:
            continue
    return out

def build_episode(
    ticket: Any,
    *,
    review_cycle_id: str | None = None,
    state_dir: Path | str | None = None,
    specialist_results: dict[str, Any] | None = None,
    completion_result: str = "",
    gate_result: str = "",
    failure_origin: str = "",
    rework_target: str = "",
    started_at: float | None = None,
    completed_at: float | None = None,
) -> ReviewEpisode:
    """Build a ReviewEpisode from a ticket snapshot plus current verdicts.

    Diff snapshot is captured from ticket's current affected_modules/revisions
    and git if available. Caller should pass already-captured snapshot if
    available to avoid re-deriving.
    """
    cid = review_cycle_id or str(getattr(ticket, "current_review_cycle_id", "") or f"rc_{uuid.uuid4().hex[:8]}")
    exec_id = str(getattr(ticket, "current_review_execution_id", "") or "")
    impl_attempt = str(getattr(ticket, "current_attempt_id", "") or "")
    attempts = int(getattr(ticket, "attempts", 0) or 0)
    snap = _capture_snapshot(ticket, Path(state_dir) if state_dir is not None else None)
    changed_files = list(snap.get("changed_files", []) or list(getattr(ticket, "affected_modules", []) or []))
    # Preserve repo-relative paths (fix #16)
    changed_files = [str(p) for p in changed_files if isinstance(p, str) and p.strip()]
    # Infer language_mix from extensions
    lang_mix: list[str] = []
    for f in changed_files:
        ext = Path(f).suffix.lower()
        if ext == ".py" and "python" not in lang_mix:
            lang_mix.append("python")
        elif ext in (".ts",".tsx",".js",".jsx") and "typescript" not in lang_mix:
            lang_mix.append("typescript")
        elif ext in (".go",) and "go" not in lang_mix:
            lang_mix.append("go")
        elif ext in (".rs",) and "rust" not in lang_mix:
            lang_mix.append("rust")
    # Change categories from file paths
    cats: list[str] = []
    for f in changed_files:
        low = f.lower()
        if "auth" in low and "auth" not in cats:
            cats.append("auth")
        if "scheduler" in low and "scheduler" not in cats:
            cats.append("scheduler")
        if "migration" in low and "migration" not in cats:
            cats.append("migration")
        if "claim" in low and "claim" not in cats:
            cats.append("claim")
        if "subprocess" in low or "shell" in low and "subprocess" not in cats:
            cats.append("subprocess")
    tc = getattr(ticket, "ticket_class", None)
    tc_val = tc.value if hasattr(tc, "value") else str(tc) if tc else ""
    risk = getattr(ticket, "risk", None)
    risk_val = risk.value if hasattr(risk, "value") else str(risk) if risk else ""
    sev = getattr(getattr(ticket, "severity", None), "value", "")
    if not sev:
        sev = str(getattr(ticket, "severity", "") or "")
    # Normalize gate_result / rework_target per fix #7
    gr = str(gate_result or "").upper()
    if gr not in VALID_GATE_RESULTS:
        if gr == "REWORK":
            gr = "FAIL"
        elif gr:
            gr = gr if gr in VALID_GATE_RESULTS else "FAIL"
        else:
            gr = "PASS" if str(completion_result).upper() in ("COMPLETE","RESOLVED") else ("FAIL" if str(completion_result).upper()=="REWORK" else "")
            if not gr:
                gr = ""
    rt = str(rework_target or "").upper()
    if rt not in VALID_REWORK_TARGETS:
        if rt == "IMPLEMENTATION_ERROR":
            rt = "IMPLEMENT"
        elif rt:
            rt = rt if rt in VALID_REWORK_TARGETS else "IMPLEMENT"
        else:
            # infer from failure_origin
            fo_up = str(failure_origin or "").upper()
            if fo_up == "PLANNING_ERROR":
                rt = "PLANNING"
            elif fo_up == "DECOMPOSITION_ERROR":
                rt = "DECOMP"
            elif fo_up:
                rt = "IMPLEMENT"
            else:
                rt = "NONE" if not completion_result or str(completion_result).upper() in ("COMPLETE","RESOLVED") else "IMPLEMENT"
    fo = str(failure_origin or "").upper()
    if fo and fo not in VALID_FAILURE_ORIGINS:
        fo = "UNKNOWN"
    # completion_result is lifecycle outcome (COMPLETE/REWORK/etc)
    cr = str(completion_result or "").upper()
    # specialists_run derived from specialist_results keys if provided
    specs = list((specialist_results or {}).keys()) if specialist_results else []

    ep = ReviewEpisode(
        review_cycle_id=cid,
        review_execution_ids=[exec_id] if exec_id else [],
        ticket_id=str(getattr(ticket, "id", "") or getattr(ticket, "ticket_id", "") or ""),
        implementation_attempt_id=impl_attempt,
        implementation_attempt_number=attempts,
        base_revision=str(snap.get("base_revision","") or ""),
        result_revision=str(snap.get("result_revision","") or ""),
        diff_hash=str(snap.get("diff_hash","") or ""),
        started_at=float(started_at or getattr(ticket, "updated_at", time.time()) or time.time()),
        completed_at=float(completed_at or time.time()),
        ticket_class=str(tc_val),
        risk_class=str(risk_val),
        severity=str(sev),
        discovery_role=str(getattr(ticket, "discovery_category", "") or ""),
        goal_disposition=str(getattr(ticket, "goal_disposition", "") or ""),
        changed_files=changed_files,
        changed_file_count=len(changed_files),
        changed_lines_added=int(snap.get("changed_lines_added",0) or 0),
        changed_lines_removed=int(snap.get("changed_lines_removed",0) or 0),
        affected_modules=list(getattr(ticket, "affected_modules", []) or []),
        language_mix=lang_mix,
        change_categories=cats,
        specialists_run=specs,
        specialist_results=dict(specialist_results or {}),
        completion_result=cr,
        failure_origin=fo,
        rework_target=rt,
        gate_result=gr,
    )
    return ep

def rebuild_all_episodes(state_dir: Path | str, incremental: bool = False) -> int:
    """Rebuild derived review_episodes from raw events + ticket store.

    Current minimal implementation: scans TicketStore for tickets with a
    review_cycle_id and builds episodes. If no TicketStore, scans rl_events
    for REVIEW_STARTED / REVIEW_APPROVED / REVIEW_REWORK groupings.

    For verification, produces semantic equivalence not byte identity.
    """
    state_path = Path(state_dir)
    episodes_dir = _episodes_dir(state_path)
    episodes_dir.mkdir(parents=True, exist_ok=True)
    # Try ticket-store path first
    tickets_path = state_path / "tickets.json"
    count = 0
    seen: set[str] = set()
    if tickets_path.exists():
        try:
            from codebot.ticket_dispatcher import get_ticket_store
            from codebot.ticket_engine import ReadOnlyTicketView
            s = get_ticket_store(state_path)
            view = ReadOnlyTicketView(s) if s is not None else None
            tickets = view.list_tickets(include_workers=False) if view is not None else []
            for t in tickets:
                cid = str(getattr(t, "current_review_cycle_id", "") or "")
                if not cid:
                    continue
                if cid in seen:
                    continue
                seen.add(cid)
                try:
                    st_val = t.state.value if hasattr(t.state, "value") else str(t.state)
                except Exception:
                    st_val = str(getattr(t, "state",""))
                cr_map = {"COMPLETE":"COMPLETE","REWORK":"REWORK","RESOLVED":"RESOLVED","CANCELLED":"CANCELLED","DEFERRED":"DEFERRED","SUPERSEDED":"SUPERSEDED","REVIEW":"REVIEW"}
                cr = cr_map.get(str(st_val).upper(), str(st_val).upper())
                spec_results: dict[str, Any] = {}
                specialists_run: list[str] = []
                try:
                    from codebot.review_store import load_ticket_verdicts
                    verdicts = load_ticket_verdicts(state_path, t.id)
                    for v in verdicts:
                        if int(v.get("implementation_attempt_id",0) or 0) != int(getattr(t,"attempts",0) or 0):
                            pass
                        reviewer = str(v.get("reviewer",""))
                        base = reviewer.split("-",1)[0]
                        if base in ("reviewer", "primary_reviewer"):
                            continue
                        from codebot.escalation_rules import SPECIALIST_ROLES
                        type_for = None
                        for stype, rname in SPECIALIST_ROLES.items():
                            if rname == base:
                                type_for = stype
                                break
                        if type_for:
                            specialists_run.append(type_for)
                            spec_results[type_for] = {
                                "decision": str(v.get("verdict", v.get("decision",""))).upper(),
                                "blocking": bool(v.get("blocking") or any(f.get("severity","").upper() in ("HIGH","CRITICAL") for f in v.get("findings",[]) or [])),
                            }
                except Exception:
                    pass
                ep = build_episode(
                    t,
                    review_cycle_id=cid,
                    state_dir=state_path,
                    specialist_results=spec_results,
                    completion_result=cr,
                    gate_result="PASS" if cr in ("COMPLETE","RESOLVED") else ("FAIL" if cr=="REWORK" else ""),
                    started_at=float(getattr(t,"updated_at",time.time())),
                    completed_at=float(getattr(t,"updated_at",time.time())),
                )
                if specialists_run:
                    ep.specialists_run = sorted(set(specialists_run))
                    ep.specialist_results = spec_results
                save_episode(state_path, ep)
                count += 1
        except Exception:
            pass
    if count == 0:
        # Fallback: scan events for cycles
        try:
            from codebot.rl_event_log import read_events
            events = read_events(state_path)
            # Group by review_cycle_id in context
            cycles: dict[str, list[dict[str,Any]]] = {}
            for ev in events:
                ctx = ev.get("context",{}) or {}
                rc = str(ctx.get("review_cycle_id","") or "")
                if rc:
                    cycles.setdefault(rc, []).append(ev)
            for rc, evs in cycles.items():
                if rc in seen:
                    continue
                seen.add(rc)
                # Find ticket id
                tid = str(evs[0].get("ticket_id","") or "")
                ticket_obj = None
                if tickets_path.exists():
                    try:
                        from codebot.ticket_dispatcher import get_ticket_store
                        s2 = get_ticket_store(state_path)
                        if s2 is not None:
                            ticket_obj = s2.get(tid)
                    except Exception:
                        pass
                if ticket_obj is None:
                    # synth minimal ticket
                    from codebot.ticket_engine import Ticket, TicketClass, TicketState, Severity, RiskLevel
                    ticket_obj = Ticket(
                        id=tid, title=f"synthetic {tid}", ticket_class=TicketClass.BUG, severity=Severity.MEDIUM,
                        state=TicketState.REVIEW, source="synthetic", evidence="ev", problem_statement="p",
                        desired_state="ds", acceptance_criteria=["ac"], affected_modules=[], dependencies=[],
                        risk=RiskLevel.LOW, blast_radius="low", security_impact="none", migration_impact="none",
                        required_reviewers=[], required_tests=[], documentation_requirements=[], rollback_strategy="revert",
                        estimated_cost_tokens=100, created_at=time.time(), updated_at=time.time(),
                    )
                    ticket_obj = ticket_obj.__class__(**{**ticket_obj.__dict__, "current_review_cycle_id": rc})
                ep = build_episode(ticket_obj, review_cycle_id=rc, state_dir=state_path)
                save_episode(state_path, ep)
                count += 1
        except Exception:
            pass
    return count

def record_delayed_outcome(
    review_cycle_id: str,
    outcome_type: str,
    state_dir: Path | str,
    *,
    attribution: str = "POSSIBLE",
    severity: str | None = None,
    category: str | None = None,
    delay_s: float | None = None,
    details: dict[str, Any] | None = None,
) -> Path:
    """Append a delayed outcome referencing a review_cycle_id.

    Never mutates the episode file; creates review_delayed_outcomes/{cycle}__{event_id}.json
    """
    ddir = _delayed_dir(Path(state_dir))
    ddir.mkdir(parents=True, exist_ok=True)
    eid = f"{_safe_id(review_cycle_id)}__{uuid.uuid4().hex[:8]}"
    path = ddir / f"{eid}.json"
    payload: dict[str, Any] = {
        "review_cycle_id": review_cycle_id,
        "outcome_type": outcome_type,
        "attribution": attribution,
        "severity": severity,
        "category": category,
        "delay_s": delay_s,
        "details": details or {},
        "created_at": time.time(),
        "event_id": eid,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path

def _read_offset(state_dir: Path) -> int:
    p = _offset_path(state_dir)
    if not p.exists():
        return 0
    try:
        return int(p.read_text(encoding="utf-8").strip() or "0")
    except (ValueError, OSError):
        return 0

def _write_offset(state_dir: Path, offset: int) -> None:
    p = _offset_path(state_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(str(int(offset)), encoding="utf-8")
    os.replace(tmp, p)
