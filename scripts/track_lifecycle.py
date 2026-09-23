#!/usr/bin/env python3
"""Lifecycle tracker for CodeBot tickets.

Reads lifecycle_events.jsonl, lifecycle_packets/, and tickets.json to produce
diagnostic reports on the IMPLEMENTING→COMPLETE pipeline, with emphasis on
failure modes (REWORK, REJECTED, DEFERRED, DUPLICATE, BLOCKED).

Read-only: never modifies state.

Views:
  failures   - Tickets that entered failure states, with root causes
  rework     - Rework loop analysis: chains, triggers, time cost
  timing     - Per-stage duration statistics (min/max/avg/p50/p95)
  agents     - Per-actor performance metrics
  bottlenecks - Stages exceeding age thresholds
  snapshot   - Current state distribution with age
  pipeline   - Full ASCII timelines for filtered tickets

Usage:
  python scripts/track_lifecycle.py --view failures
  python scripts/track_lifecycle.py --view rework --class-filter security
  python scripts/track_lifecycle.py --view timing --since 48
  python scripts/track_lifecycle.py --view all --json
"""

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_STATE_DIR = Path(__file__).resolve().parent.parent / ".codebot" / "state"
FAILURE_STATES = frozenset({"REWORK", "REJECTED", "DEFERRED", "DUPLICATE", "BLOCKED"})
TERMINAL_STATES = frozenset({"COMPLETE", "REJECTED", "DUPLICATE"})
PIPELINE_STATES = ("IMPLEMENTING", "REVIEWING", "VERIFYING", "COMPLETE", "REWORK")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_events(events_file: Path, since_ts: float | None = None) -> list[dict]:
    """Parse lifecycle_events.jsonl, optionally filtering by timestamp."""
    events = []
    if not events_file.exists():
        print(f"Warning: events file not found: {events_file}", file=sys.stderr)
        return events
    try:
        with open(events_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if since_ts and e.get("timestamp", 0) < since_ts:
                        continue
                    events.append(e)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        print(f"Warning: failed to read events: {exc}", file=sys.stderr)
    return events


def load_packets(packets_dir: Path, ticket_ids: set[str] | None = None) -> dict[str, dict]:
    """Load lifecycle packets, optionally filtering to specific ticket IDs."""
    packets = {}
    if not packets_dir.exists():
        return packets
    for p in packets_dir.glob("*.json"):
        tid = p.stem.replace("_", "/")  # reverse the safe_id transform
        # Actually the safe_id replaces / with _, but CB tickets don't have /
        tid = p.stem
        if ticket_ids and tid not in ticket_ids:
            continue
        try:
            packets[tid] = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
    return packets


def load_tickets(tickets_file: Path) -> dict[str, dict]:
    """Load tickets.json into a dict keyed by ticket ID."""
    if not tickets_file.exists():
        print(f"Warning: tickets file not found: {tickets_file}", file=sys.stderr)
        return {}
    try:
        data = json.loads(tickets_file.read_text(encoding="utf-8"))
        return {t["id"]: t for t in data.get("tickets", []) if "id" in t}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Warning: failed to load tickets: {exc}", file=sys.stderr)
        return {}


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def build_timelines(events: list[dict]) -> dict[str, list[dict]]:
    """Group events by ticket_id, sorted by timestamp."""
    timelines: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        timelines[e.get("ticket_id", "")].append(e)
    for tid in timelines:
        timelines[tid].sort(key=lambda x: x.get("timestamp", 0))
    return timelines


def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p / 100.0
    f = int(k)
    c = f + 1
    if c >= len(sorted_vals):
        return sorted_vals[-1]
    return sorted_vals[f] + (k - f) * (sorted_vals[c] - sorted_vals[f])


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.1f}m"
    return f"{seconds/3600:.1f}h"


