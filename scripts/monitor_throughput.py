#!/usr/bin/env python3
"""Monitor CodeBot throughput over 20 minutes."""

import json
import time
import sys
from pathlib import Path
from datetime import datetime, timezone

STATE_DIR = Path("/home/kozuka/Work/CodeBot/.codebot/state")
LOGS_DIR = Path("/home/kozuka/Work/CodeBot/.codebot/logs")
RATE_FILE = STATE_DIR / "rate_limits.json"
TICKETS_FILE = STATE_DIR / "tickets.json"

def get_rate_state():
    try:
        return json.loads(RATE_FILE.read_text())
    except:
        return {}

def get_ticket_state():
    try:
        data = json.loads(TICKETS_FILE.read_text())
        tickets = data.get("tickets", [])
        by_state = {}
        for t in tickets:
            s = t.get("state", "UNKNOWN")
            by_state[s] = by_state.get(s, 0) + 1
        return by_state, len(tickets)
    except:
        return {}, 0

def count_agents():
    running = 0
    stale = 0
    dead = 0
    try:
        import subprocess
        out = subprocess.check_output(["ps", "-eo", "args="], text=True, timeout=5)
        for line in out.splitlines():
            if "codebot.api_runner" in line:
                running += 1
    except:
        pass
    
    for hb in STATE_DIR.glob("*.heartbeat"):
        try:
            ts = float(hb.read_text().strip().split()[0])
            age = time.time() - ts
            if age < 120:
                pass  # running
            elif age < 600:
                stale += 1
            else:
                dead += 1
        except:
            pass
    
    return running, stale, dead

def count_429s_lastminute():
    count = 0
    cutoff = time.time() - 60
    try:
        for log_file in LOGS_DIR.glob("*.log"):
            try:
                text = log_file.read_text(errors="ignore")
                for line in text.splitlines():
                    if "429" in line:
                        # Try to extract timestamp
                        import re
                        m = re.search(r"\[(\d+):(\d+):(\d+)\]", line)
                        if m:
                            h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
                            # Rough check: if it's from last minute
                            if h == datetime.now().hour:
                                count += 1
            except:
                pass
    except:
        pass
    return count

def main():
    duration = 20 * 60  # 20 minutes
    interval = 60  # check every minute
    
    print(f"Monitoring CodeBot for {duration//60} minutes...")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print()
    
    start = time.time()
    tick = 0
    initial_tickets = None
    initial_complete = None
    
    while time.time() - start < duration:
        tick += 1
        elapsed = time.time() - start
        remaining = duration - elapsed
        
        # Collect metrics
        rate_state = get_rate_state()
        ticket_state, total_tickets = get_ticket_state()
        running, stale, dead = count_agents()
        recent_429s = count_429s_lastminute()
        
        # Calculate stats
        total_req = sum(s.get("total_requests", 0) for s in rate_state.values())
        total_rl = sum(s.get("total_rate_limits", 0) for s in rate_state.values())
        complete = ticket_state.get("COMPLETE", 0)
        implementing = ticket_state.get("IMPLEMENTING", 0)
        ready = ticket_state.get("READY", 0)
        
        if initial_tickets is None:
            initial_tickets = total_tickets
            initial_complete = complete
        
        new_complete = complete - (initial_complete or 0)
        
        # Print status
        print(f"[{tick:2d}/20] {elapsed/60:.1f}min elapsed, {remaining/60:.1f}min remaining")
        print(f"  Agents: {running} running, {stale} stale, {dead} dead")
        print(f"  Tickets: {total_tickets} total, {complete} complete (+{new_complete}), {implementing} implementing, {ready} ready")
        print(f"  API: {total_req} requests, {total_rl} 429s ({total_rl/total_req*100:.1f}%)" if total_req > 0 else "  API: no data")
        print(f"  429s (last min): {recent_429s}")
        
        # Model breakdown
        if rate_state:
            print(f"  Top models by requests:")
            sorted_models = sorted(rate_state.items(), key=lambda x: x[1].get("total_requests", 0), reverse=True)[:5]
            for model, state in sorted_models:
                req = state.get("total_requests", 0)
                rl = state.get("total_rate_limits", 0)
                rpm = state.get("learned_rpm", 0)
                print(f"    {model}: {req} req, {rl} 429s, {rpm:.0f} RPM")
        
        print()
        sys.stdout.flush()
        
        # Sleep until next check
        if time.time() - start < duration:
            time.sleep(min(interval, duration - (time.time() - start)))
    
    # Final summary
    print("=" * 60)
    print(f"MONITORING COMPLETE")
    print(f"Duration: {duration//60} minutes")
    print(f"Final state:")
    print(f"  Total requests: {total_req}")
    print(f"  Total 429s: {total_rl}")
    print(f"  429 rate: {total_rl/total_req*100:.1f}%" if total_req > 0 else "  429 rate: N/A")
    print(f"  Tickets completed: +{new_complete}")
    print(f"  Throughput: {new_complete/(duration/60):.1f} tickets/min")
    print(f"  Estimated tokens/min: ~{new_complete * 1500 / (duration/60):.0f}")
    print(f"  Estimated tokens/day: ~{new_complete * 1500 / (duration/60) * 60 * 24:,.0f}")

if __name__ == "__main__":
    main()
