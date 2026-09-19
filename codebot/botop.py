#!/usr/bin/env python3
"""botop — Standalone bot operations CLI for CodeBot.

Purpose
-------
Provides direct bot management operations (status, logs, restart, pause,
resume, drain) without requiring the full orchestrator loop. Equivalent
to Monitor's botop.py but portable across any CodeBot-managed project.

Live mode
---------
`botop live` renders a live dashboard: agents, tickets, throughput,
claims/leases, budget, diagnostics. `botop term` drops into an
interactive terminal for iterative inspection.

Why
---
Operators need quick bot management without starting the full scheduler.
This CLI reads state files directly and sends signals to running processes.

Invariants
----------
- stdlib-only
- Read-only operations by default (status, logs)
- Mutating operations (restart, pause, drain) require explicit flags
- Never modifies source code
- Fail-open per source: one corrupt file skips that source, not the bot
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _find_state_dir(project_root: Path) -> Path:
    candidates = [
        project_root / ".codebot" / "state",
        project_root / "state",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def _find_logs_dir(project_root: Path) -> Path:
    candidates = [
        project_root / ".codebot" / "logs",
        project_root / "logs",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def _find_project_name(project_root: Path) -> str:
    for cand in [project_root / ".codebot" / "project.yaml", project_root / "project.yaml"]:
        if cand.exists():
            try:
                txt = cand.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r"^\s*name:\s*\"?([^\"\n]+)\"?", txt, re.MULTILINE)
                if m:
                    return m.group(1).strip().strip('"').strip("'")
            except Exception:
                pass
    return project_root.name


# ---------------------------------------------------------------------------
# ANSI / color
# ---------------------------------------------------------------------------

_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "gray": "\033[90m",
}

# Global flag set by main() from --no-color argument
_GLOBAL_NO_COLOR = False

def _supports_color(no_color: bool | None = None) -> bool:
    # Explicit argument takes precedence, then global flag, then env var, then TTY
    if no_color is True:
        return False
    if no_color is False:
        # Caller explicitly wants color, skip global/env checks
        return sys.stdout.isatty()
    # no_color is None: check global, env, tty
    if _GLOBAL_NO_COLOR:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()

def _c(text: str, color: str, enabled: bool) -> str:
    if not enabled or color not in _ANSI:
        return text
    return f"{_ANSI[color]}{text}{_ANSI['reset']}"

def _strip_ansi(s: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", s)


def _visible_width(s: str) -> int:
    """Return the visible character width of *s*, ignoring ANSI escape codes."""
    return len(_strip_ansi(s))


def _ansi_pad(s: str, width: int, align: str = "left") -> str:
    """Pad *s* to *width* visible characters, ANSI-aware.

    *align* is ``'left'`` (default) or ``'right'``.
    When the visible text is already wider than *width* the string is
    returned unchanged (no truncation).
    """
    vw = _visible_width(s)
    if vw >= width:
        return s
    pad = " " * (width - vw)
    return s + pad if align == "left" else pad + s


# ---------------------------------------------------------------------------
# Safe readers
# ---------------------------------------------------------------------------

def _read_text_safe(p: Path, limit: int = 1_000_000) -> str | None:
    try:
        if not p.exists():
            return None
        # bounded read
        txt = p.read_text(encoding="utf-8", errors="ignore")
        if len(txt) > limit:
            txt = txt[-limit:]
        return txt
    except Exception:
        return None

def _read_json_safe(p: Path) -> Any | None:
    try:
        if not p.exists():
            return None
        raw = p.read_text(encoding="utf-8", errors="ignore")
        if not raw.strip():
            return None
        return json.loads(raw)
    except Exception:
        return None

def _parse_checkpoint_text(raw: str) -> dict | None:
    raw = raw.strip()
    if not raw:
        return None
    # try json first
    try:
        j = json.loads(raw)
        if isinstance(j, dict):
            return j
    except Exception:
        pass
    # json with trailing garbage: try to find first {...} block
    # if single-quoted python dict, ast
    try:
        # strip trailing commas/extra braces like "},"
        # attempt ast literal eval on raw that looks like python dict
        # replace json-style outer? ast handles both single and double quotes if valid python
        v = ast.literal_eval(raw.strip().strip("\x00"))
        if isinstance(v, dict):
            return v
    except Exception:
        pass
    # try to extract json object via brace balancing and ast fallback
    # fallback: try to find {...} substring
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            sub = raw[start:end+1]
            try:
                j2 = json.loads(sub)
                if isinstance(j2, dict):
                    return j2
            except Exception:
                pass
            try:
                v2 = ast.literal_eval(sub)
                if isinstance(v2, dict):
                    return v2
            except Exception:
                pass
    except Exception:
        pass
    return None

def _read_checkpoint_file(p: Path) -> dict | None:
    txt = _read_text_safe(p, limit=20_000)
    if txt is None:
        return None
    return _parse_checkpoint_text(txt)

def _read_heartbeat_value(p: Path) -> float | None:
    try:
        if not p.exists():
            return None
        txt = p.read_text(encoding="utf-8", errors="ignore").strip().split()[0]
        return float(txt)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Agent collection
# ---------------------------------------------------------------------------

def _find_agent_pid(agent_name: str) -> int | None:
    # try pgrep first
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"api_runner.*{agent_name}"],
            capture_output=True, text=True, timeout=3,
        )
        if result.stdout.strip():
            return int(result.stdout.strip().split()[0])
    except Exception:
        pass
    # fallback ps scan
    try:
        out = subprocess.check_output(["ps", "-eo", "pid=,args="], text=True, timeout=5)
        needle1 = f"api_runner.py {agent_name} "
        needle2 = f"api_runner {agent_name}"
        needle3 = f"codebot.api_runner {agent_name}"
        for line in out.splitlines():
            s = line.strip()
            if not s:
                continue
            parts = s.split(None, 1)
            if len(parts) < 2:
                continue
            pid_s, args = parts
            if needle1 in args or needle2 in args or needle3 in args or f" {agent_name} " in args and "api_runner" in args:
                try:
                    return int(pid_s)
                except ValueError:
                    continue
    except Exception:
        pass
    return None

def _find_all_api_pids() -> dict[str, int]:
    """Return {agent: pid} via ps scan for all api_runner processes."""
    out: dict[str, int] = {}
    try:
        ps = subprocess.check_output(["ps", "-eo", "pid=,args="], text=True, timeout=5)
        for line in ps.splitlines():
            m = re.search(r"api_runner(?:\.py)?\s+([A-Za-z0-9_\-]+)", line)
            if m:
                ag = m.group(1)
                pid_s = line.strip().split(None, 1)[0]
                try:
                    out[ag] = int(pid_s)
                except ValueError:
                    pass
    except Exception:
        pass
    return out


def _collect_agents(project_root: Path) -> list[dict[str, Any]]:
    state_dir = _find_state_dir(project_root)
    logs_dir = _find_logs_dir(project_root)
    now = time.time()
    # discover via heartbeat files + state files + status files
    names: set[str] = set()
    for p in state_dir.glob("*.heartbeat"):
        names.add(p.stem)
    for p in state_dir.glob("*.status.json"):
        names.add(p.name.replace(".status.json", ""))
    for p in state_dir.glob("*.state.json"):
        names.add(p.name.replace(".state.json", ""))
    for p in state_dir.glob("*.paused"):
        names.add(p.name.replace(".paused", ""))
    # also include from botop discovery if none
    if not names:
        # fallback list known roles? keep empty to show "No agents found"
        pass

    pid_map = _find_all_api_pids()
    agents: list[dict[str, Any]] = []
    for name in sorted(names):
        hb_path = state_dir / f"{name}.heartbeat"
        status_path = state_dir / f"{name}.status.json"
        state_path = state_dir / f"{name}.state.json"
        ckpt_path = state_dir / f"{name}.checkpoint.json"
        scratch_path = state_dir / f"{name}.scratchpad.json"
        paused_path = state_dir / f"{name}.paused"
        log_path = logs_dir / f"{name}.log"
        tasklog_path = logs_dir / f"{name}.tasklog"
        mission_path = logs_dir / f"{name}.mission"

        hb_ts = _read_heartbeat_value(hb_path)
        hb_age = (now - hb_ts) if hb_ts else None
        if hb_age is not None and hb_age < 0:
            hb_age = 0

        status = _read_json_safe(status_path) if status_path.exists() else None
        if not isinstance(status, dict):
            status = {}
        state = _read_json_safe(state_path) if state_path.exists() else None
        if not isinstance(state, dict):
            state = {}
        ckpt = _read_checkpoint_file(ckpt_path) if ckpt_path.exists() else None
        scratch = _read_json_safe(scratch_path) if scratch_path.exists() else None
        if not isinstance(scratch, dict):
            scratch = {}

        paused = paused_path.exists()
        pid = pid_map.get(name) or _find_agent_pid(name)
        running = pid is not None

        # derive status bucket
        if paused:
            bucket = "PAUSED"
        elif hb_age is None:
            bucket = "UNKNOWN"
            if running:
                bucket = "RUNNING"
        elif hb_age < 120:
            bucket = "RUNNING"
        elif hb_age < 600:
            bucket = "STALE"
        else:
            bucket = "DEAD"
        if running and bucket in ("STALE", "DEAD", "UNKNOWN"):
            # if we have a live pid, treat as RUNNING even if heartbeat stale recently
            # but keep STALE flag visually; don't override DEAD if very old (>600)?
            # keep original bucket but note running
            pass

        # state.json gives restart/consecutive
        restart_count = state.get("restart_count")
        consec_err = state.get("consecutive_errors")
        bot_status = state.get("status", "")
        started = state.get("started")

        cur_task = status.get("current_task", "") or scratch.get("phase", "") or ""
        task_desc = str(status.get("task_description", "") or "")[:60]
        if task_desc and task_desc != cur_task:
            if cur_task in ("", "starting") or cur_task.startswith("tool:"):
                cur_task = f"{cur_task} {task_desc}"[:40] if cur_task else task_desc[:40]
        if not cur_task and ckpt:
            cur_task = str(ckpt.get("current_task", "") or ckpt.get("reason", ""))[:40]
        iter_n = status.get("iteration", scratch.get("iteration", 0))
        status_age = None
        try:
            ua = status.get("updated_at")
            if ua:
                status_age = now - float(ua)
        except Exception:
            pass

        # log ages
        log_age = None
        tasklog_age = None
        log_size = None
        tasklog_lines = None
        try:
            if log_path.exists():
                log_age = now - log_path.stat().st_mtime
                log_size = log_path.stat().st_size
            if tasklog_path.exists():
                tasklog_age = now - tasklog_path.stat().st_mtime
                # quick line count capped at reading tail? we approximate via read
                try:
                    # bounded line count
                    txt = tasklog_path.read_text(encoding="utf-8", errors="ignore")
                    tasklog_lines = txt.count("\n")
                except Exception:
                    pass
        except Exception:
            pass

        # model info from state? try to infer from orchestrator registry not available here,
        # so try to read from status? but status doesn't have model; fallback to mission or log header not needed
        # try to infer from checkpoint or scratch? not reliable; leave blank and let live show "-"
        model = ""
        # attempt to read from state dir's bot_metrics or rl_state later; leave for throughput section
        # also try mission file first line?
        if mission_path.exists():
            try:
                head = mission_path.read_text(encoding="utf-8", errors="ignore")[:2000]
                m = re.search(r"model\s+([A-Za-z0-9\.\-_]+)", head)
                if m:
                    model = m.group(1)[:24]
            except Exception:
                pass

        # heartbeat path age for next_run estimation? Not available without orchestrator
        # we have started + iteration etc

        agents.append({
            "name": name,
            "bucket": bucket,
            "running": running,
            "pid": pid,
            "paused": paused,
            "hb_age": hb_age,
            "hb_ts": hb_ts,
            "status_age": status_age,
            "log_age": log_age,
            "tasklog_age": tasklog_age,
            "log_size": log_size,
            "tasklog_lines": tasklog_lines,
            "current_task": cur_task,
            "iteration": iter_n,
            "restart_count": restart_count,
            "consecutive_errors": consec_err,
            "bot_status": bot_status,
            "started": started,
            "model": model,
            "ckpt": ckpt,
            "state": state,
            "status": status,
            "scratch": scratch,
        })
    # sort: RUNNING first, then STALE, then PAUSED, then DEAD, then UNKNOWN
    order = {"RUNNING": 0, "STALE": 1, "PAUSED": 2, "DEAD": 3, "UNKNOWN": 4}
    agents.sort(key=lambda a: (order.get(a["bucket"], 9), (a["hb_age"] if a["hb_age"] is not None else 99999), a["name"]))
    return agents


def _collect_tickets(project_root: Path) -> tuple[Any | None, dict | None, list[Any]]:
    state_dir = _find_state_dir(project_root)
    # try multiple locations
    tickets_file = None
    for cand in [state_dir / "tickets.json", state_dir / "codebot_tickets.json", project_root / "tickets.json", Path.cwd() / "tickets.json"]:
        if cand.exists():
            tickets_file = cand
            break
    if tickets_file is None:
        return None, None, []
    # try TicketStore if available
    try:
        from codebot.ticket_engine import TicketStore
        store = TicketStore(tickets_file)
        summary = store.summary()
        # collect all tickets via internal dict if accessible
        tickets = list(getattr(store, "_tickets", {}).values()) if hasattr(store, "_tickets") else []
        # fallback if tickets empty but summary says total >0, read raw
        if not tickets:
            raw = _read_json_safe(tickets_file)
            if isinstance(raw, dict) and isinstance(raw.get("tickets"), list):
                tickets = raw["tickets"]
        return store, summary, tickets
    except Exception:
        raw = _read_json_safe(tickets_file)
        if isinstance(raw, dict):
            tickets = raw.get("tickets", [])
            # build summary manually
            cnt: dict[str, int] = Counter()  # type: ignore
            for t in tickets:
                st = t.get("state", "UNKNOWN") if isinstance(t, dict) else "UNKNOWN"
                cnt[str(st)] += 1
            return None, dict(cnt), tickets if isinstance(tickets, list) else []
        return None, None, []


def _collect_claims(project_root: Path) -> list[dict[str, Any]]:
    state_dir = _find_state_dir(project_root)
    claims_dir = state_dir / "claims"
    if not claims_dir.exists():
        return []
    claims: list[dict[str, Any]] = []
    now = time.time()
    for cf in sorted(claims_dir.glob("*.json")):
        try:
            data = json.loads(cf.read_text(encoding="utf-8", errors="ignore"))
            if not isinstance(data, dict):
                data = {}
            # claims from orchestrator use ticket_id/bot/at/class ; older botop expected worker/at
            ticket_id = data.get("ticket_id") or data.get("id") or cf.stem.split(".")[0]
            worker = data.get("bot") or data.get("worker") or data.get("owner") or "?"
            at = data.get("at") or data.get("claimed_at") or data.get("ts") or 0
            try:
                at_f = float(at)
            except Exception:
                at_f = 0
            age = (now - at_f) if at_f else 0
            claims.append({
                "file": cf.name,
                "path": cf,
                "ticket_id": str(ticket_id)[:24],
                "worker": str(worker),
                "at": at_f,
                "age": age,
                "class": data.get("class") or data.get("ticket_class") or "",
                "raw": data,
            })
        except Exception:
            claims.append({"file": cf.name, "path": cf, "ticket_id": "?", "worker": "?", "at": 0, "age": 0, "class": "", "raw": {}})
    claims.sort(key=lambda c: c["age"], reverse=True)
    return claims


def _collect_rl(project_root: Path) -> dict | None:
    state_dir = _find_state_dir(project_root)
    p = state_dir / "rl_state.json"
    j = _read_json_safe(p)
    if isinstance(j, dict):
        return j
    # also try codebot/state/rl_state.json fallback
    p2 = Path(__file__).parent / "state" / "rl_state.json"
    j2 = _read_json_safe(p2)
    return j2 if isinstance(j2, dict) else None


def _collect_token_ledger(project_root: Path) -> dict | None:
    state_dir = _find_state_dir(project_root)
    for cand in [state_dir / "token_ledger.json", Path(__file__).parent / "state" / "token_ledger.json", project_root / "state" / "token_ledger.json"]:
        j = _read_json_safe(cand)
        if isinstance(j, dict):
            return j
    return None


def _collect_leases(project_root: Path) -> dict | None:
    state_dir = _find_state_dir(project_root)
    p = state_dir / "leases.json"
    j = _read_json_safe(p)
    if isinstance(j, dict):
        return j
    return None

def _collect_orchestrator_info(project_root: Path) -> dict[str, Any]:
    state_dir = _find_state_dir(project_root)
    now = time.time()
    info: dict[str, Any] = {}
    pid_path = state_dir / "orchestrator.pid"
    try:
        if pid_path.exists():
            txt = pid_path.read_text(encoding="utf-8", errors="ignore").strip().split()
            pid = int(txt[0]) if txt else None
            info["pid"] = pid
            # check alive
            alive = False
            if pid:
                try:
                    os.kill(pid, 0)
                    alive = True
                except ProcessLookupError:
                    alive = False
                except PermissionError:
                    alive = True
            info["alive"] = alive
            try:
                info["pid_age"] = now - pid_path.stat().st_mtime
            except Exception:
                info["pid_age"] = None
        else:
            info["pid"] = None
            info["alive"] = False
    except Exception:
        info["pid"] = None
        info["alive"] = False
    last_spawn = state_dir / ".last_spawn"
    if last_spawn.exists():
        try:
            v = float(last_spawn.read_text(encoding="utf-8", errors="ignore").strip().split()[0])
            info["last_spawn_ts"] = v
            info["last_spawn_age"] = now - v
        except Exception:
            info["last_spawn_ts"] = None
            info["last_spawn_age"] = None
    else:
        info["last_spawn_ts"] = None
        info["last_spawn_age"] = None
    drain = state_dir / ".drain"
    info["drain"] = drain.exists()
    if info["drain"]:
        try:
            info["drain_text"] = drain.read_text(encoding="utf-8", errors="ignore").strip()[:200]
        except Exception:
            info["drain_text"] = ""
    else:
        info["drain_text"] = ""
    paused = list(state_dir.glob("*.paused"))
    info["paused_count"] = len(paused)
    info["paused_names"] = [p.name.replace(".paused", "") for p in paused[:20]]
    update_lock = state_dir / ".update_lock"
    info["update_lock"] = update_lock.exists()
    info["heartbeat_count"] = len(list(state_dir.glob("*.heartbeat")))
    return info


def _collect_budget_state(token_ledger: dict | None) -> tuple[str, int]:
    if not token_ledger:
        return "unknown", 0
    total = token_ledger.get("total_actual", 0)
    try:
        total_i = int(total)
    except Exception:
        total_i = 0
    # use token_budget thresholds
    cap = 4_000_000_000
    if total_i >= cap:
        return "stop", total_i
    if total_i >= cap * 0.9:
        return "shed_tier3", total_i
    if total_i >= cap * 0.8:
        return "warn", total_i
    return "ok", total_i


def _collect_ticket_throughput(tickets: list[Any]) -> dict[str, Any]:
    if not tickets:
        return {"total": 0, "by_state": {}, "by_severity": {}, "by_class": {}, "avg_age_h": None, "oldest_h": None, "newest_h": None, "rework_rate": 0, "throughput_24h": 0, "throughput_7d": 0}
    by_state: Counter = Counter()
    by_sev: Counter = Counter()
    by_class: Counter = Counter()
    ages: list[float] = []
    rework_total = 0
    now = time.time()
    created_24h = 0
    created_7d = 0
    for t in tickets:
        if isinstance(t, dict):
            st = str(t.get("state", "UNKNOWN"))
            sev = str(t.get("severity", ""))
            cl = str(t.get("ticket_class", ""))
            created = t.get("created_at") or 0
            updated = t.get("updated_at") or created
            rc = int(t.get("rework_count", 0) or 0)
            # normalize state upper
            by_state[st] += 1
            if sev:
                by_sev[sev] += 1
            if cl:
                by_class[cl] += 1
            if rc:
                rework_total += 1
            try:
                ca = float(created)
                if ca:
                    ages.append(now - ca)
                    if now - ca < 86400:
                        created_24h += 1
                    if now - ca < 7*86400:
                        created_7d += 1
            except Exception:
                pass
        else:
            # Ticket object
            try:
                by_state[t.state.value] += 1  # type: ignore
                by_sev[t.severity.value] += 1  # type: ignore
                by_class[t.ticket_class.value] += 1  # type: ignore
                ca = float(getattr(t, "created_at", 0) or 0)
                if ca:
                    ages.append(now - ca)
                    if now - ca < 86400:
                        created_24h += 1
                    if now - ca < 7*86400:
                        created_7d += 1
                if int(getattr(t, "rework_count", 0) or 0) > 0:
                    rework_total += 1
            except Exception:
                pass
    avg_age_h = (sum(ages)/len(ages)/3600) if ages else None
    oldest_h = (max(ages)/3600) if ages else None
    newest_h = (min(ages)/3600) if ages else None
    total = len(tickets)
    rework_rate = round(rework_total/total, 4) if total else 0
    return {
        "total": total,
        "by_state": dict(by_state),
        "by_severity": dict(by_sev),
        "by_class": dict(by_class),
        "avg_age_h": round(avg_age_h, 2) if avg_age_h is not None else None,
        "oldest_h": round(oldest_h, 2) if oldest_h is not None else None,
        "newest_h": round(newest_h, 2) if newest_h is not None else None,
        "rework_rate": rework_rate,
        "throughput_24h": created_24h,
        "throughput_7d": created_7d,
    }


def _collect_events_state(project_root: Path, limit: int = 5) -> list[dict]:
    state_dir = _find_state_dir(project_root)
    p = state_dir / "events.jsonl"
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]
        out = []
        for line in lines:
            try:
                j = json.loads(line)
                if isinstance(j, dict):
                    out.append(j)
            except Exception:
                continue
        return out
    except Exception:
        return []

def _collect_findings_state(project_root: Path) -> list[dict]:
    state_dir = _find_state_dir(project_root)
    p = state_dir / "findings.jsonl"
    if not p.exists():
        p = Path(__file__).parent / "state" / "findings.jsonl"
        if not p.exists():
            return []
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        out = []
        for line in lines[-20:]:
            try:
                j = json.loads(line)
                if isinstance(j, dict):
                    out.append(j)
            except Exception:
                continue
        return out
    except Exception:
        return []

def _collect_gatekeeper_state(project_root: Path) -> list[dict]:
    state_dir = _find_state_dir(project_root)
    for cand in [state_dir / "gatekeeper_log.jsonl", Path(__file__).parent / "state" / "gatekeeper_log.jsonl"]:
        if cand.exists():
            try:
                lines = cand.read_text(encoding="utf-8", errors="ignore").splitlines()[-10:]
                out = []
                for line in lines:
                    try:
                        j = json.loads(line)
                        if isinstance(j, dict):
                            out.append(j)
                    except Exception:
                        continue
                return out
            except Exception:
                return []
    return []

def _collect_anomalies(project_root: Path) -> list[dict]:
    state_dir = _find_state_dir(project_root)
    p = state_dir / "anomaly_alerts.json"
    j = _read_json_safe(p)
    if isinstance(j, list):
        return j[-10:]
    if isinstance(j, dict) and isinstance(j.get("alerts"), list):
        return j["alerts"][-10:]
    return []

def _collect_bot_metrics(project_root: Path) -> dict | None:
    state_dir = _find_state_dir(project_root)
    for cand in [state_dir / "bot_metrics.json", Path(__file__).parent / "state" / "bot_metrics.json"]:
        j = _read_json_safe(cand)
        if isinstance(j, dict):
            return j
    return None


# ---------------------------------------------------------------------------
# Helpers: formatting
# ---------------------------------------------------------------------------

def _age_str(age: float | None, enabled: bool = False) -> str:
    if age is None:
        return _c("-", "gray", enabled)
    if age < 0:
        age = 0
    if age < 60:
        s = f"{age:.0f}s"
        col = "green" if age < 120 else "yellow"
    elif age < 3600:
        s = f"{age/60:.0f}m"
        col = "yellow" if age < 600 else "red"
    elif age < 86400:
        s = f"{age/3600:.1f}h"
        col = "red"
    else:
        s = f"{age/86400:.1f}d"
        col = "red"
    return _c(s, col, enabled) if col else s

def _bucket_color(bucket: str, enabled: bool) -> str:
    cols = {"RUNNING": "green", "STALE": "yellow", "PAUSED": "cyan", "DEAD": "red", "UNKNOWN": "gray"}
    return _c(bucket, cols.get(bucket, "gray"), enabled)

def _severity_color(sev: str, enabled: bool) -> str:
    m = {"critical": "red", "high": "yellow", "medium": "cyan", "low": "gray"}
    return _c(sev, m.get(sev.lower(), "gray"), enabled)

def _state_color(st: str, enabled: bool) -> str:
    cols = {
        "IMPLEMENTING": "yellow", "REVIEWING": "cyan", "VERIFYING": "blue",
        "COMPLETE": "green", "READY": "green", "PLANNING": "magenta",
        "REWORK": "red", "BLOCKED": "red", "DISCOVERED": "gray",
    }
    return _c(st, cols.get(st.upper(), "gray"), enabled)


# ---------------------------------------------------------------------------
# Commands — original + improved
# ---------------------------------------------------------------------------

def cmd_status(project_root: Path, verbose: bool = False, json_out: bool = False) -> int:
    agents = _collect_agents(project_root)
    orch = _collect_orchestrator_info(project_root)
    state_dir = _find_state_dir(project_root)
    if json_out:
        payload = {
            "project": str(project_root),
            "state_dir": str(state_dir),
            "orchestrator": orch,
            "agents": [
                {k: v for k, v in a.items() if k not in ("ckpt", "state", "status", "scratch")}
                for a in agents
            ],
            "timestamp": time.time(),
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0

    enabled = _supports_color()
    now_h = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(_c(f"CodeBot status — {project_root.name} — {now_h}", "bold", enabled))
    print(f"state: {state_dir}  logs: {_find_logs_dir(project_root)}")
    orch_pid = orch.get("pid")
    orch_alive = orch.get("alive")
    ls_age = orch.get("last_spawn_age")
    ls_s = f"{ls_age:.0f}s ago" if isinstance(ls_age, (int, float)) else "?"
    drain_txt = _c("DRAIN ACTIVE", "red", enabled) if orch.get("drain") else _c("drain clear", "green", enabled)
    paused_txt = f"paused={orch.get('paused_count')}" if orch.get("paused_count") else "paused=0"
    orch_s = f"orch pid={orch_pid or '-'} {'alive' if orch_alive else 'down'}  last_spawn {ls_s}  {drain_txt}  {paused_txt}"
    print(orch_s)
    if orch.get("drain") and orch.get("drain_text"):
        print(f"  drain: {orch['drain_text'][:120]}")
    if not agents:
        print("No agents found.")
        return 0
    # header
    if verbose:
        print(f"{'Agent':<24s} {'Status':<10s} {'HB':>8s} {'LOG':>8s} {'ITER':>5s} {'TASK':<20s} {'PID':>7s} {'RST':>4s} {'ERR':>4s} {'MODEL':<20s}")
        print("-" * 116)
        for a in agents:
            hb = _age_str(a["hb_age"], enabled) if a["hb_age"] is not None else _c("-", "gray", enabled)
            loga = _age_str(a["log_age"], enabled) if a["log_age"] is not None else _c("-", "gray", enabled)
            bucket = _bucket_color(a["bucket"], enabled)
            pid_s = str(a["pid"]) if a["pid"] else "-"
            task = (a["current_task"] or "")[:20]
            model = (a["model"] or "-")[:20]
            rst = str(a["restart_count"]) if a["restart_count"] is not None else "-"
            err = str(a["consecutive_errors"]) if a["consecutive_errors"] is not None else "-"
            print(f"{a['name']:<24s} {_ansi_pad(bucket, 10)} {_ansi_pad(hb, 8, 'right')} {_ansi_pad(loga, 8, 'right')} {str(a['iteration']):>5s} {task:<20s} {pid_s:>7s} {rst:>4s} {err:>4s} {model:<20s}")
    else:
        print(f"{'Agent':<24s} {'Status':<10s} {'HB Age':>8s} {'PID':>7s} {'ITER':>5s} {'TASK':<20s} {'MODEL':<20s}")
        print("-" * 101)
        for a in agents:
            bucket = _bucket_color(a["bucket"], enabled)
            pid_s = str(a["pid"]) if a["pid"] else "-"
            task = (a["current_task"] or "")[:20]
            iter_s = str(a["iteration"]) if a["iteration"] else "-"
            hb_c = _age_str(a["hb_age"], enabled) if a["hb_age"] is not None else _c("?", "gray", enabled)
            model = (a["model"] or "-")[:20]
            print(f"{a['name']:<24s} {_ansi_pad(bucket, 10)} {_ansi_pad(hb_c, 8, 'right')} {pid_s:>7s} {iter_s:>5s} {task:<20s} {model:<20s}")
    # summary
    cnt = Counter(a["bucket"] for a in agents)
    print(f"\nAgents: {len(agents)}  " + "  ".join(f"{k}={cnt.get(k,0)}" for k in ["RUNNING","STALE","PAUSED","DEAD","UNKNOWN"] if cnt.get(k)))
    running = [a for a in agents if a["bucket"]=="RUNNING"]
    if running:
        print(f"Running PIDs: " + ", ".join(f"{a['name']}:{a['pid']}" for a in running[:10] if a["pid"]))
    return 0


def cmd_logs(project_root: Path, agent: str, lines: int = 50, follow: bool = False) -> int:
    logs_dir = _find_logs_dir(project_root)
    # allow agent without suffix; try .log first, fallback to tasklog
    candidates = [logs_dir / f"{agent}.log", logs_dir / f"{agent}.tasklog", logs_dir / f"{agent}.mission"]
    log_file = candidates[0]
    if not log_file.exists():
        # if agent.log missing, try to find any matching
        found = None
        for cand in candidates:
            if cand.exists():
                found = cand
                break
        if not found:
            print(f"No log file found: {log_file}", file=sys.stderr)
            # list available
            avail = sorted(p.stem for p in logs_dir.glob("*.log"))
            if avail:
                print(f"Available: {', '.join(avail[:20])}", file=sys.stderr)
            return 1
        log_file = found
    if follow:
        print(f"Following {log_file} — Ctrl-C to stop", file=sys.stderr)
        proc = None
        try:
            proc = subprocess.Popen(["tail", "-F", "-n", str(lines), str(log_file)], stdout=subprocess.PIPE, text=True, bufsize=1)
            assert proc.stdout is not None
            for line in proc.stdout:
                print(line, end="")
        except FileNotFoundError:
            try:
                with open(log_file, encoding="utf-8", errors="ignore") as f:
                    f.seek(0, 2)
                    f.seek(0)
                    all_lines = f.readlines()
                    for line in all_lines[-lines:]:
                        print(line, end="")
                    while True:
                        line = f.readline()
                        if not line:
                            time.sleep(0.5)
                            continue
                        print(line, end="")
            except KeyboardInterrupt:
                pass
        except KeyboardInterrupt:
            pass
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        return 0
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), str(log_file)],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            print(result.stdout, end="")
            return 0
        raise FileNotFoundError
    except FileNotFoundError:
        try:
            with open(log_file, encoding="utf-8", errors="ignore") as f:
                all_lines = f.readlines()
                for line in all_lines[-lines:]:
                    print(line, end="")
            return 0
        except Exception as e:
            print(f"Error reading logs: {e}", file=sys.stderr)
            return 1
    except Exception as e:
        print(f"Error reading logs: {e}", file=sys.stderr)
        return 1


def cmd_restart(project_root: Path, agent: str) -> int:
    pid = _find_agent_pid(agent)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to {agent} (PID {pid})")
            time.sleep(2)
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
                print(f"Sent SIGKILL to {agent} (PID {pid})")
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            print(f"{agent} (PID {pid}) already exited")
        except PermissionError:
            print(f"Permission denied to kill {agent} (PID {pid})", file=sys.stderr)
            return 1
    else:
        print(f"{agent} is not running")
    return 0


def cmd_pause(project_root: Path, agent: str) -> int:
    state_dir = _find_state_dir(project_root)
    pause_file = state_dir / f"{agent}.paused"
    pause_file.write_text(str(time.time()))
    print(f"Paused {agent}")
    return 0


def cmd_resume(project_root: Path, agent: str) -> int:
    state_dir = _find_state_dir(project_root)
    pause_file = state_dir / f"{agent}.paused"
    try:
        pause_file.unlink()
        print(f"Resumed {agent}")
    except FileNotFoundError:
        print(f"{agent} was not paused")
    return 0


def cmd_drain(project_root: Path, reason: str = "manual") -> int:
    state_dir = _find_state_dir(project_root)
    state_dir.mkdir(parents=True, exist_ok=True)
    drain_file = state_dir / ".drain"
    drain_file.write_text(f"{time.time()} {reason}")
    print(f"Drain set: {reason}")
    return 0


def cmd_clear_drain(project_root: Path) -> int:
    state_dir = _find_state_dir(project_root)
    drain_file = state_dir / ".drain"
    try:
        drain_file.unlink()
        print("Drain cleared")
    except FileNotFoundError:
        print("No drain active")
    return 0


def cmd_claims(project_root: Path, json_out: bool = False) -> int:
    claims = _collect_claims(project_root)
    state_dir = _find_state_dir(project_root)
    if json_out:
        print(json.dumps(claims, indent=2, default=str))
        return 0
    if not claims:
        claims_dir = state_dir / "claims"
        if not claims_dir.exists():
            print("No claims directory")
        else:
            print("No active claims")
        return 0
    enabled = _supports_color()
    print(_c(f"Claims ({len(claims)})", "bold", enabled))
    print(f"{'Claim File':<45s} {'Worker':<22s} {'Ticket':<16s} {'Age':>8s} {'Class':<10s}")
    print("-" * 110)
    for c in claims[:30]:
        age_s = f"{c['age']:.0f}s" if c["age"] else "?"
        age_c = _c(age_s, "yellow" if c["age"]>300 else "green" if c["age"]<60 else "gray", enabled)
        worker = c["worker"][:22]
        tid = c["ticket_id"][:16]
        cl = c["class"][:10]
        print(f"{c['file']:<45s} {worker:<22s} {tid:<16s} {_ansi_pad(age_c, 18, 'right')} {cl:<10s}")
    stale = [c for c in claims if c["age"] > 7200]
    if stale:
        print(_c(f"\nStale claims (>2h): {len(stale)} — may be orphaned, consider lease inspect", "yellow", enabled))
    return 0


def cmd_tickets(project_root: Path, json_out: bool = False, limit: int = 15, state_filter: str | None = None) -> int:
    store, summary, tickets = _collect_tickets(project_root)
    if not summary and not tickets:
        print("No ticket store found")
        return 0
    enabled = _supports_color()
    if json_out:
        # raw tickets to dict
        out = []
        for t in tickets:
            if isinstance(t, dict):
                out.append(t)
            else:
                try:
                    out.append(t.to_dict())  # type: ignore
                except Exception:
                    out.append(str(t))
        print(json.dumps({"summary": summary, "tickets": out[: limit if limit else 100]}, indent=2, default=str))
        return 0

    total = len(tickets) if tickets else sum(summary.values()) if summary else 0  # type: ignore
    print(_c(f"Tickets — total {total}", "bold", enabled))
    if summary:
        # colored state table
        ordered_states = ["DISCOVERED","VALIDATING","TRIAGED","READY","PLANNING","IMPLEMENTING","REVIEWING","VERIFYING","COMPLETE","REWORK","BLOCKED","DEFERRED","REJECTED","DUPLICATE"]
        present = [s for s in ordered_states if summary.get(s)]
        # also include unexpected
        for k in summary:
            if k not in ordered_states:
                present.append(k)
        line = "  ".join(f"{_state_color(k, enabled)}={summary[k]}" for k in present)
        print(f"By state: {line}")
        sev_cnt: Counter = Counter()
        class_cnt: Counter = Counter()
        risk_cnt: Counter = Counter()
        for t in tickets:
            if isinstance(t, dict):
                sev_cnt[str(t.get("severity",""))] += 1
                class_cnt[str(t.get("ticket_class",""))] += 1
                risk_cnt[str(t.get("risk",""))] += 1
            else:
                try:
                    sev = getattr(t, "severity", "")
                    sev_cnt[str(sev.value if hasattr(sev, "value") else sev)] += 1
                except Exception:
                    pass
                try:
                    cl = getattr(t, "ticket_class", "")
                    class_cnt[str(cl.value if hasattr(cl, "value") else cl)] += 1
                except Exception:
                    pass
                try:
                    rk = getattr(t, "risk", "")
                    risk_cnt[str(rk.value if hasattr(rk, "value") else rk)] += 1
                except Exception:
                    pass

    # throughput derived
    thr = _collect_ticket_throughput(tickets)
    if thr["total"]:
        print(f"Throughput: 24h +{thr['throughput_24h']}  7d +{thr['throughput_7d']}  avg age {thr['avg_age_h']}h  oldest {thr['oldest_h']}h  rework {thr['rework_rate']*100:.1f}%")

    # show filtered or ready etc
    def _ticket_title(t: Any) -> str:
        if isinstance(t, dict):
            return str(t.get("title",""))[:70]
        return str(getattr(t, "title",""))[:70]
    def _ticket_id(t: Any) -> str:
        if isinstance(t, dict):
            return str(t.get("id",""))
        return str(getattr(t, "id",""))
    def _ticket_sev(t: Any) -> str:
        if isinstance(t, dict):
            return str(t.get("severity",""))
        try:
            v = getattr(t, "severity", "")
            return v.value if hasattr(v,"value") else str(v)
        except Exception:
            return ""
    def _ticket_state(t: Any) -> str:
        if isinstance(t, dict):
            return str(t.get("state",""))
        try:
            v = getattr(t, "state", "")
            return v.value if hasattr(v,"value") else str(v)
        except Exception:
            return ""
    def _ticket_rework(t: Any) -> int:
        if isinstance(t, dict):
            return int(t.get("rework_count", 0) or 0)
        try:
            return int(getattr(t, "rework_count", 0) or 0)
        except Exception:
            return 0

    # if filter requested
    filtered = tickets
    if state_filter:
        sf = state_filter.upper()
        filtered = [t for t in tickets if _ticket_state(t).upper() == sf]

    if state_filter:
        print(f"\n{state_filter.upper()} tickets ({len(filtered)}):")
        for t in filtered[:limit]:
            sev = _ticket_sev(t)
            rw = _ticket_rework(t)
            rw_s = f" R{rw}" if rw else ""
            print(f"  [{_ansi_pad(_severity_color(sev, enabled), 10)}] {_ticket_id(t):<18s}{rw_s} {_ticket_title(t)}")
    else:
        pipeline = ["DISCOVERED","VALIDATING","TRIAGED","READY","PLANNING","IMPLEMENTING","REVIEWING","VERIFYING","COMPLETE"]
        side_states = ["REWORK","BLOCKED","DEFERRED","REJECTED","DUPLICATE"]

        def _show_state(st: str) -> None:
            subset = [t for t in tickets if _ticket_state(t).upper() == st]
            count = len(subset)
            indicator = _c("●", "green", enabled) if count > 0 else _c("○", "gray", enabled)
            print(f"\n{indicator} {st} ({count}):")
            if count == 0:
                print("  (empty)")
                return
            if st == "READY":
                sev_order = {"critical":0,"high":1,"medium":2,"low":3}
                subset = sorted(subset, key=lambda x: sev_order.get(_ticket_sev(x).lower(), 99))
            for t in subset[:limit]:
                sev = _ticket_sev(t)
                tid = _ticket_id(t)
                title = _ticket_title(t)
                rw = _ticket_rework(t)
                rw_s = f" R{rw}" if rw else ""
                print(f"  [{_ansi_pad(_severity_color(sev, enabled), 18)}] {tid:<20s}{rw_s} {title}")
            if count > limit:
                print(f"  ... +{count-limit} more (use --limit {limit*2})")

        print(_c("\n── Pipeline ──", "bold", enabled))
        for st in pipeline:
            _show_state(st)

        print(_c("\n── Side States ──", "bold", enabled))
        for st in side_states:
            _show_state(st)

    return 0


def cmd_throughput(project_root: Path, json_out: bool = False) -> int:
    _, summary, tickets = _collect_tickets(project_root)
    claims = _collect_claims(project_root)
    rl = _collect_rl(project_root)
    ledger = _collect_token_ledger(project_root)
    enabled = _supports_color()
    thr = _collect_ticket_throughput(tickets)
    budget_state, budget_total = _collect_budget_state(ledger)

    if json_out:
        payload = {
            "tickets": thr,
            "claims_active": len(claims),
            "claims_stale": len([c for c in claims if c["age"]>7200]),
            "budget_state": budget_state,
            "budget_total": budget_total,
            "rl_global": (rl or {}).get("global") if rl else None,
            "rl_bots": len((rl or {}).get("bots", {})) if rl else 0,
            "summary": summary,
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0

    print(_c("Throughput & Flow", "bold", enabled))
    print(f"Tickets: total {thr['total']}  " + "  ".join(f"{k}={v}" for k,v in (summary or {}).items()))
    print(f"New tickets: 24h {thr['throughput_24h']}  7d {thr['throughput_7d']}  avg age {thr['avg_age_h']}h  rework {thr['rework_rate']*100:.1f}%")
    print(f"Claims: active {len(claims)}  stale>2h {len([c for c in claims if c['age']>7200])}  oldest {max((c['age'] for c in claims), default=0):.0f}s")
    if ledger:
        print(f"Budget: {budget_state}  total {budget_total:,}  day {ledger.get('day_utc','?')}  per-model {len(ledger.get('by_model',{}))}")
        bm = ledger.get("by_model") or {}
        for mod, row in list(bm.items())[:6]:
            try:
                pa = int(row.get("prompt_actual",0)); ca = int(row.get("completion_actual",0))
                print(f"  {mod:<20s} prompt {pa:>8d}  completion {ca:>8d}  total {pa+ca:>9d}")
            except Exception:
                pass
    else:
        print("Budget: no token_ledger.json (scheduler not yet emitted)")
    if rl and isinstance(rl.get("global"), dict):
        g = rl["global"]
        print(f"RL: total_events {g.get('total_events',0)}  total_rewards {g.get('total_rewards',0):.1f}  avg {g.get('avg_reward_global',0):.3f}  bots {len(rl.get('bots',{}))}")
        # top performers
        bots = rl.get("bots",{})
        if bots:
            # sort by avg_reward desc
            sorted_bots = sorted(bots.items(), key=lambda kv: float(kv[1].get("avg_reward",0) or 0), reverse=True)
            print("Top bots by avg_reward:")
            for name, b in sorted_bots[:6]:
                print(f"  {name:<28s} runs={b.get('total_runs',0):>4d} succ={b.get('successes',0):>4d} fail={b.get('failures',0):>3d} avg={float(b.get('avg_reward',0)):.3f} eps={b.get('epsilon',0):.2f} succ_streak={b.get('consecutive_successes',0)}")
            # worst
            worst = [kv for kv in sorted_bots if kv[1].get("failures",0)>0][-3:]
            if worst:
                print("Bots with failures:")
                for name,b in worst:
                    print(f"  {name:<28s} fail={b.get('failures',0)} fail_streak={b.get('consecutive_failures',0)} last_reward={b.get('last_reward','?')}")
    else:
        print("RL: no rl_state.json")
    # gatekeeper throughput
    gk = _collect_gatekeeper_state(project_root)
    if gk:
        passes = sum(1 for e in gk if e.get("decision")=="COMPLETE" or e.get("passed") is True)
        fails = len(gk)-passes
        print(f"Gatekeeper (last {len(gk)}): pass {passes}  rework {fails}")
    # leases throughput
    leases = _collect_leases(project_root)
    if leases is not None:
        print(f"Leases: active {len(leases.get('leases',{}))}  dead_letters {len(leases.get('dead_letters',[]))}  attempts tracked {len(leases.get('attempts',{}))}")
    return 0


def cmd_metrics(project_root: Path, json_out: bool = False) -> int:
    rl = _collect_rl(project_root)
    bm = _collect_bot_metrics(project_root)
    ledger = _collect_token_ledger(project_root)
    enabled = _supports_color()
    if json_out:
        print(json.dumps({"rl": rl, "bot_metrics": bm, "ledger": ledger}, indent=2, default=str))
        return 0
    print(_c("Metrics", "bold", enabled))
    if bm:
        summ = bm.get("summary", {})
        print(f"Bot metrics @ {bm.get('timestamp_human','?')}  fleet tokens {bm.get('ledger_total_actual',0):,}  tracked {summ.get('bots_tracked',0)} active {summ.get('active',0)} idle {summ.get('idle',0)} stale {summ.get('stale',0)} erroring {summ.get('erroring',0)}")
    elif rl:
        g = rl.get("global",{})
        print(f"RL global: events {g.get('total_events',0)} rewards {g.get('total_rewards',0):.1f} avg {g.get('avg_reward_global',0):.3f}")
    else:
        print("No bot_metrics.json or rl_state.json found")

    # per-agent metrics collector style if available
    agents = _collect_agents(project_root)
    if agents and rl:
        bots = rl.get("bots",{})
        print(f"\nPer-agent (from rl_state + heartbeat):")
        print(f"{'Agent':<26s} {'Runs':>5s} {'Succ':>5s} {'Fail':>5s} {'AvgR':>6s} {'HB':>7s} {'Task':<20s}")
        print("-" * 90)
        for a in agents[:25]:
            b = bots.get(a["name"], {})
            runs = b.get("total_runs", 0)
            succ = b.get("successes", 0)
            fail = b.get("failures", 0)
            avg = b.get("avg_reward", 0)
            hb = _age_str(a["hb_age"], enabled) if a["hb_age"] is not None else "-"
            task = (a["current_task"] or "")[:20]
            print(f"{a['name']:<26s} {str(runs):>5s} {str(succ):>5s} {str(fail):>5s} {f'{float(avg):.2f}' if avg else '-':>6s} {_ansi_pad(hb, 15, 'right')} {task:<20s}")
    # ledger per-model
    if ledger:
        print(f"\nToken ledger day {ledger.get('day_utc','?')} total {ledger.get('total_actual',0):,}")
        for mod, row in list((ledger.get("by_model") or {}).items())[:10]:
            print(f"  {mod:<22s} {row}")
    return 0


def cmd_budget(project_root: Path, json_out: bool = False) -> int:
    ledger = _collect_token_ledger(project_root)
    if not ledger:
        print("No token_ledger.json found")
        return 0
    if json_out:
        print(json.dumps(ledger, indent=2))
        return 0
    enabled = _supports_color()
    print(_c(f"Token budget — day {ledger.get('day_utc','?')}", "bold", enabled))
    total = ledger.get("total_actual", 0)
    state, _ = _collect_budget_state(ledger)
    state_c = _c(state, {"ok":"green","warn":"yellow","shed_tier3":"yellow","stop":"red"}.get(state,"gray"), enabled)
    print(f"Total actual: {total:,}  state: {state_c}  cap 4,000,000,000")
    bm = ledger.get("by_model") or {}
    if bm:
        print(f"{'Model':<24s} {'Prompt':>10s} {'Completion':>11s} {'Total':>10s}")
        print("-" * 60)
        sorted_m = sorted(bm.items(), key=lambda kv: int(kv[1].get("prompt_actual",0))+int(kv[1].get("completion_actual",0)), reverse=True)
        for mod, row in sorted_m[:20]:
            pa = int(row.get("prompt_actual",0) or 0)
            ca = int(row.get("completion_actual",0) or 0)
            print(f"{mod:<24s} {pa:>10d} {ca:>11d} {pa+ca:>10d}")
    return 0


def cmd_events(project_root: Path, limit: int = 20, json_out: bool = False, event_type: str | None = None) -> int:
    state_dir = _find_state_dir(project_root)
    p = state_dir / "events.jsonl"
    if not p.exists():
        print("No events.jsonl")
        return 0
    events = _collect_events_state(project_root, limit=limit*2 if event_type else limit)
    if event_type:
        events = [e for e in events if e.get("type")==event_type][-limit:]
    if json_out:
        print(json.dumps(events, indent=2, default=str))
        return 0
    enabled = _supports_color()
    print(_c(f"Events (last {len(events)})", "bold", enabled))
    for e in events[-limit:]:
        ts = e.get("ts", 0)
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%H:%M:%S")
        except Exception:
            dt = str(ts)[:10]
        typ = e.get("type","unknown")
        data = e.get("data",{})
        # sanitize preview
        preview = json.dumps(data, ensure_ascii=False)[:100].replace("\n"," ")
        print(f"{dt}  {_ansi_pad(_c(typ, 'cyan', enabled), 18)} {preview}")
    return 0


def cmd_findings(project_root: Path, limit: int = 20, json_out: bool = False) -> int:
    findings = _collect_findings_state(project_root)
    if not findings:
        print("No findings.jsonl")
        return 0
    if json_out:
        print(json.dumps(findings[-limit:], indent=2, default=str))
        return 0
    enabled = _supports_color()
    print(_c(f"Findings (last {len(findings[-limit:])})", "bold", enabled))
    for f in findings[-limit:]:
        sev = f.get("severity","")
        bot = f.get("bot","")
        mod = f.get("module","")
        txt = str(f.get("finding",""))[:80]
        sev_c = _severity_color(sev, enabled)
        print(f"[{_ansi_pad(sev_c, 18)}] {bot:<22s} {mod:<20s} {txt}")
    return 0


def cmd_leases(project_root: Path, json_out: bool = False) -> int:
    leases = _collect_leases(project_root)
    if leases is None:
        print("No leases.json")
        return 0
    if json_out:
        print(json.dumps(leases, indent=2))
        return 0
    enabled = _supports_color()
    print(_c("Leases", "bold", enabled))
    ls = leases.get("leases",{})
    attempts = leases.get("attempts",{})
    dls = leases.get("dead_letters",[])
    print(f"Active leases: {len(ls)}  tracked attempts: {len(attempts)}  dead letters: {len(dls)}")
    if ls:
        print(f"{'ID':<22s} {'Owner':<20s} {'Expires in':>12s} {'Attempt':>8s}")
        print("-" * 70)
        now = time.time()
        for iid, row in list(ls.items())[:20]:
            owner = str(row.get("owner",""))[:20]
            exp = float(row.get("expires_at",0) or 0)
            age = exp - now
            age_s = f"{age:.0f}s" if age>0 else "expired"
            print(f"{iid:<22s} {owner:<20s} {age_s:>12s} {str(row.get('attempt','')):>8s}")
    if dls:
        print(f"\nDead letters ({len(dls)}):")
        for dl in dls[:10]:
            print(f"  {dl.get('id','?'):<22s} {str(dl.get('reason',''))[:60]}")
    return 0


def cmd_deadletters(project_root: Path, json_out: bool = False) -> int:
    from codebot.lease_state import dead_letters as _dl
    state_dir = _find_state_dir(project_root)
    try:
        letters = _dl(state_dir)
    except Exception:
        letters = []
    if json_out:
        print(json.dumps(letters, indent=2))
        return 0
    enabled = _supports_color()
    print(_c(f"Dead letters ({len(letters)})", "bold", enabled))
    for dl in letters[:20]:
        print(f"  {dl.get('id','?'):<20s} {str(dl.get('reason',''))[:80]}")
    return 0


def cmd_gatekeeper(project_root: Path, json_out: bool = False) -> int:
    gk = _collect_gatekeeper_state(project_root)
    if not gk:
        print("No gatekeeper_log.jsonl")
        return 0
    if json_out:
        print(json.dumps(gk, indent=2, default=str))
        return 0
    enabled = _supports_color()
    print(_c(f"Gatekeeper (last {len(gk)})", "bold", enabled))
    for e in gk[-15:]:
        ts = e.get("timestamp", 0)
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%m-%d %H:%M")
        except Exception:
            dt = str(ts)[:16]
        tid = e.get("ticket_id","?")
        dec = e.get("decision","?")
        col = "green" if dec=="COMPLETE" else "red" if dec=="REWORK" else "yellow"
        print(f"{dt}  {tid:<18s} {_ansi_pad(_c(dec, col, enabled), 18)} rework={e.get('rework_count',0)}  failed={','.join(e.get('failed_gates',[]) or [])[:40]}")
    return 0


def cmd_health(project_root: Path, json_out: bool = False) -> int:
    """Comprehensive diagnostics — the 'any other diagnostic information' view."""
    state_dir = _find_state_dir(project_root)
    logs_dir = _find_logs_dir(project_root)
    agents = _collect_agents(project_root)
    _, summary, tickets = _collect_tickets(project_root)
    claims = _collect_claims(project_root)
    rl = _collect_rl(project_root)
    ledger = _collect_token_ledger(project_root)
    leases = _collect_leases(project_root)
    orch = _collect_orchestrator_info(project_root)
    bm = _collect_bot_metrics(project_root)
    gk = _collect_gatekeeper_state(project_root)
    events = _collect_events_state(project_root, limit=5)
    findings = _collect_findings_state(project_root)
    anom = _collect_anomalies(project_root)
    enabled = _supports_color() and not json_out

    if json_out:
        thr = _collect_ticket_throughput(tickets)
        payload = {
            "project": str(project_root),
            "state_dir": str(state_dir),
            "orchestrator": orch,
            "agents_summary": dict(Counter(a["bucket"] for a in agents)),
            "agents": [{k: v for k,v in a.items() if k not in ("ckpt","state","status","scratch")} for a in agents],
            "tickets": thr,
            "summary": summary,
            "claims": len(claims),
            "claims_stale": len([c for c in claims if c["age"]>7200]),
            "budget": ledger,
            "rl_global": (rl or {}).get("global"),
            "leases": leases,
            "gatekeeper_tail": gk[-5:],
            "events_tail": events,
            "anomalies": anom,
            "bot_metrics": bm.get("summary") if bm else None,
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0

    print(_c(f"\n══ CodeBot Diagnostics — {project_root.name} — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} ══", "bold", enabled))
    print(f"state: {state_dir}  logs: {logs_dir}  project: {_find_project_name(project_root)}")

    # Orch
    print(_c("\n─ Orchestrator ─", "bold", enabled))
    print(f"pid {orch.get('pid') or '-'} {'alive' if orch.get('alive') else 'down'}  last_spawn {f'{orch.get('last_spawn_age'):.0f}s ago' if orch.get('last_spawn_age') is not None else '?'}  heartbeats {orch.get('heartbeat_count')}  paused {orch.get('paused_count')}  drain {_c('YES','red',enabled) if orch.get('drain') else _c('no','green',enabled)}")
    if orch.get('paused_names'):
        print(f"  paused: {', '.join(orch['paused_names'][:10])}")
    if orch.get('drain_text'):
        print(f"  drain: {orch['drain_text'][:160]}")

    # Agents
    print(_c("\n─ Agents ─", "bold", enabled))
    cnt = Counter(a["bucket"] for a in agents)
    print(f"total {len(agents)}  RUNNING {cnt.get('RUNNING',0)}  STALE {cnt.get('STALE',0)}  PAUSED {cnt.get('PAUSED',0)}  DEAD {cnt.get('DEAD',0)}  UNKNOWN {cnt.get('UNKNOWN',0)}")
    # highlight problematic
    stale = [a for a in agents if a["bucket"] in ("STALE","DEAD")]
    if stale:
        print(_c(f"  attention: {len(stale)} stale/dead:", "yellow", enabled) + " " + ", ".join(f"{a['name']}({_age_str(a['hb_age'], False)})" for a in stale[:10]))
    erring = [a for a in agents if a.get("consecutive_errors") and int(a["consecutive_errors"] or 0) > 0]
    if erring:
        print(_c(f"  erroring: {len(erring)}", "red", enabled) + " " + ", ".join(f"{a['name']}:{a['consecutive_errors']}" for a in erring[:8]))
    # top running
    running = [a for a in agents if a["bucket"]=="RUNNING"][:8]
    if running:
        print(f"  running: {', '.join(f'{a['name']} hb {a['hb_age']:.0f}s' for a in running)}")

    # Tickets
    print(_c("\n─ Tickets ─", "bold", enabled))
    if summary:
        line = "  ".join(f"{_state_color(k, enabled)}={v}" for k,v in summary.items())
        print(f"states: {line}  total {sum(summary.values())}")
    thr = _collect_ticket_throughput(tickets)
    if thr["total"]:
        print(f"flow: 24h +{thr['throughput_24h']}  7d +{thr['throughput_7d']}  avg age {thr['avg_age_h']}h  rework {thr['rework_rate']*100:.1f}%")
    # next ready peek
    ready = [t for t in tickets if (t.get("state") if isinstance(t, dict) else getattr(t,"state","").value if hasattr(getattr(t,"state",""),"value") else getattr(t,"state","")) == "READY"] if tickets and isinstance(tickets[0], dict) else [t for t in tickets if str(getattr(t,"state","")).upper()=="READY"]
    if ready:
        print(f"  ready queue peek: {[ (t.get('id') if isinstance(t, dict) else getattr(t,'id','')) for t in ready[:3]]}")

    # Claims/Leases
    print(_c("\n─ Claims / Leases ─", "bold", enabled))
    print(f"claims active {len(claims)}  stale>2h {len([c for c in claims if c['age']>7200])}")
    if claims:
        oldest = max(claims, key=lambda c: c["age"])
        print(f"  oldest: {oldest['file']} age {oldest['age']:.0f}s worker {oldest['worker']}")
    if leases is not None:
        print(f"leases active {len(leases.get('leases',{}))}  dead_letters {len(leases.get('dead_letters',[]))}")
        if leases.get("dead_letters"):
            print(f"  dead: {[d.get('id') for d in leases['dead_letters'][:5]]}")
    else:
        # try lease_state file check
        ls_path = state_dir / "leases.json"
        print(f"leases.json {'present' if ls_path.exists() else 'absent'}")

    # Throughput / Budget
    print(_c("\n─ Throughput / Budget ─", "bold", enabled))
    if ledger:
        state, total = _collect_budget_state(ledger)
        print(f"budget {state}  total {total:,}  day {ledger.get('day_utc','?')}  per-model {len(ledger.get('by_model',{}))}")
    else:
        print("budget: no ledger")
    if rl and rl.get("global"):
        g = rl["global"]
        print(f"RL global events {g.get('total_events')}  avg {g.get('avg_reward_global',0):.3f}  bots {len(rl.get('bots',{}))}")
    if bm:
        print(f"bot_metrics: {bm.get('summary',{})}")
    if gk:
        print(f"gatekeeper last {len(gk)}: " + ", ".join(f"{e.get('ticket_id','?')[:8]}->{e.get('decision','?')}" for e in gk[-5:]))

    # Events / Findings / Anomalies
    print(_c("\n─ Signals ─", "bold", enabled))
    if events:
        print(f"events (last {len(events)}):")
        for e in events:
            print(f"  {e.get('type')} {str(e.get('data',{}))[:80]}")
    else:
        print("events: none")
    if findings:
        print(f"findings: {len(findings)} total, last: {findings[-1].get('finding','')[:80] if findings else ''}")
    else:
        print("findings: none")
    if anom:
        print(_c(f"anomalies: {len(anom)}", "yellow", enabled))
        for a in anom[-3:]:
            print(f"  [{a.get('severity','')}] {a.get('rule','')}: {a.get('message','')[:80]}")
    else:
        print("anomalies: none")

    # Logs health
    print(_c("\n─ Logs ─", "bold", enabled))
    try:
        logs = list(logs_dir.glob("*.log"))
        total_logs = sum(p.stat().st_size for p in logs if p.exists())
        print(f"log files {len(logs)}  total {total_logs/1024:.0f}KB  largest {[p.name for p in sorted(logs, key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)[:3]]}")
    except Exception:
        print("logs: unable to scan")

    # Recommendations
    print(_c("\n─ Doctor ─", "bold", enabled))
    issues: list[str] = []
    if orch.get("drain"):
        issues.append("DRAIN active — no new spawns")
    if stale:
        issues.append(f"{len(stale)} stale/dead agents — check orchestrator / heartbeat")
    if not orch.get("alive") and orch.get("pid"):
        issues.append("orchestrator pid dead — restart orchestrator")
    stale_claims = [c for c in claims if c["age"]>7200]
    if stale_claims:
        issues.append(f"{len(stale_claims)} stale claims (>2h) — leases may need retry/clear")
    if ledger and _collect_budget_state(ledger)[0] in ("warn","shed_tier3","stop"):
        issues.append(f"budget { _collect_budget_state(ledger)[0]} — throttling may occur")
    if not tickets:
        issues.append("no tickets — discovery may be idle")
    elif summary and summary.get("REWORK",0) > 3:
        issues.append(f"high rework {summary.get('REWORK')} — review gate failures")
    if not issues:
        print(_c("✓ healthy — no blocking issues detected", "green", enabled))
    else:
        for i, iss in enumerate(issues, 1):
            print(_c(f"  {i}. {iss}", "yellow", enabled))
    print()
    return 0


# ---------------------------------------------------------------------------
# Live dashboard
# ---------------------------------------------------------------------------

def _render_live_snapshot(project_root: Path, enabled: bool, ticker: int, interval: float, detail_lines: int = 0, view: str = "both") -> str:
    """Return a full live frame as string.

    view: "both" (default), "agents", or "tickets"
    """
    now = time.time()
    now_h = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    state_dir = _find_state_dir(project_root)
    agents = _collect_agents(project_root)
    _, summary, tickets = _collect_tickets(project_root)
    claims = _collect_claims(project_root)
    rl = _collect_rl(project_root)
    ledger = _collect_token_ledger(project_root)
    orch = _collect_orchestrator_info(project_root)
    thr = _collect_ticket_throughput(tickets)

    lines: list[str] = []
    # header
    proj = _find_project_name(project_root)
    drain_badge = _c(" ● DRAIN ", "red", enabled) if orch.get("drain") else _c(" ○ live ", "green", enabled)
    lines.append(_c(f" CodeBot BOTOP — {proj} — {now_h} — refresh {interval:.0f}s  (q quit, r refresh) ", "bold", enabled) + drain_badge + _c(f"  tick {ticker}", "dim", enabled))
    lines.append(f" {state_dir}  logs:{_find_logs_dir(project_root).name}/  interval {interval:.1f}s")

    # orchestrator bar
    pid = orch.get("pid")
    alive = orch.get("alive")
    ls_age = orch.get("last_spawn_age")
    ls_s = f"{ls_age:.0f}s ago" if isinstance(ls_age, (int,float)) else "?"
    paused_n = orch.get("paused_count",0)
    orch_s = f"orch pid {pid or '-'} {'alive' if alive else 'DOWN'}  last_spawn {ls_s}  hb {orch.get('heartbeat_count')} agents  paused {paused_n}"
    if orch.get("drain_text"):
        orch_s += f"  drain:{orch['drain_text'][:40]}"
    lines.append(_c("┌─ Orchestrator ─────────────────────────────────────────────────────────", "dim", enabled))
    lines.append(f"│ {orch_s}")
    lines.append(_c("└────────────────────────────────────────────────────────────────────", "dim", enabled))

    # agents table
    cnt = Counter(a["bucket"] for a in agents)
    lines.append(_c(f"┌─ Agents ({len(agents)})  RUN {cnt.get('RUNNING',0)}  STALE {cnt.get('STALE',0)}  DEAD {cnt.get('DEAD',0)}  PAUSED {cnt.get('PAUSED',0)} ──────────────────", "bold", enabled))
    if not agents:
        lines.append("│ No agents found.")
    else:
        lines.append(f"│ {'Agent':<24s} {'State':<10s} {'HB':>8s} {'LOG':>8s} {'IT':>4s} {'TASK':<20s} {'PID':>7s} {'ERR':>4s} {'MODEL':<20s}")
        lines.append(_c("│ " + "─"*101, "dim", enabled))
        for a in agents[:20]:
            bucket = _bucket_color(a["bucket"], enabled)
            hb = _age_str(a["hb_age"], enabled) if a["hb_age"] is not None else _c("-", "gray", enabled)
            loga = _age_str(a["log_age"], enabled) if a["log_age"] is not None else _c("-", "gray", enabled)
            task = (a["current_task"] or "-")[:20]
            pid_s = str(a["pid"]) if a["pid"] else "-"
            err = str(a["consecutive_errors"]) if a.get("consecutive_errors") not in (None, "") else "-"
            iter_s = str(a["iteration"]) if a["iteration"] else "-"
            name = a["name"][:24]
            model = (a["model"] or "-")[:20]
            lines.append(f"│ {name:<24s} {_ansi_pad(bucket, 10)} {_ansi_pad(hb, 8, 'right')} {_ansi_pad(loga, 8, 'right')} {iter_s:>4s} {task:<20s} {pid_s:>7s} {err:>4s} {model:<20s}")
        if len(agents) > 32:
            lines.append(f"│ ... +{len(agents)-20} more (use botop status --verbose)")
    lines.append(_c("└──────────────────────────────────────────────────────────────────────────", "dim", enabled))

    # tickets
    lines.append(_c(f"┌─ Tickets — total {thr['total']} ────────────────────────────────────────────────", "bold", enabled))
    if summary:
        states_line = "  ".join(f"{_state_color(k, enabled)}={v}" for k,v in summary.items())
        lines.append(f"│ {states_line}")
    if thr["total"]:
        lines.append(f"│ 24h +{thr['throughput_24h']}  7d +{thr['throughput_7d']}  avg age {thr['avg_age_h']}h  rework {thr['rework_rate']*100:.1f}%  oldest {thr['oldest_h']}h")
    # show top tickets per state (compact)
    def _tid(t):
        return (t.get("id") if isinstance(t, dict) else getattr(t,"id",""))  # type: ignore
    def _tstate(t):
        if isinstance(t, dict):
            return str(t.get("state",""))
        try:
            v = getattr(t,"state","")
            return v.value if hasattr(v,"value") else str(v)
        except Exception:
            return ""
    def _ttitle(t):
        if isinstance(t, dict):
            return str(t.get("title",""))[:42]
        return str(getattr(t,"title",""))[:42]
    def _tsev(t):
        if isinstance(t, dict):
            return str(t.get("severity",""))
        try:
            v = getattr(t,"severity","")
            return v.value if hasattr(v,"value") else str(v)
        except Exception:
            return ""
    for st_key in ["READY","IMPLEMENTING","REVIEWING","VERIFYING","COMPLETE"]:
        subset = [t for t in tickets if _tstate(t).upper()==st_key]
        if subset:
            sev_order = {"critical":0,"high":1,"medium":2,"low":3}
            if st_key=="READY":
                subset = sorted(subset, key=lambda x: sev_order.get(_tsev(x).lower(),99))
            lines.append(f"│ {st_key} ({len(subset)}):")
            for t in subset[:3]:
                sev = _tsev(t)
                sev_c = _severity_color(sev, enabled)
                lines.append(f"│   {_ansi_pad(sev_c, 18)} {_tid(t)[:18]:<18s} {_ttitle(t)}")
            if len(subset)>3:
                lines.append(f"│   ... +{len(subset)-3} more")
    lines.append(_c("└────────────────────────────────────────────────────────────────────", "dim", enabled))
    lines.append(_c(" botop live — q quit │ <enter> refresh │ botop term for interactive terminal │ botop health for full diagnostics", "dim", enabled))
    return "\n".join(lines)


def cmd_live(project_root: Path, interval: float = 2.0, once: bool = False, no_color: bool = False, json_out: bool = False) -> int:
    if json_out:
        # single snapshot as json
        agents = _collect_agents(project_root)
        _, summary, tickets = _collect_tickets(project_root)
        claims = _collect_claims(project_root)
        rl = _collect_rl(project_root)
        ledger = _collect_token_ledger(project_root)
        orch = _collect_orchestrator_info(project_root)
        thr = _collect_ticket_throughput(tickets)
        payload = {
            "project": str(project_root),
            "orchestrator": orch,
            "agents": [{k: v for k,v in a.items() if k not in ("ckpt","state","status","scratch")} for a in agents],
            "tickets": {"summary": summary, "throughput": thr},
            "claims": claims,
            "rl_global": (rl or {}).get("global") if rl else None,
            "budget_total": _collect_budget_state(ledger)[1] if ledger else None,
            "timestamp": time.time(),
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0

    enabled = _supports_color(no_color)
    ticker = 0
    interval = max(0.5, float(interval))
    # If stdout is not a TTY (piped), automatically behave like --once to prevent unbounded output
    if not sys.stdout.isatty():
        once = True
    if once:
        frame = _render_live_snapshot(project_root, enabled, ticker, interval)
        # don't clear screen in once mode
        print(frame)
        return 0

    # live loop
    # setup terminal raw mode for q detection? keep simple: blocking sleep with KeyboardInterrupt handling
    # Use select for non-blocking input if tty
    import select

    print(_c("Starting live dashboard — Ctrl-C or 'q' to quit", "dim", enabled), file=sys.stderr)
    time.sleep(0.2)
    while True:
        ticker += 1
        frame = _render_live_snapshot(project_root, enabled, ticker, interval)
        if sys.stdout.isatty():
            sys.stdout.write("\033[2J\033[H")
        else:
            sys.stdout.write("\n" + "="*80 + "\n")
        sys.stdout.write(frame + "\n")
        sys.stdout.flush()

        # wait interval with input poll for quit
        deadline = time.time() + interval
        while time.time() < deadline:
            if sys.stdin.isatty():
                # non-blocking check for 'q' + enter
                try:
                    rlist, _, _ = select.select([sys.stdin], [], [], 0.2)
                    if rlist:
                        line = sys.stdin.readline()
                        if line is None:
                            pass
                        else:
                            s = line.strip().lower()
                            if s in ("q", "quit", "exit"):
                                print(_c("\nQuit live", "dim", enabled))
                                return 0
                            # any enter forces immediate refresh
                            break
                except Exception:
                    time.sleep(0.2)
            else:
                time.sleep(0.2)
                break
            # if not tty, just sleep interval once
            if not sys.stdin.isatty():
                time.sleep(max(0, deadline - time.time()))
                break
        # loop continues — brief check for interrupt will be caught outer


def cmd_term(project_root: Path, no_color: bool = False) -> int:
    enabled = _supports_color(no_color)
    # try readline for history
    try:
        import readline  # type: ignore
        histfile = Path.home() / ".botop_history"
        try:
            readline.read_history_file(str(histfile))
        except Exception:
            pass
        readline.set_history_length(500)
    except Exception:
        readline = None  # type: ignore

    print(_c(f"\n══ CodeBot Interactive Terminal — {project_root.name} ══", "bold", enabled))
    print(_c("Type 'help' for commands, 'live' for dashboard, 'quit' to exit.", "dim", enabled))
    print(_c("Commands: status [verbose/json], tickets [ready/implementing/...], claims, throughput, metrics, budget, events, findings, leases, deadletters, gatekeeper, health/doctor, logs <agent> [--follow], restart/pause/resume <agent>, drain/clear-drain, live [--once], clear", "dim", enabled))
    print(f"state: {_find_state_dir(project_root)}  logs: {_find_logs_dir(project_root)}\n")

    def _help():
        print("""
