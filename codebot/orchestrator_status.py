"""Status reporting helpers extracted from orchestrator.py.

Provides human-readable and machine-readable bot status output.
"""
from __future__ import annotations

import time
from typing import Any


def get_status(
    bots: dict,
    *,
    batch_read_heartbeats_fn: Any,
    model_profile_fn: Any,
    effective_heartbeat_timeout_fn: Any,
) -> dict:
    """Return a dict of bot statuses for display or API."""
    out: dict[str, dict[str, Any]] = {}
    heartbeat_cache = batch_read_heartbeats_fn([b.config.name for b in bots.values()])
    for n, b in bots.items():
        pid = b.process.pid if b.process and b.process.poll() is None else None
        hb = heartbeat_cache.get(b.config.name, 0.0)
        ha = max(0.0, time.time() - hb) if hb > 0 else None
        pr = model_profile_fn(b.config.model)
        out[n] = {
            "enabled": b.config.enabled, "running": pid is not None, "pid": pid,
            "model": b.config.model, "risk": pr.lockup_risk if pr else "unknown",
            "eff_timeout": effective_heartbeat_timeout_fn(b),
            "heartbeat_age_seconds": round(ha, 1) if ha else None,
            "next_run_in": round(b.next_run_at - time.time(), 1) if b.next_run_at and not pid else None,
            "restart_count": b.restart_count, "consecutive_errors": b.consecutive_errors,
        }
    return out


def print_status(
    bots: dict,
    *,
    get_status_fn: Any,
) -> None:
    """Print a human-readable status table to stdout."""
    st = get_status_fn(bots)
    print("\n" + "=" * 90 + "\nBOT ORCHESTRATOR STATUS\n" + "=" * 90)
    for n, i in st.items():
        s = "RUNNING" if i["running"] else ("DISABLED" if not i["enabled"] else ("WAITING" if i["next_run_in"] and i["next_run_in"] > 0 else "STOPPED"))
        nxt_val = i.get("next_run_in")
        nxt_str = f"{nxt_val:.0f}s" if nxt_val and nxt_val > 0 else "-"
        hb_str = f"{i['heartbeat_age_seconds']}s" if i['heartbeat_age_seconds'] else "-"
        print(f"  {n:15s} {s:9s} PID={str(i['pid'] or '-'):>6s} HB={hb_str:>7s} NEXT={nxt_str:>6s} eff={i['eff_timeout']:4.0f}s risk={i['risk']:11s} {i['model']}")
    print("=" * 90 + "\n")
