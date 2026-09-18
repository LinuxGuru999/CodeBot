#!/usr/bin/env python3
"""Unified per-bot metrics collector for RSI/RL strategy evaluation.

Purpose
-------
Aggregates every available signal source into one per-bot snapshot so the
RSI (self-improvement) and RL (bandit prompt optimization) strategies can
be evaluated from a single file. Supplements (never replaces) the existing
rl_state.json, measurements.json, and token_ledger.json pipelines.

Metric dimensions (all bots, all sources):
  execution  — runs, completions, errors, timeouts, restarts, avg iterations
  tokens     — prompt/completion actuals per model, cost proxy
  alignment  — RL reward, avg reward, score, epsilon, Q-values, triggers
  progress   — tasklog lines, scratchpad lines, checkpoint recency, findings
  quality    — FP rate, coverage %, error rate from measurements.json
  liveness   — heartbeat age, log age, process alive, next run ETA

Why
---
Current signals are scattered across 7+ files with different schemas:
rl_state.json (rewards), token_ledger.json (tokens), measurements.json
(quality), *.status.json (liveness), *.tasklog (progress),
*.scratchpad.md (context), alignment checkpoints (throughput). No single
view exists to answer "is bot X improving?" — this collector builds it.

Invariants
----------
- stdlib-only; read-only (never mutates state files).
- Fail-open per source: one corrupt file skips that source, not the bot.
- Output: state/bot_metrics.json (atomic tmp->replace) + bot_metrics_history.jsonl.
- Bounded: tasklog tail 200 lines scanned, scratchpad tail 100 lines.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BOTS_DIR = Path(__file__).parent
STATE_DIR = BOTS_DIR / "state"
LOGS_DIR = BOTS_DIR / "logs"
METRICS_FILE = STATE_DIR / "bot_metrics.json"
HISTORY_FILE = STATE_DIR / "bot_metrics_history.jsonl"
MAX_HISTORY_LINES = 5000

_NON_WORKER_BOTS = [
    "issues", "features", "bug_triage",
    "goal_steering", "ui_improve",
    "doc_sync", "test_coverage", "code_quality", "prompt_opt", "dependency",
    "github_bot", "build",
    "e2e_smoke", "security_auditor", "release", "alignment",
    "feature_decomposer",
]


def _read_json(path: Path) -> dict | None:
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _get_bots_with_fresh_heartbeats(max_age_s: int = 300) -> list[str]:
    """Return bots with heartbeat file modified within max_age_s (default 5 min)."""
    now = time.time()
    fresh: list[str] = []
    for hp in STATE_DIR.glob("*.heartbeat"):
        try:
            if hp.stat().st_mtime > now - max_age_s:
                fresh.append(hp.stem)
        except OSError:
            pass
    return fresh


def _discover_workers() -> list[str]:
    workers: set[str] = set()
    try:
        from orchestrator import WORKER_POOL
        workers.update(WORKER_POOL)
    except Exception:
        pass
    for p in STATE_DIR.glob("worker-*.heartbeat"):
        name = p.stem
        if name.startswith("worker-"):
            try:
                int(name.split("-")[1])
                workers.add(name)
            except ValueError:
                pass
    for p in STATE_DIR.glob("worker-*.checkpoint.json"):
        name = p.stem.replace(".checkpoint", "")
        if name.startswith("worker-"):
            try:
                int(name.split("-")[1])
                workers.add(name)
            except ValueError:
                pass
    rl = _read_json(STATE_DIR / "rl_state.json")
    if rl:
        for name in (rl.get("bots") or {}):
            if isinstance(name, str) and name.startswith("worker-"):
                try:
                    int(name.split("-")[1])
                    workers.add(name)
                except ValueError:
                    pass
    if not workers:
        workers = {f"worker-{i}" for i in range(1, 13)}
    return sorted(workers, key=lambda n: int(n.split("-")[1]))


KNOWN_BOTS = _NON_WORKER_BOTS + _discover_workers()


def _collect_execution(bot: str, now: float | None = None) -> dict[str, Any]:
    """Runs, completions, errors, restarts, iterations from checkpoints + exit events."""
    runs = 0
    completed = 0
    errors = 0
    restarts = 0
    iterations: list[int] = []
    last_reason = ""
    for suffix in ("", "_iter79_manager", "_manager"):
        ckpt = _read_json(STATE_DIR / f"{bot}.checkpoint.json")
        if ckpt is None:
            continue
        runs += 1
        reason = str(ckpt.get("reason", ""))
        last_reason = reason or last_reason
        if reason in ("completed", "done"):
            completed += 1
        if "error" in reason or "failed" in reason:
            errors += 1
        it = ckpt.get("scan_iteration") or ckpt.get("tool_iterations") or 0
        try:
            iterations.append(int(it))
        except Exception:
            pass
        break
    for ev in (STATE_DIR / "alignment_events").glob(f"{bot}*.exit.json"):
        data = _read_json(ev)
        if data is None:
            continue
        runs += 1
        code = data.get("exit_code")
        if code == 0:
            completed += 1
        elif code is not None:
            errors += 1
        if "restart" in str(data.get("exit_reason", "")).lower():
            restarts += 1
    return {
        "runs": runs,
        "completed": completed,
        "errors": errors,
        "restarts": restarts,
        "completion_rate": round(completed / runs, 4) if runs else 0.0,
        "error_rate": round(errors / runs, 4) if runs else 0.0,
        "max_iterations": max(iterations) if iterations else 0,
        "last_reason": last_reason[:100],
    }


def _collect_alignment(bot: str) -> dict[str, Any]:
    """RL reward, score, epsilon, Q-values, triggers from rl_state + alignment files."""
    out: dict[str, Any] = {
        "avg_reward": 0.0, "last_reward": None, "last_score": None,
        "total_runs": 0, "epsilon": None, "q_arms": 0, "trigger_active": False,
    }
    rl = _read_json(STATE_DIR / "rl_state.json")
    if rl:
        bs = (rl.get("bots") or {}).get(bot, {})
        if bs:
            out["avg_reward"] = float(bs.get("avg_reward", 0.0))
            out["last_reward"] = bs.get("last_reward")
            out["last_score"] = bs.get("last_score")
            out["total_runs"] = int(bs.get("total_runs", 0))
            out["epsilon"] = bs.get("epsilon")
            out["q_arms"] = len(bs.get("q_values", {}) or {})
    scores = _read_json(STATE_DIR / "alignment_scores.json")
    if scores:
        entry = scores.get(bot) or (scores.get("bots") or {}).get(bot)
        if isinstance(entry, dict):
            out["alignment_score"] = entry.get("score")
        elif isinstance(entry, (int, float)):
            out["alignment_score"] = entry
    trig = STATE_DIR / "alignment_triggers" / f"{bot}.evolve.json"
    out["trigger_active"] = trig.exists()
    return out


def _collect_progress(bot: str, now: float) -> dict[str, Any]:
    """Tasklog/scratchpad/checkpoint recency + findings output."""
    tasklog_lines = 0
    tasklog_last = ""
    tasklog_age_s: float | None = None
    try:
        tp = LOGS_DIR / f"{bot}.tasklog"
        if tp.exists():
            lines = tp.read_text(encoding="utf-8").splitlines()
            tasklog_lines = len(lines)
            if lines:
                tasklog_last = lines[-1][:200]
            tasklog_age_s = round(now - tp.stat().st_mtime, 1)
    except Exception:
        pass
    scratch_lines = 0
    try:
        sp = STATE_DIR / f"{bot}.scratchpad.md"
        if sp.exists():
            scratch_lines = len(sp.read_text(encoding="utf-8").splitlines())
    except Exception:
        pass
    ckpt_age_s: float | None = None
    try:
        cp = STATE_DIR / f"{bot}.checkpoint.json"
        if cp.exists():
            ckpt_age_s = round(now - cp.stat().st_mtime, 1)
    except Exception:
        pass
    findings = 0
    for cand in (STATE_DIR / "docs", BOTS_DIR / "docs"):
        pass
    for pattern in (f"{bot}*.md", f"*{bot}*.md"):
        try:
            for f in (BOTS_DIR / "docs").rglob(pattern):
                if f.is_file():
                    findings += 1
        except Exception:
            pass
    return {
        "tasklog_lines": tasklog_lines,
        "tasklog_last": tasklog_last,
        "tasklog_age_s": tasklog_age_s,
        "scratchpad_lines": scratch_lines,
        "checkpoint_age_s": ckpt_age_s,
        "output_files": findings,
    }


def _collect_liveness(bot: str, now: float) -> dict[str, Any]:
    """Heartbeat age, log age, status freshness from status/heartbeat/log files."""
    hb_age: float | None = None
    try:
        hp = STATE_DIR / f"{bot}.heartbeat"
        if hp.exists():
            txt = hp.read_text(encoding="utf-8").strip().split()[0]
            hb_age = round(now - float(txt), 1)
    except Exception:
        pass
    log_age: float | None = None
    try:
        lp = LOGS_DIR / f"{bot}.log"
        if lp.exists():
            log_age = round(now - lp.stat().st_mtime, 1)
    except Exception:
        pass
    status = _read_json(STATE_DIR / f"{bot}.status.json") or {}
    status_age: float | None = None
    try:
        ua = status.get("updated_at", 0)
        if ua:
            status_age = round(now - float(ua), 1)
    except Exception:
        pass
    return {
        "heartbeat_age_s": hb_age,
        "log_age_s": log_age,
        "status_age_s": status_age,
        "current_task": str(status.get("current_task", ""))[:100],
        "iteration": status.get("iteration", 0),
    }


def _collect_quality(bot: str, measurements: dict | None) -> dict[str, Any]:
    """FP rate, coverage, error rate with fallbacks when primary sources are absent."""
    out: dict[str, Any] = {"fp_rate": None, "coverage_pct": None, "meas_error_rate": None, "findings": 0, "fp_source": "none"}
    if measurements:
        try:
            fp = (measurements.get("false_positive_rate") or {}).get(bot)
            if fp is not None:
                out["fp_rate"] = float(fp)
                out["fp_source"] = "measurements"
            cov = (measurements.get("coverage") or {}).get(bot, {})
            if cov.get("coverage_pct") is not None:
                out["coverage_pct"] = cov.get("coverage_pct")
            mer = (measurements.get("error_rate") or {}).get(bot)
            if mer is not None:
                out["meas_error_rate"] = float(mer)
            out["findings"] = int((measurements.get("findings_per_scan") or {}).get(bot, 0))
        except Exception:
            pass
    if out["fp_rate"] is None:
        try:
            rl = _read_json(STATE_DIR / "rl_state.json")
            bs = ((rl or {}).get("bots") or {}).get(bot, {})
            fails = int(bs.get("failures", 0))
            total = int(bs.get("total_runs", 0))
            if total > 0:
                out["fp_rate"] = round(fails / total, 4)
                out["fp_source"] = "rl_failures"
            else:
                out["fp_rate"] = 0.0
                out["fp_source"] = "no_runs"
        except Exception:
            out["fp_rate"] = 0.0
            out["fp_source"] = "fallback"
    return out


def _collect_tokens(ledger: dict | None) -> dict[str, int]:
    """Total prompt/completion actuals from token_ledger.json (fleet-wide)."""
    total_p = total_c = 0
    if ledger:
        for row in (ledger.get("by_model") or {}).values():
            try:
                total_p += int(row.get("prompt_actual", 0))
                total_c += int(row.get("completion_actual", 0))
            except Exception:
                pass
    return {"fleet_prompt_actual": total_p, "fleet_completion_actual": total_c}


def _collect_bot_tokens(bot: str) -> dict[str, Any]:
    """Per-bot token attribution from logs/{bot}.stream.json usage block."""
    out: dict[str, Any] = {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "model": None, "api_calls": 0, "source": "none",
    }
    try:
        sp = LOGS_DIR / f"{bot}.stream.json"
        if not sp.exists():
            return out
        data = json.loads(sp.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return out
        usage = data.get("usage")
        if isinstance(usage, dict):
            try:
                out["prompt_tokens"] = int(usage.get("prompt_tokens", 0))
                out["completion_tokens"] = int(usage.get("completion_tokens", 0))
                out["total_tokens"] = int(usage.get("total_tokens", out["prompt_tokens"] + out["completion_tokens"]))
                out["source"] = "stream_usage"
            except Exception:
                pass
        model = data.get("model")
        if isinstance(model, str) and model:
            out["model"] = model
        try:
            out["api_calls"] = int(data.get("api_calls", 0))
        except Exception:
            pass
        if out["total_tokens"] == 0:
            try:
                msgs = data.get("messages") or []
                chars = sum(len(str((m or {}).get("content", ""))) for m in msgs if isinstance(m, dict))
                out["prompt_tokens"] = chars // 4
                out["total_tokens"] = out["prompt_tokens"]
                out["source"] = "char_estimate"
            except Exception:
                pass
    except Exception:
        pass
    return out


def _collect_throughput(bot: str) -> dict[str, Any]:
    """Items claimed vs completed, stale age, reopen rate proxy."""
    claimed = completed = 0
    stale_age_s: float | None = None
    try:
        for cand in (BOTS_DIR.parent / "docs" / "triage" / "QUEUE.md",
                     BOTS_DIR / "docs" / "triage" / "QUEUE.md"):
            if not cand.exists():
                continue
            text = cand.read_text(encoding="utf-8")
            import re as _re
            for m in _re.finditer(r"\|\s*(Q-\d+|QUEUE-[A-Z]+-\d+)\s*\|[^|]*\|[^|]*\|[^|]*\|([^|]*)\|([^|]*)\|", text):
                status = (m.group(3) or "").strip().lower()
                assignee = (m.group(2) or "").strip().lower()
                if bot.replace("_", "") in assignee or bot in assignee:
                    claimed += 1
                    if status in ("implemented", "done", "completed", "shipped"):
                        completed += 1
            break
    except Exception:
        pass
    try:
        cp = STATE_DIR / f"{bot}.checkpoint.json"
        if cp.exists():
            import time as _t
            stale_age_s = round(_t.time() - cp.stat().st_mtime, 1)
    except Exception:
        pass
    rate = round(completed / claimed, 4) if claimed else 0.0
    return {"claimed": claimed, "completed": completed, "completion_rate": rate,
            "abandoned": max(0, claimed - completed), "checkpoint_age_s": stale_age_s}


def _collect_output_quality(bot: str) -> dict[str, Any]:
    """Build-gate pass proxy, lint delta proxy, precision from triage outcomes."""
    gate_pass: bool | None = None
    try:
        for cand in (BOTS_DIR / "docs" / "optimization" / "build-gate-INDEX.md",
                     BOTS_DIR.parent / "docs" / "optimization" / "build-gate-INDEX.md"):
            if cand.exists():
                txt = cand.read_text(encoding="utf-8")
                if "FAIL" in txt.upper():
                    gate_pass = False
                elif "PASS" in txt.upper() or "GREEN" in txt.upper():
                    gate_pass = True
                break
    except Exception:
        pass
    precision: float | None = None
    dup_rate: float | None = None
    try:
        rl = _read_json(STATE_DIR / "rl_state.json")
        bs = ((rl or {}).get("bots") or {}).get(bot, {})
        succ = int(bs.get("successes", 0))
        fail = int(bs.get("failures", 0))
        tot = succ + fail
        if tot > 0:
            precision = round(succ / tot, 4)
    except Exception:
        pass
    return {"build_gate_pass": gate_pass, "finding_precision": precision, "duplicate_rate": dup_rate}


def _collect_economics(bot: str, tok: dict[str, Any], exe: dict[str, Any]) -> dict[str, Any]:
    """Tokens per completion, noop rate, model price proxy."""
    MODEL_PRICE_PER_1K = {
        "qwen-3.8-max": 0.002, "qwen-3.8-max-thinking": 0.004,
        "qwen-3.7-plus": 0.0015, "qwen-3.7-max-thinking": 0.003,
        "qwen-3.6-plus": 0.001, "xiaomi-mimo-2.5": 0.0005,
        "meta-muse-spark-1.3": 0.001, "meta-muse-spark-1.3": 0.001,
    }
    total = int(tok.get("total_tokens", 0))
    done = max(1, int(exe.get("completed", 0)))
    model = str(tok.get("model") or "")
    price = MODEL_PRICE_PER_1K.get(model, 0.001)
    noop = 0.0
    try:
        ckpt = _read_json(STATE_DIR / f"{bot}.checkpoint.json") or {}
        if str(ckpt.get("reason", "")).lower().startswith("noop"):
            noop = 1.0
    except Exception:
        pass
    return {
        "tokens_per_completion": round(total / done, 1),
        "est_cost_usd": round(total / 1000.0 * price, 4),
        "model": model or None,
        "noop_flag": noop,
    }


def _collect_autonomy(bot: str, exe: dict[str, Any]) -> dict[str, Any]:
    """Human interventions, stuck restarts, sandbox denials, failure streaks."""
    interventions = 0
    stuck_restarts = 0
    sandbox_denied = 0
    try:
        hq = _read_json(STATE_DIR / "human_review_queue.json") or []
        if isinstance(hq, list):
            interventions = sum(1 for e in hq if isinstance(e, dict) and e.get("bot") == bot)
    except Exception:
        pass
    try:
        for ev in (STATE_DIR / "alignment_events").glob(f"{bot}*.exit.json"):
            d = _read_json(ev) or {}
            if d.get("exit_reason") == "stuck":
                stuck_restarts += 1
    except Exception:
        pass
    streak = 0
    try:
        rl = _read_json(STATE_DIR / "rl_state.json")
        streak = int((((rl or {}).get("bots") or {}).get(bot, {})).get("consecutive_failures", 0))
    except Exception:
        pass
    return {"human_interventions": interventions, "stuck_restarts": stuck_restarts,
            "sandbox_denied": sandbox_denied, "failure_streak": streak,
            "auto_disabled": streak >= 3}


def _collect_learning(bot: str, ali: dict[str, Any]) -> dict[str, Any]:
    """Reward slope proxy, epsilon, Q-spread, trigger latency proxy."""
    eps = ali.get("epsilon")
    q_arms = int(ali.get("q_arms", 0))
    q_spread: float | None = None
    try:
        rl = _read_json(STATE_DIR / "rl_state.json")
        qv = (((rl or {}).get("bots") or {}).get(bot, {}).get("q_values") or {})
        vals = [float(v) for v in qv.values()]
        if len(vals) >= 2:
            q_spread = round(max(vals) - min(vals), 4)
    except Exception:
        pass
    return {"epsilon": eps, "q_arms": q_arms, "q_spread": q_spread,
            "needs_exploration_reset": bool(eps is not None and float(eps) < 0.05)}


def _collect_scrutiny() -> dict[str, Any]:
    flagged: list[str] = []
    try:
        lp = STATE_DIR / "scrutiny_log.jsonl"
        if lp.exists():
            for line in lp.read_text(encoding="utf-8").splitlines()[-500:]:
                try:
                    e = json.loads(line)
                    iid = e.get("item_id")
                    if iid:
                        flagged.append(str(iid))
                except Exception:
                    continue
    except Exception:
        pass
    seen: dict[str, None] = {}
    for iid in flagged:
        seen[iid] = None
    unique = list(seen.keys())
    approved: set[str] = set()
    try:
        ap = STATE_DIR / "approved_ids.json"
        if ap.exists():
            data = json.loads(ap.read_text(encoding="utf-8"))
            if isinstance(data, list):
                approved = {str(x) for x in data}
            elif isinstance(data, dict):
                approved = {str(k) for k, v in data.items() if v}
    except Exception:
        pass
    return {
        "flagged_items": unique[-50:],
        "flagged_count": len(unique),
        "human_approved_count": len(approved),
        "mode": "scrutiny-never-blocks",
    }


def collect_all(only_fresh: bool = False) -> dict[str, Any]:
    now = time.time()
    measurements = _read_json(STATE_DIR / "measurements.json")
    ledger = _read_json(STATE_DIR / "token_ledger.json")
    fleet_tokens = _collect_tokens(ledger)
    bots: dict[str, Any] = {}
    active = idle = stale = erroring = improving = regressing = 0
    if only_fresh:
        targets = set(KNOWN_BOTS) & set(_get_bots_with_fresh_heartbeats()) or set(KNOWN_BOTS)
    else:
        targets = set(KNOWN_BOTS)
    for bot in KNOWN_BOTS:
        if bot not in targets:
            continue
        exe = _collect_execution(bot)
        ali = _collect_alignment(bot)
        pro = _collect_progress(bot, now)
        liv = _collect_liveness(bot, now)
        qua = _collect_quality(bot, measurements)
        lr = ali.get("last_reward")
        ar = ali.get("avg_reward", 0.0)
        trend = "flat"
        if isinstance(lr, (int, float)):
            if lr > ar + 0.05:
                trend = "improving"
                improving += 1
            elif lr < ar - 0.05:
                trend = "regressing"
                regressing += 1
        hb = liv.get("heartbeat_age_s")
        if hb is not None and hb < 300:
            active += 1
        elif hb is not None and hb < 3600:
            idle += 1
        elif hb is not None:
            stale += 1
        if exe["error_rate"] > 0.5 and exe["runs"] >= 2:
            erroring += 1
        tok = _collect_bot_tokens(bot)
        bots[bot] = {
            "execution": exe,
            "alignment": ali,
            "progress": pro,
            "liveness": liv,
            "quality": qua,
            "tokens": tok,
            "throughput": _collect_throughput(bot),
            "output_quality": _collect_output_quality(bot),
            "economics": _collect_economics(bot, tok, exe),
            "autonomy": _collect_autonomy(bot, exe),
            "learning": _collect_learning(bot, ali),
            "reward_trend": trend,
        }
    snapshot = {
        "timestamp": now,
        "timestamp_human": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "fleet_tokens": fleet_tokens,
        "ledger_day": (ledger or {}).get("day_utc"),
        "ledger_total_actual": (ledger or {}).get("total_actual", 0),
        "bots": bots,
        "scrutiny": _collect_scrutiny(),
        "summary": {
            "bots_tracked": len(KNOWN_BOTS),
            "active": active,
            "idle": idle,
            "stale": stale,
            "erroring": erroring,
            "improving": improving,
            "regressing": regressing,
        },
    }
    return snapshot


def save_snapshot(snapshot: dict[str, Any]) -> None:
    """Atomic write to bot_metrics.json + append to history JSONL."""
    tmp = Path(str(METRICS_FILE) + ".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    tmp.replace(METRICS_FILE)
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot) + "\n")
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
        if len(lines) > MAX_HISTORY_LINES:
            HISTORY_FILE.write_text("\n".join(lines[-MAX_HISTORY_LINES:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def main() -> None:
    import sys
    only_fresh = "--incremental" in sys.argv or "--only-fresh" in sys.argv
    snap = collect_all(only_fresh=only_fresh)
    save_snapshot(snap)
    s = snap["summary"]
    print(f"=== Bot Metrics @ {snap['timestamp_human']} ===")
    print(f"Bots: {s['bots_tracked']} tracked | {s['active']} active | {s['idle']} idle | {s['stale']} stale | {s['erroring']} erroring")
    print(f"RL trend: {s['improving']} improving | {s['regressing']} regressing")
    print(f"Tokens today: {snap['ledger_total_actual']:,} ({snap['ledger_day']})")
    print(f"Saved to: {METRICS_FILE}")


if __name__ == "__main__":
    main()