botop term — interactive
  status [--verbose] [--json]          agent heartbeat + pid table
  tickets [--json] [--state STATE] [--limit N]
  claims [--json]
  throughput [--json]                  ticket flow + RL + budget
  metrics [--json]                     per-agent metrics
  budget [--json]
  events [--limit N] [--type TYPE] [--json]
  findings [--limit N] [--json]
  leases / deadletters [--json]
  gatekeeper [--json]
  health / doctor [--json]            full diagnostics
  logs <agent> [--lines N] [--follow]
  restart <agent>   pause <agent>   resume <agent>
  drain [--reason X]   clear-drain
  live [--interval S] [--once] [--json]  live dashboard (q to return)
  watch ≈ live
  clear                               clear screen
  help                                this help
  quit / exit / q                     leave terminal
  !<shell>                            shell escape (e.g. !ls .codebot/state)
""")

    while True:
        try:
            prompt = _c("botop> ", "cyan", enabled)
            line = input(prompt)
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            continue
        if line is None:
            break
        s = line.strip()
        if not s:
            continue
        if s.startswith("!"):
            # shell escape - use shell=False with shlex.split to prevent injection
            sh = s[1:].strip()
            if not sh:
                continue
            try:
                import shlex
                cmd = shlex.split(sh)
                subprocess.run(cmd, shell=False)
            except Exception as e:
                print(f"shell error: {e}", file=sys.stderr)
            continue
        low = s.lower()
        if low in ("quit", "exit", "q"):
            break
        if low == "help":
            _help()
            continue
        if low == "clear":
            sys.stdout.write("\033[2J\033[H")
            continue
        # parse command line like shell split
        import shlex
        try:
            parts = shlex.split(s)
        except ValueError as e:
            print(f"parse error: {e}", file=sys.stderr)
            continue
        if not parts:
            continue
        cmd = parts[0].lower()
        args = parts[1:]

        # dispatch
        try:
            if cmd in ("live", "watch", "top", "dash"):
                # parse live args inline
                iv = 2.0
                once = False
                jout = False
                nc = no_color
                i = 0
                while i < len(args):
                    if args[i] in ("--interval", "-i") and i+1 < len(args):
                        try: iv = float(args[i+1])
                        except: pass
                        i+=2
                    elif args[i] == "--once":
                        once = True; i+=1
                    elif args[i] == "--json":
                        jout=True; i+=1
                    elif args[i] == "--no-color":
                        nc=True; i+=1
                    else:
                        i+=1
                cmd_live(project_root, interval=iv, once=once, no_color=nc, json_out=jout)
            elif cmd == "status":
                verbose = "--verbose" in args or "-v" in args
                jout = "--json" in args
                cmd_status(project_root, verbose=verbose, json_out=jout)
            elif cmd == "tickets":
                jout = "--json" in args
                lim = 15
                st_filter = None
                if "--limit" in args:
                    try: lim = int(args[args.index("--limit")+1])
                    except: pass
                # allow `tickets ready` shorthand
                known_states = {"ready","implementing","reviewing","complete","discovered","rework","blocked","planning","verifying","triaged","deferred","rejected","duplicate","validating"}
                for a in args:
                    if a.lower() in known_states:
                        st_filter = a
                # also --state
                if "--state" in args:
                    try: st_filter = args[args.index("--state")+1]
                    except: pass
                cmd_tickets(project_root, json_out=jout, limit=lim, state_filter=st_filter)
            elif cmd == "claims":
                jout = "--json" in args
                cmd_claims(project_root, json_out=jout)
            elif cmd == "throughput":
                jout = "--json" in args
                cmd_throughput(project_root, json_out=jout)
            elif cmd in ("metrics", "metric"):
                jout = "--json" in args
                cmd_metrics(project_root, json_out=jout)
            elif cmd == "budget":
                jout = "--json" in args
                cmd_budget(project_root, json_out=jout)
            elif cmd == "events":
                jout = "--json" in args
                lim = 20
                typ = None
                if "--limit" in args:
                    try: lim = int(args[args.index("--limit")+1])
                    except: pass
                if "--type" in args:
                    try: typ = args[args.index("--type")+1]
                    except: pass
                cmd_events(project_root, limit=lim, json_out=jout, event_type=typ)
            elif cmd == "findings":
                jout = "--json" in args
                lim = 20
                if "--limit" in args:
                    try: lim = int(args[args.index("--limit")+1])
                    except: pass
                cmd_findings(project_root, limit=lim, json_out=jout)
            elif cmd in ("leases", "lease"):
                jout = "--json" in args
                cmd_leases(project_root, json_out=jout)
            elif cmd in ("deadletters", "dead-letters", "dead_letter"):
                jout = "--json" in args
                cmd_deadletters(project_root, json_out=jout)
            elif cmd in ("gatekeeper", "gates"):
                jout = "--json" in args
                cmd_gatekeeper(project_root, json_out=jout)
            elif cmd in ("health", "doctor", "diagnostics", "diag"):
                jout = "--json" in args
                cmd_health(project_root, json_out=jout)
            elif cmd == "logs":
                if not args:
                    print("usage: logs <agent> [--lines N] [--follow]", file=sys.stderr)
                    continue
                agent = args[0]
                lines = 50
                follow = False
                if "--lines" in args:
                    try: lines = int(args[args.index("--lines")+1])
                    except: pass
                if "--follow" in args or "-f" in args:
                    follow = True
                # also support `logs scheduler -f` style
                for a in args:
                    if a.isdigit():
                        try: lines = int(a); 
                        except: pass
                cmd_logs(project_root, agent, lines=lines, follow=follow)
            elif cmd == "restart":
                if not args:
                    print("usage: restart <agent>", file=sys.stderr); continue
                cmd_restart(project_root, args[0])
            elif cmd == "pause":
                if not args:
                    print("usage: pause <agent>", file=sys.stderr); continue
                cmd_pause(project_root, args[0])
            elif cmd == "resume":
                if not args:
                    print("usage: resume <agent>", file=sys.stderr); continue
                cmd_resume(project_root, args[0])
            elif cmd == "drain":
                reason = "manual"
                if "--reason" in args:
                    try: reason = args[args.index("--reason")+1]
                    except: pass
                elif args:
                    reason = " ".join(args)
                cmd_drain(project_root, reason=reason)
            elif cmd == "clear-drain":
                cmd_clear_drain(project_root)
            else:
                print(f"unknown command: {cmd} (try 'help')", file=sys.stderr)
        except KeyboardInterrupt:
            print(_c("\nInterrupted", "yellow", enabled))
        except SystemExit:
            # subcommands call sys.exit; catch to stay in terminal
            pass
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)

    # save history
    try:
        if 'readline' in locals() and readline is not None:
            readline.write_history_file(str(histfile))
    except Exception:
        pass
    print(_c("Bye.", "dim", enabled))
    return 0


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="CodeBot Operations CLI — status, logs, tickets, throughput, live dashboard, interactive terminal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  python -m codebot.botop status\n"
               "  python -m codebot.botop tickets --state READY\n"
               "  python -m codebot.botop live --interval 2\n"
               "  python -m codebot.botop term\n"
               "  python -m codebot.botop logs scheduler --follow\n"
               "  python -m codebot.botop health\n",
    )
    parser.add_argument("--project", type=str, default=".", help="Project root path")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    sub = parser.add_subparsers(dest="cmd")

    # status
    p_status = sub.add_parser("status", help="Show agent status")
    p_status.add_argument("--verbose", action="store_true", help="Show full table with model/task/err")
    p_status.add_argument("--json", action="store_true", help="JSON output")

    # claims
    p_claims = sub.add_parser("claims", help="Show active ticket claims")
    p_claims.add_argument("--json", action="store_true", help="JSON output")

    # tickets
    p_tickets = sub.add_parser("tickets", help="Show ticket store summary")
    p_tickets.add_argument("--json", action="store_true", help="JSON output")
    p_tickets.add_argument("--limit", type=int, default=15, help="Max tickets per state")
    p_tickets.add_argument("--state", type=str, default=None, help="Filter by state (READY, IMPLEMENTING, etc)")

    # throughput
    p_thr = sub.add_parser("throughput", help="Show throughput metrics")
    p_thr.add_argument("--json", action="store_true", help="JSON output")

    # metrics
    p_metrics = sub.add_parser("metrics", help="Show per-agent metrics")
    p_metrics.add_argument("--json", action="store_true", help="JSON output")

    # budget
    p_budget = sub.add_parser("budget", help="Show token budget")
    p_budget.add_argument("--json", action="store_true", help="JSON output")

    # events
    p_events = sub.add_parser("events", help="Show recent events")
    p_events.add_argument("--limit", type=int, default=20, help="Number of events")
    p_events.add_argument("--type", type=str, default=None, help="Filter by event type")
    p_events.add_argument("--json", action="store_true", help="JSON output")

    # findings
    p_findings = sub.add_parser("findings", help="Show recent findings")
    p_findings.add_argument("--limit", type=int, default=20, help="Number of findings")
    p_findings.add_argument("--json", action="store_true", help="JSON output")

    # leases
    p_leases = sub.add_parser("leases", help="Show lease state")
    p_leases.add_argument("--json", action="store_true", help="JSON output")

    # deadletters
    p_dl = sub.add_parser("deadletters", help="Show dead letters")
    p_dl.add_argument("--json", action="store_true", help="JSON output")
    # alias dead-letters
    p_dl2 = sub.add_parser("dead-letters", help="Show dead letters (alias)")
    p_dl2.add_argument("--json", action="store_true", help="JSON output")

    # gatekeeper
    p_gk = sub.add_parser("gatekeeper", help="Show gatekeeper log")
    p_gk.add_argument("--json", action="store_true", help="JSON output")

    # health / doctor / diagnostics (aliases)
    p_health = sub.add_parser("health", help="Show full diagnostics")
    p_health.add_argument("--json", action="store_true", help="JSON output")
    p_doctor = sub.add_parser("doctor", help="Show full diagnostics (alias for health)")
    p_doctor.add_argument("--json", action="store_true", help="JSON output")
    p_diag = sub.add_parser("diagnostics", help="Show full diagnostics (alias)")
    p_diag.add_argument("--json", action="store_true", help="JSON output")
    p_diag2 = sub.add_parser("diag", help="Show full diagnostics (alias)")
    p_diag2.add_argument("--json", action="store_true", help="JSON output")

    # logs
    logs_p = sub.add_parser("logs", help="Show agent logs")
    logs_p.add_argument("agent", help="Agent name")
    logs_p.add_argument("--lines", type=int, default=50, help="Number of lines")
    logs_p.add_argument("--follow", "-f", action="store_true", help="Follow log (tail -F)")
    logs_p.add_argument("--tasklog", action="store_true", help="Show tasklog instead of log")

    # restart/pause/resume
    restart_p = sub.add_parser("restart", help="Restart an agent")
    restart_p.add_argument("agent", help="Agent name")

    pause_p = sub.add_parser("pause", help="Pause an agent")
    pause_p.add_argument("agent", help="Agent name")

    resume_p = sub.add_parser("resume", help="Resume a paused agent")
    resume_p.add_argument("agent", help="Agent name")

    drain_p = sub.add_parser("drain", help="Set drain flag")
    drain_p.add_argument("--reason", type=str, default="manual", help="Drain reason")

    sub.add_parser("clear-drain", help="Clear drain flag")

    # live dashboard
    p_live = sub.add_parser("live", help="Live dashboard (auto-refresh)")
    p_live.add_argument("--interval", type=float, default=2.0, help="Refresh seconds")
    p_live.add_argument("--once", action="store_true", help="Single snapshot, no loop")
    p_live.add_argument("--json", action="store_true", help="JSON snapshot, no UI")
    p_live.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    for alias in ("watch", "top", "dash", "dashboard"):
        pa = sub.add_parser(alias, help=f"Alias for live")
        pa.add_argument("--interval", type=float, default=2.0, help="Refresh seconds")
        pa.add_argument("--once", action="store_true", help="Single snapshot")
        pa.add_argument("--json", action="store_true", help="JSON snapshot")
        pa.add_argument("--no-color", action="store_true", help="Disable ANSI colors")

    # terminal
    for tname in ("term", "terminal", "shell", "repl", "interactive"):
        pt = sub.add_parser(tname, help="Interactive terminal")
        pt.add_argument("--no-color", action="store_true", help="Disable ANSI colors")

    args = parser.parse_args()
    project_root = Path(args.project).resolve()
    # global no-color flag is used by helpers via _supports_color check of env/no_color param passed explicitly

    # if no command, show a clean hint and exit 0 (not error)
    if args.cmd is None:
        print(f"CodeBot Operations CLI — {_find_project_name(project_root)}", file=sys.stderr)
        print("Hint: try `botop live --once` for a snapshot, `botop term` for interactive terminal, or `botop health` for diagnostics.", file=sys.stderr)
        sys.exit(0)

    cmd = args.cmd

    # dispatch
    if cmd == "status":
        sys.exit(cmd_status(project_root, verbose=getattr(args, "verbose", False), json_out=getattr(args, "json", False)))
    elif cmd == "logs":
        # tasklog flag: switch file
        if getattr(args, "tasklog", False):
            # handle by direct file read hint: logs --tasklog just shows tasklog file via same cmd but we adjust
            # monkey: resolve to tasklog path inside cmd_logs? easiest: call with agent and swap logic inside?
            # For now, if tasklog requested, override by reading tasklog directly
            logs_dir = _find_logs_dir(project_root)
            log_file = logs_dir / f"{args.agent}.tasklog"
            if log_file.exists():
                # reuse cmd_logs but it already checks both; just call normally
                pass
        sys.exit(cmd_logs(project_root, args.agent, args.lines, follow=getattr(args, "follow", False)))
    elif cmd == "restart":
        sys.exit(cmd_restart(project_root, args.agent))
    elif cmd == "pause":
        sys.exit(cmd_pause(project_root, args.agent))
    elif cmd == "resume":
        sys.exit(cmd_resume(project_root, args.agent))
    elif cmd == "drain":
        sys.exit(cmd_drain(project_root, args.reason))
    elif cmd == "clear-drain":
        sys.exit(cmd_clear_drain(project_root))
    elif cmd == "claims":
        sys.exit(cmd_claims(project_root, json_out=getattr(args, "json", False)))
    elif cmd == "tickets":
        sys.exit(cmd_tickets(project_root, json_out=getattr(args, "json", False), limit=getattr(args, "limit", 15), state_filter=getattr(args, "state", None)))
    elif cmd == "throughput":
        sys.exit(cmd_throughput(project_root, json_out=getattr(args, "json", False)))
    elif cmd == "metrics":
        sys.exit(cmd_metrics(project_root, json_out=getattr(args, "json", False)))
    elif cmd == "budget":
        sys.exit(cmd_budget(project_root, json_out=getattr(args, "json", False)))
    elif cmd == "events":
        sys.exit(cmd_events(project_root, limit=getattr(args, "limit", 20), json_out=getattr(args, "json", False), event_type=getattr(args, "type", None)))
    elif cmd == "findings":
        sys.exit(cmd_findings(project_root, limit=getattr(args, "limit", 20), json_out=getattr(args, "json", False)))
    elif cmd in ("leases",):
        sys.exit(cmd_leases(project_root, json_out=getattr(args, "json", False)))
    elif cmd in ("deadletters", "dead-letters"):
        sys.exit(cmd_deadletters(project_root, json_out=getattr(args, "json", False)))
    elif cmd == "gatekeeper":
        sys.exit(cmd_gatekeeper(project_root, json_out=getattr(args, "json", False)))
    elif cmd in ("health", "doctor", "diagnostics", "diag"):
        sys.exit(cmd_health(project_root, json_out=getattr(args, "json", False)))
    elif cmd in ("live", "watch", "top", "dash", "dashboard"):
        sys.exit(cmd_live(project_root, interval=getattr(args, "interval", 2.0), once=getattr(args, "once", False), no_color=getattr(args, "no_color", False), json_out=getattr(args, "json", False)))
    elif cmd in ("term", "terminal", "shell", "repl", "interactive"):
        sys.exit(cmd_term(project_root, no_color=getattr(args, "no_color", False)))
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
