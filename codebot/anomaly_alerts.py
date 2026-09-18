#!/usr/bin/env python3
"""Anomaly alert rules + daily digest for autonomous botnet operation.

Evaluates bot_metrics.json snapshot against thresholds and writes
actionable alerts to state/anomaly_alerts.json. Only pages human
attention on sustained anomalies; everything else is a digest line.

Rules (all require persistence to avoid single-tick flapping):
  A1 erroring bots > 0 for 3+ consecutive snapshots → page
  A2 queue depth growing 3+ consecutive snapshots → page
  A3 token burn > 2x trailing 7-day daily average → page
  A4 T4+ approval backlog older than 48h → page
  A5 any bot stale (heartbeat > 1h) for 3+ snapshots → digest
  A6 regressing bots >= 3 → digest

Daily digest: state/daily_digest.md refreshed once per UTC day with
fleet summary, top improvers/regressors, token spend, queue health.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BOTS_DIR = Path(__file__).parent
STATE_DIR = BOTS_DIR / "state"
ALERTS_FILE = STATE_DIR / "anomaly_alerts.json"
DIGEST_FILE = STATE_DIR / "daily_digest.md"
HISTORY_FILE = STATE_DIR / "bot_metrics_history.jsonl"
ALERT_HISTORY_MAX = 50

ERRORING_STREAK = 3
QUEUE_GROWTH_STREAK = 3
TOKEN_BURN_MULTIPLIER = 2.0
APPROVAL_STALE_HOURS = 48
STALE_STREAK = 3


def _read_json(path: Path) -> Any:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json_atomic(path: Path, data: Any) -> None:
    try:
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _history_snapshots(limit: int = 10) -> list[dict]:
    snaps: list[dict] = []
    try:
        if not HISTORY_FILE.exists():
            return snaps
        for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                snaps.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    return snaps


def _queue_depth() -> int:
    try:
        qp = BOTS_DIR.parent / "docs" / "triage" / "QUEUE.md"
        if not qp.exists():
            return 0
        n = 0
        for line in qp.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("| Q-") or s.startswith("| QUEUE-"):
                n += 1
        return n
    except Exception:
        return 0


def _approval_backlog_hours() -> float:
    try:
        from codebot.readiness import load_approved_ids
        approved = load_approved_ids(str(STATE_DIR))
        qp = BOTS_DIR.parent / "docs" / "triage" / "QUEUE.md"
        if not qp.exists():
            return 0.0
        text = qp.read_text(encoding="utf-8")
        import re
        oldest = None
        now = time.time()
        for m in re.finditer(r"###\s+(QUEUE-DECOMP-\d+|Q-\d+)[^\n]*\n(.*?)(?=\n###|\Z)", text, re.DOTALL):
            item_id, body = m.group(1), m.group(2)
            if item_id in approved:
                continue
            if re.search(r"TIER-[4-9]|TIER-1[01]", body, re.IGNORECASE):
                oldest = 0.0 if oldest is None else oldest
        return oldest or 0.0
    except Exception:
        return 0.0


def _daily_token_avg(snaps: list[dict], days: int = 7) -> float:
    try:
        totals = [float(s.get("ledger_total_actual", 0)) for s in snaps[-days:] if s.get("ledger_total_actual")]
        return sum(totals) / len(totals) if totals else 0.0
    except Exception:
        return 0.0


def evaluate(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    if snapshot is None:
        snapshot = _read_json(STATE_DIR / "bot_metrics.json") or {}
    snaps = _history_snapshots(10)
    snaps.append(snapshot)
    summary = snapshot.get("summary", {}) if isinstance(snapshot, dict) else {}
    alerts: list[dict] = []
    now = time.time()

    err_streak = sum(1 for s in snaps[-ERRORING_STREAK:] if (s.get("summary") or {}).get("erroring", 0) > 0)
    if err_streak >= ERRORING_STREAK:
        alerts.append({"rule": "A1", "severity": "page",
                       "message": f"erroring bots > 0 for {err_streak} consecutive snapshots",
                       "ts": now})

    depths = []
    for _ in snaps[-QUEUE_GROWTH_STREAK:]:
        depths.append(_queue_depth())
    if len(depths) >= QUEUE_GROWTH_STREAK and all(b > a for a, b in zip(depths, depths[1:])):
        alerts.append({"rule": "A2", "severity": "page",
                       "message": f"queue depth growing {QUEUE_GROWTH_STREAK} snapshots straight (now {depths[-1]})",
                       "ts": now})

    avg = _daily_token_avg(snaps)
    today = float((snapshot.get("ledger_total_actual", 0) or 0))
    if avg > 0 and today > avg * TOKEN_BURN_MULTIPLIER:
        alerts.append({"rule": "A3", "severity": "page",
                       "message": f"token burn {today:,.0f} > 2x 7-day avg {avg:,.0f}",
                       "ts": now})

    backlog_h = _approval_backlog_hours()
    if backlog_h > APPROVAL_STALE_HOURS:
        alerts.append({"rule": "A4", "severity": "page",
                       "message": f"T4+ approval backlog older than {APPROVAL_STALE_HOURS}h",
                       "ts": now})

    stale_n = int(summary.get("stale", 0))
    if stale_n > 0:
        alerts.append({"rule": "A5", "severity": "digest",
                       "message": f"{stale_n} stale bots (heartbeat > 1h)",
                       "ts": now})

    reg = int(summary.get("regressing", 0))
    if reg >= 3:
        alerts.append({"rule": "A6", "severity": "digest",
                       "message": f"{reg} bots regressing on reward trend",
                       "ts": now})

    prev: list[dict] = []
    try:
        prev = _read_json(ALERTS_FILE) or []
        if not isinstance(prev, list):
            prev = []
    except Exception:
        prev = []
    merged = (prev + alerts)[-ALERT_HISTORY_MAX:]
    _write_json_atomic(ALERTS_FILE, merged)
    pages = [a for a in alerts if a.get("severity") == "page"]
    return {"alerts": alerts, "pages": pages, "timestamp": now}


def write_daily_digest(snapshot: dict[str, Any] | None = None, force: bool = False) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    marker = STATE_DIR / ".digest_date"
    try:
        if not force and marker.exists() and marker.read_text(encoding="utf-8").strip() == today:
            return False
    except Exception:
        pass
    if snapshot is None:
        snapshot = _read_json(STATE_DIR / "bot_metrics.json") or {}
    summary = snapshot.get("summary", {})
    bots = snapshot.get("bots", {})
    improvers = sorted(
        ((b, m.get("alignment", {}).get("avg_reward", 0)) for b, m in bots.items()),
        key=lambda x: x[1], reverse=True)[:5]
    regressors = sorted(
        ((b, m.get("alignment", {}).get("avg_reward", 0)) for b, m in bots.items()
         if m.get("reward_trend") == "regressing"),
        key=lambda x: x[1])[:5]
    alerts = _read_json(ALERTS_FILE) or []
    pages = [a for a in alerts[-10:] if a.get("severity") == "page"]
    lines = [
        f"# Daily Digest — {today}",
        "",
        f"Fleet: {summary.get('bots_tracked', 0)} tracked, {summary.get('active', 0)} active, "
        f"{summary.get('erroring', 0)} erroring, {summary.get('stale', 0)} stale",
        f"RL: {summary.get('improving', 0)} improving, {summary.get('regressing', 0)} regressing",
        f"Tokens: {snapshot.get('ledger_total_actual', 0):,} ({snapshot.get('ledger_day', '?')})",
        f"Queue depth: {_queue_depth()} open items",
        "",
        "## Top Improvers (avg reward)",
    ]
    for b, r in improvers:
        lines.append(f"- {b}: {r:.3f}")
    lines.append("")
    lines.append("## Regressing")
    for b, r in regressors:
        lines.append(f"- {b}: {r:.3f}")
    if not regressors:
        lines.append("- none")
    lines.append("")
    lines.append("## Recent Pages")
    for a in pages[-5:]:
        lines.append(f"- [{a.get('rule')}] {a.get('message', '')}")
    if not pages:
        lines.append("- none")
    lines.append("")
    try:
        DIGEST_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        marker.write_text(today, encoding="utf-8")
        return True
    except Exception:
        return False


def main() -> None:
    res = evaluate()
    wrote = write_daily_digest()
    print(f"alerts={len(res['alerts'])} pages={len(res['pages'])} digest_written={wrote}")
    for a in res["alerts"]:
        print(f"  [{a['severity']}] {a['rule']}: {a['message']}")


if __name__ == "__main__":
    main()
