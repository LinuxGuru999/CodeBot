#!/usr/bin/env python3
"""Gradual rate limit probe — finds each model's actual RPM.

Sends requests at increasing rates until 429, records Retry-After,
then backs off. Runs ~90s per model across all 12 models.
Total time: ~20 minutes.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

API_URL = os.getenv("DIALAGRAM_API_URL", "https://dialagram.me/router/v1/chat/completions")
STATE_DIR = Path(__file__).parent.parent / ".codebot" / "state"
RESULTS_FILE = STATE_DIR / "rate_limit_probes.json"


def get_api_key():
    key = os.getenv("DIALAGRAM_API_KEY")
    if key and key.strip():
        return key.strip()
    try:
        cfg_path = Path.home() / ".config" / "opencode" / "opencode.jsonc"
        text = cfg_path.read_text(encoding="utf-8")
        import re
        m = re.search(r'"apiKey"\s*:\s*"([^"]+)"', text)
        if m:
            return m.group(1)
        import json as _json
        cleaned = re.sub(r"(?<!:)//.*", "", text)
        data = _json.loads(cleaned)
        opts = data.get("provider", {}).get("dialagram-local-router", {}).get("options", {})
        k = opts.get("apiKey")
        if isinstance(k, str) and k.strip():
            return k.strip()
    except Exception:
        pass
    return None


def send_one(model, api_key):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_tokens": 5,
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            elapsed = time.time() - start
            return True, None, elapsed, {
                "remaining": resp.headers.get("x-ratelimit-remaining"),
                "limit": resp.headers.get("x-ratelimit-limit"),
                "reset": resp.headers.get("x-ratelimit-reset"),
            }
    except urllib.error.HTTPError as e:
        elapsed = time.time() - start
        if e.code == 429:
            ra = None
            if hasattr(e, "headers"):
                ra_header = e.headers.get("Retry-After") or e.headers.get("retry-after")
                if ra_header:
                    try:
                        ra = float(ra_header)
                    except (ValueError, TypeError):
                        pass
            return False, ra, elapsed, {"status": 429}
        return False, None, elapsed, {"status": e.code}
    except Exception as e:
        elapsed = time.time() - start
        return False, None, elapsed, {"error": str(e)[:80]}


def probe_model(model, api_key, max_seconds=90):
    log = {
        "model": model,
        "start_time": datetime.now(timezone.utc).isoformat(),
        "requests_sent": 0,
        "successes": 0,
        "rate_limits": 0,
        "first_rpm_limit": None,
        "retry_afters": [],
        "measured_rpm": None,
        "headers_seen": [],
        "timeline": [],
    }

    interval = 2.0
    min_interval = 0.5
    phase = "ramp_up"

    start = time.time()
    last_request_time = 0

    while time.time() - start < max_seconds:
        elapsed_since_last = time.time() - last_request_time
        if elapsed_since_last < interval:
            time.sleep(interval - elapsed_since_last)

        ok, retry_after, req_elapsed, info = send_one(model, api_key)
        last_request_time = time.time()
        log["requests_sent"] += 1
        ts = round(time.time() - start, 1)

        if ok:
            log["successes"] += 1
            log["timeline"].append({"t": ts, "ok": True, "ms": round(req_elapsed * 1000)})
            if info.get("remaining") is not None:
                log["headers_seen"].append({"t": ts, **info})

            if phase == "ramp_up":
                interval = max(min_interval, interval * 0.85)
            elif phase == "backoff":
                phase = "steady"
                log["measured_rpm"] = round(60.0 / interval, 1)
                interval = max(min_interval, interval * 0.95)
        else:
            log["rate_limits"] += 1
            log["timeline"].append({"t": ts, "ok": False, "status": info.get("status"), "ms": round(req_elapsed * 1000)})

            if info.get("status") == 429:
                if retry_after:
                    log["retry_afters"].append(retry_after)
                    interval = max(interval, retry_after)
                else:
                    interval = min(interval * 2, 30.0)

                if phase == "ramp_up" and log["first_rpm_limit"] is None:
                    log["first_rpm_limit"] = round(60.0 / (interval / 2), 1)
                phase = "backoff"

        if log["requests_sent"] % 10 == 0:
            rpm_est = round(60.0 / max(interval, 0.1), 1)
            print(f"  [{model}] {log['requests_sent']} reqs, {log['successes']} ok, {log['rate_limits']} 429s, interval={interval:.1f}s (~{rpm_est} RPM), phase={phase}")

    log["end_time"] = datetime.now(timezone.utc).isoformat()
    log["duration_s"] = round(time.time() - start, 1)
    log["final_interval"] = round(interval, 2)
    if log["measured_rpm"] is None and log["successes"] > 0:
        log["measured_rpm"] = round(60.0 / max(interval, 0.1), 1)

    return log


def main():
    api_key = get_api_key()
    if not api_key:
        print("No API key found")
        sys.exit(1)

    models = [
        "xiaomi-mimo-2.5",
        "qwen-3.5-plus",
        "qwen-3.6-plus",
        "qwen-3.7-plus",
        "qwen-3.7-max",
        "qwen-3.8-max",
        "qwen-3.5-plus-thinking",
        "qwen-3.6-plus-thinking",
        "qwen-3.7-max-thinking",
        "qwen-3.8-max-thinking",
        "meta-muse-spark-1.2",
        "meta-muse-spark-1.3",
    ]

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Rate limit probe — {len(models)} models, ~90s each, ~20 min total")
    print(f"API: {API_URL}")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print()

    all_results = {}
    for i, model in enumerate(models):
        print(f"[{i+1}/{len(models)}] Probing {model}...")
        result = probe_model(model, api_key, max_seconds=90)
        all_results[model] = result
        print(f"  Done: {result['successes']} ok, {result['rate_limits']} 429s, measured RPM: {result['measured_rpm']}")
        print()

        with open(RESULTS_FILE, "w") as f:
            json.dump(all_results, f, indent=2)

    print(f"\nResults saved to {RESULTS_FILE}")
    print("\n" + "=" * 70)
    print(f"{'Model':<25s} {'RPM':>8s} {'429s':>6s} {'Retry-Afters':>15s}")
    print("-" * 70)
    for model, r in sorted(all_results.items(), key=lambda x: x[1].get("measured_rpm") or 0, reverse=True):
        rpm = r.get("measured_rpm", "?")
        rl = r.get("rate_limits", 0)
        ra = r.get("retry_afters", [])
        ra_str = ", ".join(f"{x:.0f}s" for x in ra[:3]) if ra else "-"
        print(f"{model:<25s} {str(rpm):>8s} {rl:>6s} {ra_str:>15s}")

    summary = {}
    for model, r in all_results.items():
        rpm = r.get("measured_rpm")
        if rpm and rpm > 0:
            summary[model] = {
                "measured_rpm": rpm,
                "min_interval": round(60.0 / rpm, 2),
                "retry_afters": r.get("retry_afters", []),
            }
    summary_file = STATE_DIR / "discovered_limits.json"
    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nDiscovered limits saved to {summary_file}")


if __name__ == "__main__":
    main()
