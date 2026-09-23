#!/usr/bin/env python3
"""Persistent lifecycle failure monitor for CodeBot tickets.

Watches lifecycle_events.jsonl for tickets entering failure states
(REWORK, REJECTED, DEFERRED, DUPLICATE, BLOCKED, NOT_ACTIONABLE, NEVER)
and emits alerts to stdout and an alert log file.

Reads the append-only events file incrementally (tracks byte offset).
Never modifies ticket state.

Usage:
  python scripts/monitor_failures.py                              # poll every 30s
  python scripts/monitor_failures.py --interval 10                # poll every 10s
  python scripts/monitor_failures.py --once                       # single scan, exit
  python scripts/monitor_failures.py --alert-log /path/to/log.jsonl
  python scripts/monitor_failures.py --states REWORK,REJECTED     # watch specific failures
"""

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

DEFAULT_STATE_DIR = Path(__file__).resolve().parent.parent / ".codebot" / "state"
DEFAULT_EVENTS_FILE = DEFAULT_STATE_DIR / "lifecycle_events.jsonl"
DEFAULT_ALERT_LOG = DEFAULT_STATE_DIR / "failure_alerts.jsonl"
DEFAULT_OFFSET_FILE = DEFAULT_STATE_DIR / ".monitor_offset"

ALL_FAILURE_STATES = frozenset({
    "REWORK", "REJECTED", "DEFERRED", "DUPLICATE",
    "BLOCKED", "NOT_ACTIONABLE", "NEVER",
})

_shutdown = False


def handle_signal(signum, frame):
    global _shutdown
    _shutdown = True


def load_offset(offset_file: Path) -> int:
    try:
        return int(offset_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def save_offset(offset_file: Path, offset: int) -> None:
    try:
        tmp = offset_file.with_suffix(".tmp")
        tmp.write_text(str(offset), encoding="utf-8")
        os.replace(tmp, offset_file)
    except OSError:
        pass


def scan_new_events(
    events_file: Path,
    offset: int,
    watch_states: frozenset[str],
) -> tuple[list[dict], int]:
    alerts = []
    new_offset = offset

    if not events_file.exists():
        return alerts, offset

    try:
        file_size = os.stat(events_file).st_size
    except OSError:
        return alerts, offset

    if file_size < offset:
        offset = 0
        new_offset = 0

    try:
        with open(events_file, encoding="utf-8") as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                new_offset = f.tell()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                to_state = event.get("to_state", "")
                if to_state in watch_states:
                    alerts.append({
                        "alerted_at": time.time(),
                        "ticket_id": event.get("ticket_id", ""),
                        "from_state": event.get("from_state", ""),
                        "to_state": to_state,
                        "attempts": event.get("attempts", 0),
                        "rework_count": event.get("rework_count", 0),
                        "queue_age_seconds": event.get("queue_age_seconds", 0),
                        "actor": event.get("actor", ""),
                        "timestamp": event.get("timestamp", 0),
                    })
    except OSError:
        pass

    return alerts, new_offset


def emit_alert(alert: dict, alert_log: Path | None) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(alert["alerted_at"]))
    age = alert["queue_age_seconds"]
    age_str = f"{age:.0f}s" if age < 60 else f"{age/60:.1f}m" if age < 3600 else f"{age/3600:.1f}h"

    msg = (
        f"[{ts}] ALERT: {alert['ticket_id']} "
        f"{alert['from_state']} → {alert['to_state']} "
        f"(attempts={alert['attempts']}, reworks={alert['rework_count']}, "
        f"age={age_str}, actor={alert['actor'] or '<none>'})"
    )
    print(msg, flush=True)

    if alert_log:
        try:
            with open(alert_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(alert) + "\n")
        except OSError:
            pass