def extract_failure_reason(ticket: dict, packet: dict | None) -> str:
    """Extract the most useful failure reason from ticket feedback or packet evidence."""
    # Check reviewer_feedback on the ticket
    feedback = ticket.get("reviewer_feedback", [])
    if feedback:
        latest = feedback[-1] if isinstance(feedback, list) else feedback
        if isinstance(latest, dict):
            # Look for finding, issue, or recommendation fields
            for key in ("finding", "issue", "recommended_fix", "reason", "message"):
                if key in latest and latest[key]:
                    val = str(latest[key])
                    return val[:200] if len(val) > 200 else val
        elif isinstance(latest, str):
            return latest[:200]

    # Check gate evidence in packet
    if packet:
        evidence = packet.get("evidence", {})
        for stage_name, stage_data in evidence.items():
            data = stage_data.get("data", {}) if isinstance(stage_data, dict) else {}
            gates = data.get("gates", [])
            for gate in gates:
                if not gate.get("passed", True):
                    gname = gate.get("gate_name", stage_name)
                    err = gate.get("error_message", "")
                    if err:
                        return f"Gate '{gname}' failed: {err[:150]}"
                    return f"Gate '{gname}' failed"

    # Check last_gate_result on ticket
    lgr = ticket.get("last_gate_result", {})
    if isinstance(lgr, dict) and lgr:
        if not lgr.get("passed", True):
            return f"Gate failed: {json.dumps(lgr)[:150]}"

    return "No specific reason captured"


def filter_tickets(
    ticket_ids: set[str] | None,
    state_filter: str | None,
    class_filter: str | None,
    tickets: dict[str, dict],
) -> set[str]:
    """Return set of ticket IDs matching all filters."""
    candidates = set(tickets.keys())
    if ticket_ids:
        candidates &= ticket_ids
    if state_filter:
        candidates = {tid for tid in candidates if tickets.get(tid, {}).get("state") == state_filter}
    if class_filter:
        candidates = {tid for tid in candidates if tickets.get(tid, {}).get("ticket_class") == class_filter}
    return candidates


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def view_failures(
    events: list[dict],
    tickets: dict[str, dict],
    packets: dict[str, dict],
    focus_ids: set[str] | None,
    top_n: int,
) -> dict:
    """Report all tickets that entered failure states."""
    failure_events = [e for e in events if e.get("to_state") in FAILURE_STATES]
    if focus_ids:
        failure_events = [e for e in failure_events if e.get("ticket_id") in focus_ids]

    # Group by ticket
    by_ticket: dict[str, list[dict]] = defaultdict(list)
    for e in failure_events:
        by_ticket[e["ticket_id"]].append(e)

    # Build report rows
    rows = []
    for tid, fevents in by_ticket.items():
        t = tickets.get(tid, {})
        pkt = packets.get(tid)
        # Get the final failure state
        final_event = fevents[-1]
        final_state = final_event["to_state"]
        from_state = final_event["from_state"]
        reason = extract_failure_reason(t, pkt)
        rows.append({
            "ticket_id": tid,
            "class": t.get("ticket_class", "?"),
            "severity": t.get("severity", "?"),
            "current_state": t.get("state", final_state),
            "failure_transition": f"{from_state} → {final_state}",
            "rework_count": t.get("rework_count", final_event.get("rework_count", 0)),
            "attempts": t.get("attempts", final_event.get("attempts", 0)),
            "total_failure_events": len(fevents),
            "reason": reason,
        })

    # Sort by rework_count desc, then attempts desc
    rows.sort(key=lambda r: (-r["rework_count"], -r["attempts"]))

    # Summary stats
    state_counts = Counter(r["current_state"] for r in rows)
    class_counts = Counter(r["class"] for r in rows)
    transition_counts = Counter(r["failure_transition"] for r in rows)

    result = {
        "total_failed_tickets": len(rows),
        "by_current_state": dict(state_counts.most_common()),
        "by_class": dict(class_counts.most_common()),
        "by_transition": dict(transition_counts.most_common()),
        "tickets": rows[:top_n] if top_n else rows,
    }
    return result


