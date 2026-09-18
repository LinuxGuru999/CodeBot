#!/usr/bin/env python3
"""botop — Standalone bot operations CLI for CodeBot.

Purpose
-------
Provides direct bot management operations (status, logs, restart, pause,
resume, drain) without requiring the full orchestrator loop. Equivalent
to Monitor's botop.py but portable across any CodeBot-managed project.

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
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


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


def cmd_status(project_root: Path) -> int:
    state_dir = _find_state_dir(project_root)
    heartbeats = sorted(state_dir.glob("*.heartbeat"))
    if not heartbeats:
        print("No agents found.")
        return 0
    now = time.time()
    print(f"{'Agent':<25s} {'Status':<10s} {'HB Age':>8s} {'PID':>8s}")
    print("-" * 55)
    for hb in heartbeats:
        name = hb.stem
        try:
            ts = float(hb.read_text().strip().split()[0])
            age = now - ts
            status = "RUNNING" if age < 120 else "STALE" if age < 600 else "DEAD"
        except (ValueError, OSError):
            age = -1
            status = "UNKNOWN"
        pid = _find_agent_pid(name)
        age_str = f"{age:.0f}s" if age >= 0 else "?"
        pid_str = str(pid) if pid else "-"
        print(f"{name:<25s} {status:<10s} {age_str:>8s} {pid_str:>8s}")
    drain_file = state_dir / ".drain"
    if drain_file.exists():
        print(f"\nDRAIN ACTIVE: {drain_file.read_text().strip()}")
    return 0


def cmd_logs(project_root: Path, agent: str, lines: int = 50) -> int:
    logs_dir = _find_logs_dir(project_root)
    log_file = logs_dir / f"{agent}.log"
    if not log_file.exists():
        print(f"No log file found: {log_file}", file=sys.stderr)
        return 1
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), str(log_file)],
            capture_output=True, text=True, timeout=5,
        )
        print(result.stdout)
        return 0
    except FileNotFoundError:
        with open(log_file, encoding="utf-8") as f:
            all_lines = f.readlines()
            for line in all_lines[-lines:]:
                print(line, end="")
        return 0
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


def cmd_claims(project_root: Path) -> int:
    state_dir = _find_state_dir(project_root)
    claims_dir = state_dir / "claims"
    if not claims_dir.exists():
        print("No claims directory")
        return 0
    claims = sorted(claims_dir.glob("*.json"))
    if not claims:
        print("No active claims")
        return 0
    now = time.time()
    print(f"{'Claim File':<45s} {'Worker':<15s} {'Age':>8s}")
    print("-" * 70)
    for cf in claims:
        try:
            data = json.loads(cf.read_text(encoding="utf-8"))
            worker = data.get("worker", "?")
            at = data.get("at", 0)
            age = now - at if at else 0
            print(f"{cf.name:<45s} {worker:<15s} {age:.0f}s")
        except (json.JSONDecodeError, OSError):
            print(f"{cf.name:<45s} {'?':<15s} {'?':>8s}")
    return 0


def cmd_tickets(project_root: Path) -> int:
    state_dir = _find_state_dir(project_root)
    tickets_file = state_dir / "tickets.json"
    if not tickets_file.exists():
        tickets_file = state_dir / "codebot_tickets.json"
    if not tickets_file.exists():
        print("No ticket store found")
        return 0
    try:
        from codebot.ticket_engine import TicketStore
        store = TicketStore(tickets_file)
        summary = store.summary()
        print(f"Total tickets: {store.count()}")
        print(f"By state: {json.dumps(summary, indent=2)}")
        ready = store.list_ready()
        if ready:
            print(f"\nReady tickets ({len(ready)}):")
            for t in ready[:10]:
                print(f"  [{t.severity.value.upper():8s}] {t.id}: {t.title[:60]}")
    except ImportError:
        print("ticket_engine not available", file=sys.stderr)
        return 1
    return 0


def _find_agent_pid(agent_name: str) -> int | None:
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"api_runner.*{agent_name}"],
            capture_output=True, text=True, timeout=3,
        )
        if result.stdout.strip():
            return int(result.stdout.strip().split()[0])
    except Exception:
        pass
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="CodeBot Operations CLI")
    parser.add_argument("--project", type=str, default=".", help="Project root path")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("status", help="Show agent status")
    sub.add_parser("claims", help="Show active ticket claims")
    sub.add_parser("tickets", help="Show ticket store summary")

    logs_p = sub.add_parser("logs", help="Show agent logs")
    logs_p.add_argument("agent", help="Agent name")
    logs_p.add_argument("--lines", type=int, default=50, help="Number of lines")

    restart_p = sub.add_parser("restart", help="Restart an agent")
    restart_p.add_argument("agent", help="Agent name")

    pause_p = sub.add_parser("pause", help="Pause an agent")
    pause_p.add_argument("agent", help="Agent name")

    resume_p = sub.add_parser("resume", help="Resume a paused agent")
    resume_p.add_argument("agent", help="Agent name")

    drain_p = sub.add_parser("drain", help="Set drain flag")
    drain_p.add_argument("--reason", type=str, default="manual", help="Drain reason")

    sub.add_parser("clear-drain", help="Clear drain flag")

    args = parser.parse_args()
    project_root = Path(args.project).resolve()

    if args.cmd == "status":
        sys.exit(cmd_status(project_root))
    elif args.cmd == "logs":
        sys.exit(cmd_logs(project_root, args.agent, args.lines))
    elif args.cmd == "restart":
        sys.exit(cmd_restart(project_root, args.agent))
    elif args.cmd == "pause":
        sys.exit(cmd_pause(project_root, args.agent))
    elif args.cmd == "resume":
        sys.exit(cmd_resume(project_root, args.agent))
    elif args.cmd == "drain":
        sys.exit(cmd_drain(project_root, args.reason))
    elif args.cmd == "clear-drain":
        sys.exit(cmd_clear_drain(project_root))
    elif args.cmd == "claims":
        sys.exit(cmd_claims(project_root))
    elif args.cmd == "tickets":
        sys.exit(cmd_tickets(project_root))
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
