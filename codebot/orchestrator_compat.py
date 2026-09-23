"""Backward-compatible patchable wrappers for orchestrator.

Extracted from orchestrator.py to reduce its size. These wrappers
honor orchestrator-level STATE_DIR/DRAIN_FILE patches from tests
while delegating to the actual implementations in process_manager
and state_manager.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import sys
import time
from pathlib import Path
from typing import Any


def read_heartbeat(bot_name: str, state_dir_override: Any = None) -> float:
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    sd = state_dir_override
    if sd is not None:
        hb = Path(sd) / f"{bot_name}.heartbeat"
        if not hb.exists():
            return 0.0
        txt = hb.read_text().strip()
        try:
            return float(txt)
        except (ValueError, OSError):
            try:
                token = txt.split()[0].replace("Z", "+00:00")
                d = _dt.datetime.fromisoformat(token)
                if d.tzinfo is None:
                    d = d.replace(tzinfo=_dt.timezone.utc)
                ts = d.timestamp()
                now = time.time()
                return ts if (now - 86400 <= ts <= now + 60) else 0.0
            except Exception:
                return 0.0
    from codebot.process_manager import read_heartbeat as _pm_read_heartbeat
    return _pm_read_heartbeat(bot_name)


def checkpoint_path(bot_name: str, state_dir_override: Any = None) -> Path:
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    if state_dir_override is not None:
        return Path(state_dir_override) / f"{bot_name}.checkpoint.json"
    from codebot.process_manager import checkpoint_path as _pm_checkpoint_path
    return _pm_checkpoint_path(bot_name)


def read_checkpoint(bot_name: str, state_dir_override: Any = None) -> dict | None:
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    sd = state_dir_override
    if sd is not None:
        p = Path(sd) / f"{bot_name}.checkpoint.json"
        bak = p.with_suffix(".bak") if p.suffix == ".json" else Path(str(p) + ".bak")
        for target in (p, bak):
            if target.exists():
                try:
                    data = _json.loads(target.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
        return None
    from codebot.process_manager import read_checkpoint as _pm_read_checkpoint
    return _pm_read_checkpoint(bot_name)


def batch_read_heartbeats(bot_names: list, state_dir_override: Any = None) -> dict:
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    sd = state_dir_override
    if sd is not None:
        results = {}
        for name in bot_names:
            hb = Path(sd) / f"{name}.heartbeat"
            if not hb.exists():
                results[name] = 0.0
                continue
            txt = hb.read_text().strip()
            try:
                results[name] = float(txt)
            except (ValueError, OSError):
                try:
                    token = txt.split()[0].replace("Z", "+00:00")
                    d = _dt.datetime.fromisoformat(token)
                    if d.tzinfo is None:
                        d = d.replace(tzinfo=_dt.timezone.utc)
                    ts = d.timestamp()
                    now = time.time()
                    results[name] = ts if (now - 86400 <= ts <= now + 60) else 0.0
                except Exception:
                    results[name] = 0.0
        return results
    from codebot.process_manager import batch_read_heartbeats as _pm_batch
    return _pm_batch(bot_names)


def read_state_file(bot_name: str, state_dir_override: Any = None) -> dict:
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    sd = state_dir_override
    if sd is not None:
        sf = Path(sd) / f"{bot_name}.state.json"
        if sf.exists():
            try:
                data = _json.loads(sf.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
        return {}
    from codebot.orchestrator_services import _read_state_file as _svc
    return _svc(bot_name)


def is_draining(drain_file_override: Any = None) -> bool:
    """Patchable wrapper: honors orch.DRAIN_FILE patches from tests."""
    if drain_file_override is not None:
        return Path(drain_file_override).exists()
    from codebot.state_manager import is_draining as _sm_is_draining
    return _sm_is_draining()