def view_rework(
    events: list[dict],
    tickets: dict[str, dict],
    packets: dict[str, dict],
    focus_ids: set[str] | None,
    top_n: int,
) -> dict:
    """Analyze rework loops: chains, triggers, time cost."""
    timelines = build_timelines(events)
    if focus_ids:
        timelines = {tid: evts for tid, evts in timelines.items() if tid in focus_ids}

    rework_chains = []
    for tid, evts in timelines.items():
        rework_events = [e for e in evts if e.get("to_state") == "REWORK"]
        if not rework_events:
            continue
        t = tickets.get(tid, {})
        pkt = packets.get(tid)

        # Build chain: find the full cycle around each REWORK
        chain_stages = []
        for i, e in enumerate(evts):
            chain_stages.append({
                "from": e.get("from_state"),
                "to": e.get("to_state"),
                "duration_s": e.get("queue_age_seconds", 0),
                "at": e.get("timestamp", 0),
            })

        total_rework_time = sum(
            e.get("queue_age_seconds", 0) for e in evts
            if e.get("from_state") == "REWORK" or e.get("to_state") == "REWORK"
        )
        total_pipeline_time = sum(e.get("queue_age_seconds", 0) for e in evts)

        reason = extract_failure_reason(t, pkt)
        rework_chains.append({
            "ticket_id": tid,
            "class": t.get("ticket_class", "?"),
            "rework_count": len(rework_events),
            "attempts": t.get("attempts", 0),
            "current_state": t.get("state", "?"),
            "total_rework_time_s": round(total_rework_time, 1),
            "total_pipeline_time_s": round(total_pipeline_time, 1),
            "rework_pct": round(100 * total_rework_time / total_pipeline_time, 1) if total_pipeline_time > 0 else 0,
            "chain": chain_stages,
            "primary_reason": reason,
        })

    rework_chains.sort(key=lambda r: (-r["rework_count"], -r["total_rework_time_s"]))

    # Multi-rework tickets (rework_count > 1)
    multi_rework = [r for r in rework_chains if r["rework_count"] > 1]

    result = {
        "total_tickets_with_rework": len(rework_chains),
        "multi_rework_tickets": len(multi_rework),
        "total_rework_transitions": sum(r["rework_count"] for r in rework_chains),
        "by_class": dict(Counter(r["class"] for r in rework_chains).most_common()),
        "tickets": rework_chains[:top_n] if top_n else rework_chains,
    }
    return result


def view_timing(events: list[dict], focus_ids: set[str] | None) -> dict:
    """Per-stage duration statistics."""
    if focus_ids:
        events = [e for e in events if e.get("ticket_id") in focus_ids]

    stage_durations: dict[str, list[float]] = defaultdict(list)
    for e in events:
        key = f"{e.get('from_state', '?')} → {e.get('to_state', '?')}"
        dur = e.get("queue_age_seconds", 0)
        if dur >= 0:
            stage_durations[key].append(dur)

    stats = []
    for stage, durations in sorted(stage_durations.items(), key=lambda x: -len(x[1])):
        durations.sort()
        stats.append({
            "stage": stage,
            "count": len(durations),
            "min_s": round(durations[0], 1),
            "max_s": round(durations[-1], 1),
            "avg_s": round(sum(durations) / len(durations), 1),
            "p50_s": round(percentile(durations, 50), 1),
            "p95_s": round(percentile(durations, 95), 1),
        })

    return {"stages": stats}


def view_agents(events: list[dict], tickets: dict[str, dict], focus_ids: set[str] | None) -> dict:
    """Per-actor performance metrics."""
    if focus_ids:
        events = [e for e in events if e.get("ticket_id") in focus_ids]

    actor_stats: dict[str, dict] = defaultdict(lambda: {
        "transitions": 0, "reworks": 0, "completions": 0,
        "tickets": set(), "durations": [],
    })

    for e in events:
        actor = e.get("actor", "") or "<unattributed>"
        s = actor_stats[actor]
        s["transitions"] += 1
        s["tickets"].add(e.get("ticket_id"))
        dur = e.get("queue_age_seconds", 0)
        if dur >= 0:
            s["durations"].append(dur)
        if e.get("to_state") == "REWORK":
            s["reworks"] += 1
        if e.get("to_state") == "COMPLETE":
            s["completions"] += 1

    rows = []
    for actor, s in sorted(actor_stats.items(), key=lambda x: -x[1]["transitions"]):
        durs = sorted(s["durations"]) if s["durations"] else [0]
        rows.append({
            "actor": actor,
            "transitions": s["transitions"],
            "unique_tickets": len(s["tickets"]),
            "reworks_triggered": s["reworks"],
            "completions": s["completions"],
            "rework_rate_pct": round(100 * s["reworks"] / s["transitions"], 1) if s["transitions"] else 0,
            "avg_stage_duration_s": round(sum(durs) / len(durs), 1),
            "p95_stage_duration_s": round(percentile(durs, 95), 1),
        })

    return {"actors": rows}


