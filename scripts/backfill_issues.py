#!/usr/bin/env python3
"""Bulk-import actionable CodeBot tickets to GitHub Issues (one-shot backfill).

Usage:
    python3 scripts/backfill_issues.py --dry-run [--limit N]
    python3 scripts/backfill_issues.py --live [--limit N] [--batch N] [--sleep S]

Scope: non-terminal tickets (everything except COMPLETE/REJECTED/DUPLICATE).
COMPLETE tickets already close on sync; terminal history is not imported.

Idempotent: skips ticket IDs already present as `[<id>]` issues (any state).
Resumable: --live writes .codebot/state/issue_backfill.json after each
batch; re-running continues where it left off.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

TERMINAL = {"COMPLETE", "REJECTED", "DUPLICATE"}
STATE_FILE = Path(".codebot/state/issue_backfill.json")

SEV_LABEL = {
    "critical": "priority-critical",
    "high": "priority-high",
    "medium": "priority-medium",
    "low": "priority-low",
}
CLASS_LABEL = {
    "bug": "bug",
    "security": "security",
    "performance": "performance",
    "feature": "enhancement",
    "documentation": "documentation",
    "test": "testing",
    "refactor": "refactor",
    "architecture": "architecture",
    "dependency": "dependencies",
}


def _gh(*args: str, timeout: int = 30) -> tuple[bool, str]:
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return False, str(e)


def _gh_rest(method: str, path: str, payload: dict | None = None,
             timeout: int = 30) -> tuple[bool, str]:
    """gh api call against REST (separate rate-limit pool from GraphQL)."""
    cmd = ["gh", "api", path, "-X", method]
    if payload is not None:
        for k, v in payload.items():
            if isinstance(v, list):
                for item in v:
                    cmd += ["-f", f"{k}[]={item}"]
            else:
                cmd += ["-f", f"{k}={v}"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return False, str(e)


def rest_issue_count() -> int:
    """Count all issues via REST pagination (no GraphQL quota)."""
    total, page = 0, 1
    while True:
        ok, out = _gh_rest("GET", "repos/LinuxGuru999/CodeBot/issues",
                           {"state": "all", "per_page": "100", "page": str(page)})
        if not ok or not out:
            break
        try:
            items = json.loads(out)
        except Exception:
            break
        if not items:
            break
        total += len([i for i in items if "pull_request" not in i])
        if len(items) < 100:
            break
        page += 1
    return total


def rest_existing_ids() -> set[str]:
    """All `[CB-...]` IDs via REST (no GraphQL quota)."""
    found: set[str] = set()
    page = 1
    import re as _re
    while True:
        ok, out = _gh_rest("GET", "repos/LinuxGuru999/CodeBot/issues",
                           {"state": "all", "per_page": "100", "page": str(page)})
        if not ok or not out:
            break
        try:
            items = json.loads(out)
        except Exception:
            break
        if not items:
            break
        for it in items:
            if "pull_request" in it:
                continue
            m = _re.search(r"\[(CB-[A-Za-z0-9-]+)\]", it.get("title", ""))
            if m:
                found.add(m.group(1))
        if len(items) < 100:
            break
        page += 1
    return found


def ensure_labels() -> None:
    _ensure_labels_body()


def _ensure_labels_body() -> None:
    ok, out = _gh("label", "list", "--limit", "100", "--json", "name")
    have = set()
    if ok and out:
        try:
            have = {i["name"] for i in json.loads(out)}
        except Exception:
            pass
    want = {"codebot", *SEV_LABEL.values(), *set(CLASS_LABEL.values())} | {"state-discovered",
        "state-planning", "state-implementing", "state-reviewing", "state-decompose"}
    colors = {"priority-critical": "B60205", "priority-high": "D93F0B",
              "priority-medium": "FBCA04", "priority-low": "0E8A16"}
    for label in sorted(want - have):
        color = colors.get(label, "1D76DB")
        ok, out = _gh("label", "create", label, "--color", color,
                      "--description", "CodeBot fleet")
        if not ok:
            print(f"  label {label}: {out[:120]}", file=sys.stderr)


def load_actionable(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    tickets = data.get("tickets", [])
    return [t for t in tickets if t.get("state") not in TERMINAL]


def existing_issue_ids() -> set[str]:
    """All `[CB-...]` IDs already on GitHub (open + closed), via search pages."""
    found: set[str] = set()
    page = 1
    while True:
        ok, out = _gh("issue", "list", "--search", "[CB-",
                      "--state", "all", "--limit", "100",
                      "--json", "title", "--page", str(page))
        if not ok or not out:
            break
        try:
            items = json.loads(out)
        except Exception:
            break
        if not items:
            break
        import re as _re
        for it in items:
            m = _re.search(r"\[(CB-[A-Za-z0-9-]+)\]", it.get("title", ""))
            if m:
                found.add(m.group(1))
        if len(items) < 100:
            break
        page += 1
    return found


def issue_body(t: dict) -> str:
    lines = [
        f"Ticket `{t.get('id')}` · `{t.get('state')}` · "
        f"{t.get('severity','?')}/{t.get('ticket_class','?')}",
        "",
        f"**Problem:** {t.get('problem_statement','')[:800]}",
        "",
        f"**Desired:** {t.get('desired_state','')[:800]}",
        "",
        f"**Modules:** {', '.join(t.get('affected_modules') or [])}",
    ]
    ac = t.get("acceptance_criteria", [])
    if ac:
        lines += ["", "**Acceptance:**"] + [f"- {a}"[:300] for a in ac[:10]]
    ev = t.get("evidence", "")
    if ev:
        lines += ["", f"**Evidence:** {ev[:500]}"]
    return "\n".join(lines)


def import_one(t: dict) -> tuple[bool, str]:
    tid = t.get("id", "")
    title = f"[{tid}] {str(t.get('title',''))[:120]}"
    labels = ["codebot"]
    sev = str(t.get("severity", "")).lower()
    if sev in SEV_LABEL:
        labels.append(SEV_LABEL[sev])
    cls = str(t.get("ticket_class", "")).lower()
    if cls in CLASS_LABEL:
        labels.append(CLASS_LABEL[cls])
    labels.append(f"state-{str(t.get('state','')).lower()}")
    ok, out = _gh_rest("POST", "repos/LinuxGuru999/CodeBot/issues",
                       {"title": title, "body": issue_body(t), "labels": labels})
    if not ok:
        return False, out[:200]
    try:
        num = str(json.loads(out).get("number", ""))
    except Exception:
        return False, out[:200]
    if not num.isdigit():
        return False, out[:200]
    url = f"https://github.com/LinuxGuru999/CodeBot/issues/{num}"
    _gh("project", "item-add", "1", "--owner", "LinuxGuru999", "--url", url)
    return True, url


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--store", type=str, default=".codebot/state/tickets.json")
    args = ap.parse_args()
    if not (args.dry_run or args.live):
        ap.error("pass --dry-run or --live")

    tickets = load_actionable(Path(args.store))
    if args.limit:
        tickets = tickets[:args.limit]
    print(f"actionable tickets in scope: {len(tickets)}")

    print("fetching existing [CB-*] issues for idempotency (REST, quota-safe)...")
    existing = rest_existing_ids()
    print(f"already mirrored: {len(existing)}")
    pending = [t for t in tickets if t.get("id") not in existing]
    print(f"pending import: {len(pending)}")

    done: dict[str, str] = {}
    if STATE_FILE.exists() and args.live:
        try:
            raw = json.loads(STATE_FILE.read_text()).get("imported", {})
            done = {k: v for k, v in raw.items() if v}
            print(f"resume state: {len(done)} with URLs (empty entries re-queued)")
        except Exception:
            pass
    pending = [t for t in pending if t.get("id") not in done]

    if args.dry_run:
        import collections as _c
        by_state = _c.Counter(t.get("state") for t in pending)
        by_sev = _c.Counter(t.get("severity") for t in pending)
        print(f"\nDRY RUN — would create {len(pending)} issues:")
        print(f"  by state: {dict(by_state)}")
        print(f"  by severity: {dict(by_sev)}")
        print(f"  batches of {args.batch}: "
              f"{(len(pending) + args.batch - 1) // args.batch}")
        print(f"  est. time at {args.sleep}s/issue: "
              f"{len(pending) * (args.sleep + 1.5) / 60:.0f} min")
        print("  sample titles:")
        for t in pending[:5]:
            print(f"    [{t.get('id')}] {str(t.get('title',''))[:70]}")
        return 0

    ensure_labels()
    created, failed = 0, 0
    backoff = float(__import__("os").environ.get("BACKFILL_BACKOFF", "5"))
    for i, t in enumerate(pending):
        ok, info = import_one(t)
        if ok:
            created += 1
            done[t["id"]] = info
            backoff = max(0.5, backoff / 2)
        else:
            failed += 1
            if "secondary rate limit" in info.lower():
                wait = min(backoff, 300)
                print(f"  RATE-LIMITED, sleeping {wait:.0f}s...", file=sys.stderr)
                time.sleep(wait)
                backoff = min(backoff * 2 + 5, 300)
                continue
            print(f"  FAIL {t.get('id')}: {info}", file=sys.stderr)
        if (i + 1) % args.batch == 0:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps({"imported": done}))
            print(f"  ... {i+1}/{len(pending)} (created {created}, failed {failed})")
        time.sleep(args.sleep)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"imported": done}))
    print(f"DONE — created {created}, failed {failed}, total {len(pending)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
