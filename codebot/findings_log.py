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
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BOTS_DIR = Path(__file__).parent
PROJECT_ROOT = BOTS_DIR.parent
STATE_DIR = PROJECT_ROOT / ".codebot" / "state"
DEFAULT_FINDINGS_PATH = STATE_DIR / "findings.jsonl"

# T4.3 incremental adapter seam: when a ProjectAdapter is provided, its
# state_dir overrides the static default above (mirrors rl_engine.py /
# orchestrator.py). Also honours CODEBOT_STATE_DIR / CODEBOT_PROJECT_ROOT
# env vars so all agents resolve the same location without an adapter.
_adapter_instance: Any | None = None


def set_project_adapter(adapter: Any) -> None:
    """Inject a ProjectAdapter; its state_dir becomes the findings location."""
    global _adapter_instance, STATE_DIR, DEFAULT_FINDINGS_PATH
    _adapter_instance = adapter
    try:
        p = adapter.paths()  # type: ignore[union-attr]
        STATE_DIR = p.state_dir
        DEFAULT_FINDINGS_PATH = p.state_dir / "findings.jsonl"
    except Exception:
        pass


def get_adapter() -> Any | None:
    """Return the injected ProjectAdapter, if any."""
    return _adapter_instance


def _resolve_state_dir() -> Path:
    """Resolve the project state dir: adapter > env > static default."""
    if _adapter_instance is not None:
        try:
            return _adapter_instance.paths().state_dir  # type: ignore[union-attr]
        except Exception:
            pass
    env_state = os.environ.get("CODEBOT_STATE_DIR")
    if env_state:
        return Path(env_state)
    env_root = os.environ.get("CODEBOT_PROJECT_ROOT")
    if env_root:
        return Path(env_root) / ".codebot" / "state"
    return STATE_DIR


def get_default_findings_path() -> Path:
    """Return the findings.jsonl path all agents should share."""
    return _resolve_state_dir() / "findings.jsonl"

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
    target = path or get_default_findings_path()
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
    target = path or get_default_findings_path()
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
    target = path or get_default_findings_path()
    try:
        if not target.exists():
            return
        lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
        if len(lines) > max_lines:
            target.write_text("\n".join(lines[-max_lines:]) + "\n", encoding="utf-8")
    except OSError:
        pass