def view_bottlenecks(
    events: list[dict],
    tickets: dict[str, dict],
    focus_ids: set[str] | None,
    max_age_hours: float,
) -> dict:
    """Flag stages where queue_age_seconds exceeds threshold."""
    threshold_s = max_age_hours * 3600
    if focus_ids:
        events = [e for e in events if e.get("ticket_id") in focus_ids]

    slow = []
    for e in events:
        dur = e.get("queue_age_seconds", 0)
        if dur > threshold_s:
            t = tickets.get(e.get("ticket_id", ""), {})
            slow.append({
                "ticket_id": e.get("ticket_id"),
                "transition": f"{e.get('from_state')} → {e.get('to_state')}",
                "duration_h": round(dur / 3600, 1),
                "class": t.get("ticket_class", "?"),
                "current_state": t.get("state", "?"),
            })

    slow.sort(key=lambda x: -x["duration_h"])
    by_stage = Counter(s["transition"] for s in slow)

    return {
        "threshold_hours": max_age_hours,
        "total_slow_transitions": len(slow),
        "by_stage": dict(by_stage.most_common()),
        "slowest": slow[:50],
    }


def view_snapshot(tickets: dict[str, dict], focus_ids: set[str] | None) -> dict:
    """Current state distribution with age."""
    now = time.time()
    pool = {tid: t for tid, t in tickets.items() if not focus_ids or tid in focus_ids}

    state_groups: dict[str, list[dict]] = defaultdict(list)
    for tid, t in pool.items():
        state = t.get("state", "UNKNOWN")
        age_h = (now - t.get("updated_at", now)) / 3600
        state_groups[state].append({"ticket_id": tid, "age_h": round(age_h, 1), "class": t.get("ticket_class", "?")})

    rows = []
    for state, items in sorted(state_groups.items(), key=lambda x: -len(x[1])):
        ages = sorted(i["age_h"] for i in items)
        classes = Counter(i["class"] for i in items)
        rows.append({
            "state": state,
            "count": len(items),
            "min_age_h": ages[0] if ages else 0,
            "max_age_h": ages[-1] if ages else 0,
            "avg_age_h": round(sum(ages) / len(ages), 1) if ages else 0,
            "p50_age_h": round(percentile(ages, 50), 1),
            "classes": dict(classes.most_common()),
        })

    return {"total_tickets": len(pool), "states": rows}


def view_pipeline(
    events: list[dict],
    tickets: dict[str, dict],
    focus_ids: set[str] | None,
    top_n: int,
) -> dict:
    """Full ASCII timelines for filtered tickets."""
    timelines = build_timelines(events)
    if focus_ids:
        timelines = {tid: evts for tid, evts in timelines.items() if tid in focus_ids}

    # Only show tickets that went through IMPLEMENTING
    pipeline_tickets = {}
    for tid, evts in timelines.items():
        states = [e.get("to_state") for e in evts]
        if "IMPLEMENTING" in states or any(s in FAILURE_STATES for s in states):
            pipeline_tickets[tid] = evts

    rows = []
    for tid, evts in list(pipeline_tickets.items())[:top_n or 50]:
        t = tickets.get(tid, {})
        stages = []
        for e in evts:
            stages.append({
                "from": e.get("from_state"),
                "to": e.get("to_state"),
                "duration": fmt_duration(e.get("queue_age_seconds", 0)),
                "rework_count": e.get("rework_count", 0),
            })
        rows.append({
            "ticket_id": tid,
            "class": t.get("ticket_class", "?"),
            "current_state": t.get("state", "?"),
            "stages": stages,
        })

    return {"tickets": rows}


# ---------------------------------------------------------------------------
# Text formatting
# ---------------------------------------------------------------------------

