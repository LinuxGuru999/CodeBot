#!/usr/bin/env python3
"""Clean up tickets: remove duplicates, fix bad titles, validate fields."""

import json
import hashlib
from pathlib import Path
from collections import Counter, defaultdict

STATE_DIR = Path("/home/kozuka/Work/CodeBot/.codebot/state")
TICKETS_FILE = STATE_DIR / "tickets.json"
BACKUP_FILE = STATE_DIR / "tickets.backup.json"

def load_tickets():
    data = json.loads(TICKETS_FILE.read_text())
    return data.get("tickets", [])

def save_tickets(tickets):
    data = {"schema_version": "2.0", "updated_at": __import__("time").time(), "tickets": tickets}
    TICKETS_FILE.write_text(json.dumps(data, indent=2))

def evidence_hash(t):
    canonical = f"{t.get('ticket_class','')}:{t.get('problem_statement','')}:{t.get('evidence','')}"
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]

def analyze_tickets(tickets):
    """Analyze tickets and return issues."""
    issues = []
    
    # Find duplicates by evidence hash
    hash_map = defaultdict(list)
    for t in tickets:
        h = evidence_hash(t)
        hash_map[h].append(t)
    
    for h, group in hash_map.items():
        if len(group) > 1:
            # Keep the oldest one
            oldest = min(group, key=lambda x: x.get("created_at", 0))
            for t in group:
                if t["id"] != oldest["id"]:
                    issues.append(("duplicate", t, f"Duplicate of {oldest['id']}"))
    
    # Find short/meaningless titles
    bad_titles = ["Test", "test", "TODO", "FIXME", "fix", "bug", "feature"]
    for t in tickets:
        title = t.get("title", "").strip()
        if len(title) < 10 or title in bad_titles:
            issues.append(("bad_title", t, f"Title too short or meaningless: '{title}'"))
    
    # Find missing fields
    for t in tickets:
        if not t.get("affected_modules"):
            issues.append(("missing_field", t, "Missing affected_modules"))
        if not t.get("evidence"):
            issues.append(("missing_field", t, "Missing evidence"))
        if not t.get("problem_statement"):
            issues.append(("missing_field", t, "Missing problem_statement"))
    
    # Find tickets with vague titles
    vague_patterns = ["missing from", "not listed in", "should be", "could be"]
    for t in tickets:
        title = t.get("title", "").lower()
        if any(p in title for p in vague_patterns):
            if t.get("ticket_class") == "documentation":
                issues.append(("vague", t, f"Documentation ticket may be low value: '{t.get('title', '')[:60]}'"))
    
    return issues

def deduplicate_tickets(tickets):
    """Remove duplicate tickets, keeping the oldest."""
    hash_map = defaultdict(list)
    for t in tickets:
        h = evidence_hash(t)
        hash_map[h].append(t)
    
    keep = []
    removed = []
    for h, group in hash_map.items():
        if len(group) > 1:
            # Keep the oldest
            oldest = min(group, key=lambda x: x.get("created_at", 0))
            keep.append(oldest)
            for t in group:
                if t["id"] != oldest["id"]:
                    removed.append(t["id"])
        else:
            keep.append(group[0])
    
    return keep, removed

def main():
    print("Loading tickets...")
    tickets = load_tickets()
    print(f"Total tickets: {len(tickets)}")
    
    # Backup
    import shutil
    shutil.copy2(TICKETS_FILE, BACKUP_FILE)
    print(f"Backup saved to {BACKUP_FILE}")
    
    # Analyze
    issues = analyze_tickets(tickets)
    print(f"\nIssues found: {len(issues)}")
    
    by_type = Counter(i[0] for i in issues)
    for issue_type, count in by_type.most_common():
        print(f"  {issue_type}: {count}")
    
    # Deduplicate
    deduped, removed_ids = deduplicate_tickets(tickets)
    print(f"\nAfter deduplication: {len(deduped)} tickets (removed {len(removed_ids)} duplicates)")
    
    # Save
    save_tickets(deduped)
    print(f"Saved to {TICKETS_FILE}")
    
    # Print removed duplicates
    if removed_ids:
        print(f"\nRemoved duplicate IDs:")
        for tid in removed_ids[:20]:
            print(f"  {tid}")
        if len(removed_ids) > 20:
            print(f"  ... and {len(removed_ids) - 20} more")

if __name__ == "__main__":
    main()
