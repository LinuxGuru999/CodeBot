"""Cross-agent findings log for shared context.

Purpose
-------
Append-only JSONL file where discovery agents record findings and
control agents read them to inform prioritization.

Why
---
Agents operate in isolation. A shared append-only log lets discovery roles
(bug_hunter, security_auditor, test_gap_auditor) surface patterns that
scheduler and goal_steering can correlate without direct inter-agent communication.

Invariants
----------
- stdlib-only (json, time, pathlib, logging).
- Append-only; never modify or delete existing lines.
- Each line is a valid JSON object with FINDINGS_SCHEMA_KEYS.
- Corrupt lines are skipped on read, never crash the reader.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BOTS_DIR = Path(__file__).parent
PROJECT_ROOT = BOTS_DIR.parent
STATE_DIR = PROJECT_ROOT / ".codebot" / "state"
DEFAULT_FINDINGS_PATH = STATE_DIR / "findings.jsonl"

FINDINGS_SCHEMA_KEYS = frozenset({"ts", "bot", "type", "module", "finding", "severity"})

MAX_FINDINGS_LINES = 10000
MAX_FINDINGS_READ = 5000


def append_finding(
    bot: str,
    finding_type: str,
    module: str,
    finding: str,
    severity: str,
    path: Path | None = None,
) -> None:
    target = path or DEFAULT_FINDINGS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.time(),
        "bot": bot,
        "type": finding_type,
        "module": module,
        "finding": finding,
        "severity": severity,
    }
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    rotate_findings(target)


def read_findings(path: Path | None = None, limit: int = MAX_FINDINGS_READ) -> list[dict[str, Any]]:
    target = path or DEFAULT_FINDINGS_PATH
    if not target.exists():
        return []
    results: list[dict[str, Any]] = []
    try:
        lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and FINDINGS_SCHEMA_KEYS.issubset(obj.keys()):
                    results.append(obj)
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return results


def rotate_findings(path: Path | None = None, max_lines: int = MAX_FINDINGS_LINES) -> None:
    target = path or DEFAULT_FINDINGS_PATH
    try:
        if not target.exists():
            return
        lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
        if len(lines) > max_lines:
            target.write_text("\n".join(lines[-max_lines:]) + "\n", encoding="utf-8")
    except OSError:
        pass