def print_failures(data: dict) -> None:
    print("\n=== FAILURE REPORT ===")
    print(f"Total tickets with failures: {data['total_failed_tickets']}")
    print(f"By current state: {json.dumps(data['by_current_state'])}")
    print(f"By class: {json.dumps(data['by_class'])}")
    print(f"By transition type: {json.dumps(data['by_transition'])}")
    print()

    if not data["tickets"]:
        print("  No failures found.")
        return

    # Table header
    hdr = f"{'TICKET':<30} {'CLASS':<14} {'STATE':<12} {'TRANSITION':<28} {'REWORKS':<8} {'ATTEMPTS':<9} REASON"
    print(hdr)
    print("-" * len(hdr))
    for r in data["tickets"]:
        reason = r["reason"][:80]
        print(f"{r['ticket_id']:<30} {r['class']:<14} {r['current_state']:<12} {r['failure_transition']:<28} {r['rework_count']:<8} {r['attempts']:<9} {reason}")


def print_rework(data: dict) -> None:
    print("\n=== REWORK ANALYSIS ===")
    print(f"Tickets with rework: {data['total_tickets_with_rework']}")
    print(f"Multi-rework (>1): {data['multi_rework_tickets']}")
    print(f"Total rework transitions: {data['total_rework_transitions']}")
    print(f"By class: {json.dumps(data['by_class'])}")
    print()

    if not data["tickets"]:
        print("  No rework found.")
        return

    hdr = f"{'TICKET':<30} {'CLASS':<14} {'STATE':<12} {'REWORKS':<8} {'ATTEMPTS':<9} {'REWORK_TIME':<12} {'REWORK%':<8} REASON"
    print(hdr)
    print("-" * len(hdr))
    for r in data["tickets"]:
        reason = r["primary_reason"][:60]
        print(f"{r['ticket_id']:<30} {r['class']:<14} {r['current_state']:<12} {r['rework_count']:<8} {r['attempts']:<9} {fmt_duration(r['total_rework_time_s']):<12} {r['rework_pct']:<8} {reason}")


def print_timing(data: dict) -> None:
    print("\n=== STAGE TIMING ===")
    hdr = f"{'STAGE':<35} {'COUNT':<8} {'MIN':<10} {'AVG':<10} {'P50':<10} {'P95':<10} {'MAX':<10}"
    print(hdr)
    print("-" * len(hdr))
    for s in data["stages"]:
        print(f"{s['stage']:<35} {s['count']:<8} {fmt_duration(s['min_s']):<10} {fmt_duration(s['avg_s']):<10} {fmt_duration(s['p50_s']):<10} {fmt_duration(s['p95_s']):<10} {fmt_duration(s['max_s']):<10}")


def print_agents(data: dict) -> None:
    print("\n=== AGENT PERFORMANCE ===")
    hdr = f"{'ACTOR':<25} {'TRANSITIONS':<12} {'TICKETS':<9} {'REWORKS':<9} {'COMPLETES':<10} {'REWORK%':<9} {'AVG_DUR':<10}"
    print(hdr)
    print("-" * len(hdr))
    for r in data["actors"]:
        print(f"{r['actor']:<25} {r['transitions']:<12} {r['unique_tickets']:<9} {r['reworks_triggered']:<9} {r['completions']:<10} {r['rework_rate_pct']:<9} {fmt_duration(r['avg_stage_duration_s']):<10}")


def print_bottlenecks(data: dict) -> None:
    print(f"\n=== BOTTLENECKS (>{data['threshold_hours']}h) ===")
    print(f"Total slow transitions: {data['total_slow_transitions']}")
    print(f"By stage: {json.dumps(data['by_stage'])}")
    print()
    for r in data["slowest"][:20]:
        print(f"  {r['ticket_id']:<30} {r['transition']:<30} {r['duration_h']}h  class={r['class']} state={r['current_state']}")


def print_snapshot(data: dict) -> None:
    print(f"\n=== STATE SNAPSHOT ({data['total_tickets']} tickets) ===")
    hdr = f"{'STATE':<22} {'COUNT':<7} {'MIN_AGE':<10} {'AVG_AGE':<10} {'P50_AGE':<10} {'MAX_AGE':<10} TOP_CLASSES"
    print(hdr)
    print("-" * len(hdr))
    for r in data["states"]:
        classes = ", ".join(f"{k}:{v}" for k, v in list(r["classes"].items())[:3])
        print(f"{r['state']:<22} {r['count']:<7} {fmt_duration(r['min_age_h']*3600):<10} {fmt_duration(r['avg_age_h']*3600):<10} {fmt_duration(r['p50_age_h']*3600):<10} {fmt_duration(r['max_age_h']*3600):<10} {classes}")


