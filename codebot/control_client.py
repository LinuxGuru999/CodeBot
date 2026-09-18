#!/usr/bin/env python3
"""Control client for the Fly-hosted botnet control_server.py.

Purpose
-------
Provides a lightweight HTTP client for communicating with the control_server
endpoints. Used by botop CLI and external tooling to query agent status,
trigger restarts, and manage drain state remotely.

Why
---
The control server exposes a REST-like API over HTTP. External tools and
the botop CLI need a dependency-free way to call these endpoints without
pulling in requests or aiohttp.

Invariants
----------
- stdlib-only (http.client, json, urllib.parse)
- All requests are synchronous blocking calls
- Connection errors fail open (return None, never raise)

Usage:
  CONTROL_URL=https://monitor-botnet.fly.dev CONTROL_TOKEN=xxx python3 control_client.py status
  CONTROL_URL=https://monitor-botnet.fly.dev CONTROL_TOKEN=xxx python3 control_client.py logs issues --lines 200
  CONTROL_URL=https://monitor-botnet.fly.dev CONTROL_TOKEN=xxx python3 control_client.py restart bug_fix
  CONTROL_URL=https://monitor-botnet.fly.dev CONTROL_TOKEN=xxx python3 control_client.py drain
  CONTROL_URL=https://monitor-botnet.fly.dev CONTROL_TOKEN=xxx python3 control_client.py clear-drain

If CONTROL_URL/TOKEN unset, defaults to http://127.0.0.1:8081 with no auth (local test).

Stdlib only.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error

URL = os.environ.get("CONTROL_URL", "http://127.0.0.1:8081").rstrip("/")
TOKEN = os.environ.get("CONTROL_TOKEN", "").strip()

def req(method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    url = f"{URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw) if raw else {}
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(raw) if raw else {"error": raw}
        except Exception:
            return e.code, {"error": raw, "code": e.code}
    except Exception as e:
        return 0, {"error": str(e)}

def cmd_status():
    code, j = req("GET", "/bots")
    if code != 200:
        print(j); sys.exit(1)
    for b in j:
        print(f"{b['name']:15s} {'RUN' if b.get('running') else 'WAIT':5s}  hb={str(b.get('heartbeat_age_seconds'))+'s' if b.get('heartbeat_age_seconds') is not None else '-':8s}  eff={b.get('effective_timeout')}  risk={b.get('risk')}  model={b.get('model')}  nxt={b.get('next_run_in_seconds')}")


def cmd_scheduler_status():
    code, payload = req("GET", "/scheduler/status")
    if code != 200:
        print(payload)
        sys.exit(1)
    if not isinstance(payload, dict):
        print(payload)
        sys.exit(1)
    disabled_details = payload.get("disabled_details") or []
    if not isinstance(disabled_details, list):
        disabled_details = []
    bounded = {
        "version": payload.get("version"),
        "budget_state": payload.get("budget_state"),
        "budget_day": payload.get("budget_day"),
        "budget_total_actual": payload.get("budget_total_actual"),
        "drain": payload.get("drain"),
        "dead_letter_count": payload.get("dead_letter_count"),
        "dead_letter_ids": (payload.get("dead_letter_ids") or [])[:20],
        "queue_tracked": payload.get("queue_tracked"),
        "lease_active": payload.get("lease_active"),
        "paused_count": payload.get("paused_count"),
        "paused_bots": (payload.get("paused_bots") or [])[:20],
        "disabled_count": payload.get("disabled_count"),
        "disabled_bots": (payload.get("disabled_bots") or [])[:20],
        "disabled_details": disabled_details[:20],
        "starved_oldest": payload.get("starved_oldest"),
        "starved_top": (payload.get("starved_top") or [])[:5],
        "batch_utilization": payload.get("batch_utilization"),
        "per_model_actual": dict(list((payload.get("per_model_actual") or {}).items())[:32]),
    }
    print(json.dumps(bounded, indent=2)[:8000])


def cmd_scheduler_events(limit: str = "20", event_type: str | None = None):
    try:
        limit_int = max(1, min(int(limit), 100))
    except ValueError:
        print(f"--limit must be an integer, got: {limit}")
        sys.exit(1)
    path = f"/scheduler/events?limit={limit_int}"
    if event_type:
        from urllib.parse import quote
        path += f"&type={quote(event_type)}"
    code, payload = req("GET", path)
    if code != 200:
        print(payload)
        sys.exit(1)
    print(json.dumps(payload, indent=2)[:8000])


def cmd_dead_letters():
    code, payload = req("GET", "/scheduler/dead-letters")
    if code != 200:
        print(payload)
        sys.exit(1)
    print(json.dumps(payload, indent=2)[:8000])


def cmd_dead_letter_retry(item_id: str):
    code, payload = req("POST", f"/scheduler/dead-letters/{item_id}/retry", {})
    print(payload if isinstance(payload, dict) else json.dumps(payload, indent=2))
    if code != 200:
        sys.exit(1)

def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "status":
        cmd_status()
    elif cmd == "scheduler-status":
        cmd_scheduler_status()
    elif cmd == "scheduler-events":
        limit = "20"
        event_type = None
        args = sys.argv[2:]
        i = 0
        while i < len(args):
            if args[i] == "--limit" and i + 1 < len(args):
                limit = args[i + 1]
                i += 2
            elif args[i] == "--type" and i + 1 < len(args):
                event_type = args[i + 1]
                i += 2
            elif args[i].startswith("--"):
                print(f"unknown option: {args[i]}")
                print("Usage: control_client.py scheduler-events [--limit N] [--type TYPE]")
                sys.exit(1)
            else:
                print(f"unknown argument: {args[i]}")
                print("Usage: control_client.py scheduler-events [--limit N] [--type TYPE]")
                sys.exit(1)
        cmd_scheduler_events(limit, event_type)
    elif cmd == "dead-letters":
        cmd_dead_letters()
    elif cmd == "retry-dead-letter" and len(sys.argv) >= 3:
        cmd_dead_letter_retry(sys.argv[2])
    elif cmd == "health":
        code, j = req("GET", "/health"); print(json.dumps(j, indent=2))
    elif cmd == "logs" and len(sys.argv) >= 3:
        name = sys.argv[2]
        lines = 200
        if len(sys.argv) > 3 and sys.argv[3] == "--lines":
            if len(sys.argv) < 5:
                print("Usage: control_client.py logs <name> --lines N"); sys.exit(1)
            try:
                lines = int(sys.argv[4])
            except ValueError:
                print(f"--lines must be an integer, got: {sys.argv[4]}"); sys.exit(1)
        code, j = req("GET", f"/bots/{name}/logs?lines={lines}")
        print(j.get("tail", json.dumps(j, indent=2)) if isinstance(j, dict) else j)
    elif cmd == "restart" and len(sys.argv) >= 3:
        code, j = req("POST", f"/bots/{sys.argv[2]}/restart", {}); print(j)
    elif cmd == "pause" and len(sys.argv) >= 3:
        code, j = req("POST", f"/bots/{sys.argv[2]}/pause", {}); print(j)
    elif cmd == "resume" and len(sys.argv) >= 3:
        code, j = req("POST", f"/bots/{sys.argv[2]}/resume", {}); print(j)
    elif cmd in ("drain", "safe-stop"):
        code, j = req("POST", "/control/drain", {}); print(j)
    elif cmd in ("clear-drain", "undrain"):
        code, j = req("POST", "/control/clear-drain", {}); print(j)
    elif cmd == "update":
        code, j = req("POST", "/control/update", {}); print(json.dumps(j, indent=2)[:8000])
    elif cmd == "state":
        code, j = req("GET", "/state"); print(json.dumps(j, indent=2))
    else:
        print(f"unknown command: {cmd}\n{__doc__}"); sys.exit(2)

if __name__ == "__main__":
    main()