def scan_existing_failures(
    tickets_file: Path,
    watch_states: frozenset[str],
) -> list[dict]:
    if not tickets_file.exists():
        return []
    try:
        data = json.loads(tickets_file.read_text(encoding="utf-8"))
        tickets = data.get("tickets", [])
    except (json.JSONDecodeError, OSError):
        return []

    alerts = []
    now = time.time()
    for t in tickets:
        state = t.get("state", "")
        if state in watch_states:
            alerts.append({
                "alerted_at": now,
                "ticket_id": t.get("id", ""),
                "from_state": "EXISTING",
                "to_state": state,
                "attempts": t.get("attempts", 0),
                "rework_count": t.get("rework_count", 0),
                "queue_age_seconds": now - t.get("updated_at", now),
                "actor": t.get("assigned_agent", ""),
                "timestamp": t.get("updated_at", 0),
            })
    return alerts


def run_once(
    events_file: Path,
    offset_file: Path,
    alert_log: Path | None,
    watch_states: frozenset[str],
    scan_existing: bool,
    tickets_file: Path,
) -> int:
    offset = load_offset(offset_file)
    alerts, new_offset = scan_new_events(events_file, offset, watch_states)

    total = len(alerts)
    for alert in alerts:
        emit_alert(alert, alert_log)

    if scan_existing:
        existing = scan_existing_failures(tickets_file, watch_states)
        total += len(existing)
        for alert in existing:
            emit_alert(alert, alert_log)

    save_offset(offset_file, new_offset)
    return total


def run_loop(
    events_file: Path,
    offset_file: Path,
    alert_log: Path | None,
    watch_states: frozenset[str],
    interval: float,
    scan_existing: bool,
    tickets_file: Path,
) -> None:
    global _shutdown
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(f"Monitoring {events_file} for failure states: {', '.join(sorted(watch_states))}")
    print(f"Poll interval: {interval}s | Alert log: {alert_log or 'stdout only'}")
    print(f"Press Ctrl+C to stop.\n", flush=True)

    first_scan = True
    while not _shutdown:
        do_existing = scan_existing and first_scan
        count = run_once(events_file, offset_file, alert_log, watch_states, do_existing, tickets_file)
        if count > 0:
            print(f"--- {count} failure(s) detected ---\n", flush=True)
        first_scan = False

        deadline = time.time() + interval
        while not _shutdown and time.time() < deadline:
            time.sleep(min(1.0, deadline - time.time()))

    print("\nMonitor stopped.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor CodeBot tickets for failure state transitions.")
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--events-file", type=Path, default=None)
    parser.add_argument("--tickets-file", type=Path, default=None)
    parser.add_argument("--alert-log", type=Path, default=None)
    parser.add_argument("--offset-file", type=Path, default=None)
    parser.add_argument("--interval", type=float, default=30.0, help="Poll interval in seconds")
    parser.add_argument("--once", action="store_true", help="Single scan then exit")
    parser.add_argument("--scan-existing", action="store_true", help="Also report tickets already in failure states on first scan")
    parser.add_argument(
        "--states",
        type=str,
        default=None,
        help="Comma-separated failure states to watch (default: all)",
    )
    parser.add_argument("--json", action="store_true", dest="output_json", help="Output alerts as JSON lines")
    args = parser.parse_args()

    state_dir = args.state_dir or DEFAULT_STATE_DIR
    events_file = args.events_file or (state_dir / "lifecycle_events.jsonl")
    tickets_file = args.tickets_file or (state_dir / "tickets.json")
    alert_log = args.alert_log or DEFAULT_ALERT_LOG
    offset_file = args.offset_file or DEFAULT_OFFSET_FILE

    if args.states:
        watch_states = frozenset(s.strip().upper() for s in args.states.split(",") if s.strip())
    else:
        watch_states = ALL_FAILURE_STATES

    if args.once:
        count = run_once(events_file, offset_file, alert_log, watch_states, args.scan_existing, tickets_file)
        sys.exit(0 if count == 0 else 1)
    else:
        run_loop(events_file, offset_file, alert_log, watch_states, args.interval, args.scan_existing, tickets_file)


if __name__ == "__main__":
    main()