def print_pipeline(data: dict) -> None:
    print("\n=== PIPELINE TIMELINES ===")
    for t in data["tickets"]:
        print(f"\n  {t['ticket_id']} [{t['class']}] → {t['current_state']}")
        chain = " → ".join(
            f"{s['to']}({s['duration']})" for s in t["stages"]
        )
        print(f"    {chain}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="CodeBot ticket lifecycle tracker and failure analyzer.")
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--events-file", type=Path, default=None)
    parser.add_argument("--packets-dir", type=Path, default=None)
    parser.add_argument("--tickets-file", type=Path, default=None)
    parser.add_argument("--view", choices=["failures", "rework", "timing", "agents", "bottlenecks", "snapshot", "pipeline", "all"], default="all")
    parser.add_argument("--ticket-ids", type=str, default=None, help="Comma-separated ticket IDs to focus on")
    parser.add_argument("--since", type=float, default=None, help="Hours ago to filter from")
    parser.add_argument("--state-filter", type=str, default=None, help="Filter to tickets currently in this state")
    parser.add_argument("--class-filter", type=str, default=None, help="Filter to tickets of this class")
    parser.add_argument("--max-age-hours", type=float, default=1.0, help="Bottleneck threshold in hours")
    parser.add_argument("--top", type=int, default=0, help="Limit output rows (0=all)")
    parser.add_argument("--json", action="store_true", dest="output_json")
    args = parser.parse_args()

    state_dir = args.state_dir or DEFAULT_STATE_DIR
    events_file = args.events_file or (state_dir / "lifecycle_events.jsonl")
    packets_dir = args.packets_dir or (state_dir / "lifecycle_packets")
    tickets_file = args.tickets_file or (state_dir / "tickets.json")

    since_ts = time.time() - (args.since * 3600) if args.since else None
    focus_ids = set(args.ticket_ids.split(",")) if args.ticket_ids else None

    # Load data
    events = load_events(events_file, since_ts)
    tickets = load_tickets(tickets_file)

    # Apply state/class filters to get focused ticket set
    if args.state_filter or args.class_filter:
        focus_ids = filter_tickets(focus_ids, args.state_filter, args.class_filter, tickets)

    # Only load packets if we need them (failures/rework views)
    need_packets = args.view in ("failures", "rework", "all")
    packets = load_packets(packets_dir, focus_ids) if need_packets else {}

    # Run views
    results = {}
    view = args.view
    if view in ("failures", "all"):
        results["failures"] = view_failures(events, tickets, packets, focus_ids, args.top)
    if view in ("rework", "all"):
        results["rework"] = view_rework(events, tickets, packets, focus_ids, args.top)
    if view in ("timing", "all"):
        results["timing"] = view_timing(events, focus_ids)
    if view in ("agents", "all"):
        results["agents"] = view_agents(events, tickets, focus_ids)
    if view in ("bottlenecks", "all"):
        results["bottlenecks"] = view_bottlenecks(events, tickets, focus_ids, args.max_age_hours)
    if view in ("snapshot", "all"):
        results["snapshot"] = view_snapshot(tickets, focus_ids)
    if view in ("pipeline", "all"):
        results["pipeline"] = view_pipeline(events, tickets, focus_ids, args.top)

    # Output
    if args.output_json:
        print(json.dumps(results, indent=2, default=str))
    else:
        if "failures" in results:
            print_failures(results["failures"])
        if "rework" in results:
            print_rework(results["rework"])
        if "timing" in results:
            print_timing(results["timing"])
        if "agents" in results:
            print_agents(results["agents"])
        if "bottlenecks" in results:
            print_bottlenecks(results["bottlenecks"])
        if "snapshot" in results:
            print_snapshot(results["snapshot"])
        if "pipeline" in results:
            print_pipeline(results["pipeline"])


if __name__ == "__main__":
    main()
