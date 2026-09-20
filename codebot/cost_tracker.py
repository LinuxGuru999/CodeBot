#!/usr/bin/env python3
"""Per-ticket cost attribution from the token ledger.

Purpose
-------
Bridges the fleet-wide token_ledger.json (maintained by token_budget.py)
with individual ticket lifecycle events, producing per-ticket cost records
that feed the economics engine described in CODEBOT-ROADMAP.md §15.

Why
---
The existing token ledger tracks daily totals by model but has no concept
of which ticket consumed those tokens. Without per-ticket attribution,
there is no way to compute "reliable accepted engineering work per dollar"
or learn which models perform best for which task classes.

Invariants
----------
- stdlib-only (json, time, os, pathlib)
- All writes are atomic via tmp->replace
- Cost records are append-only; never mutated after write
- Missing ledger data degrades to zero-cost, never crashes
- Ticket IDs are validated against CB-* pattern before recording
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TicketCost:
    ticket_id: str
    agent: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    phase: str
    timestamp: float
    wall_clock_seconds: float = 0.0
    attempts: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CostTracker:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._costs_path = state_dir / "ticket_costs.jsonl"
        self._summary_path = state_dir / "ticket_cost_summary.json"
        # Cache for build_summary results
        self._summary_cache: dict[str, Any] | None = None
        self._cache_file_mtime: float | None = None
        self._cache_file_size: int | None = None
        self._cache_lock = threading.Lock()

    def record_phase_cost(
        self,
        ticket_id: str,
        agent: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        phase: str,
        wall_clock_seconds: float = 0.0,
        attempts: int = 1,
    ) -> TicketCost:
        if not ticket_id or not ticket_id.startswith("CB-"):
            raise ValueError(f"invalid ticket_id format: {ticket_id}")
        entry = TicketCost(
            ticket_id=ticket_id,
            agent=agent,
            model=model,
            prompt_tokens=max(0, prompt_tokens),
            completion_tokens=max(0, completion_tokens),
            total_tokens=max(0, prompt_tokens) + max(0, completion_tokens),
            phase=phase,
            timestamp=time.time(),
            wall_clock_seconds=max(0.0, wall_clock_seconds),
            attempts=max(1, attempts),
        )
        self._append(entry)
        self.rotate()
        return entry

    def _append(self, entry: TicketCost) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.to_dict()) + "\n"
        fd = os.open(str(self._costs_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)

    def get_ticket_total(self, ticket_id: str) -> dict[str, object]:
        total_prompt = 0
        total_completion = 0
        total_tokens = 0
        phases: dict[str, int] = {}
        for entry in self._iter_entries(ticket_id):
            total_prompt += entry["prompt_tokens"]
            total_completion += entry["completion_tokens"]
            total_tokens += entry["total_tokens"]
            phase = entry.get("phase", "unknown")
            phases[phase] = phases.get(phase, 0) + entry["total_tokens"]
        return {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_tokens,
            "by_phase": phases,
        }

    MAX_COST_LINES = 20000
    MAX_COST_READ = 10000

    def _read_recent_lines(self, limit: int | None = None) -> list[str]:
        limit = self.MAX_COST_READ if limit is None else limit
        try:
            lines = self._costs_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return []
        return lines[-limit:]

    def rotate(self, max_lines: int | None = None) -> None:
        max_lines = self.MAX_COST_LINES if max_lines is None else max_lines
        try:
            if not self._costs_path.exists():
                return
            lines = self._costs_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if len(lines) > max_lines:
                self._costs_path.write_text("\n".join(lines[-max_lines:]) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _iter_entries(self, ticket_id: str) -> list[dict[str, Any]]:
        if not self._costs_path.exists():
            return []
        results = []
        for raw in self._read_recent_lines():
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("ticket_id") == ticket_id:
                    results.append(entry)
            except json.JSONDecodeError:
                continue
        return results

    def _is_cache_valid(self) -> bool:
        """Check if cached summary is still valid based on file mtime and size."""
        if self._summary_cache is None:
            return False
        if not self._costs_path.exists():
            # If file doesn't exist now but we have a cache, check if cache was for empty state
            if self._cache_file_mtime == 0 and self._cache_file_size == 0:
                return True
            return False
        try:
            stat = self._costs_path.stat()
            current_mtime = stat.st_mtime
            current_size = stat.st_size
            return (
                self._cache_file_mtime == current_mtime
                and self._cache_file_size == current_size
            )
        except OSError:
            return False

    def _update_cache_metadata(self) -> None:
        """Update cache metadata after computing a new summary."""
        if self._costs_path.exists():
            try:
                stat = self._costs_path.stat()
                self._cache_file_mtime = stat.st_mtime
                self._cache_file_size = stat.st_size
            except OSError:
                self._cache_file_mtime = None
                self._cache_file_size = None
        else:
            self._cache_file_mtime = 0
            self._cache_file_size = 0

    def build_summary(self) -> dict[str, Any]:
        with self._cache_lock:
            # Check if cache is valid
            if self._is_cache_valid():
                return self._summary_cache  # type: ignore[return-value]

            summary: dict[str, dict[str, Any]] = {}
            if not self._costs_path.exists():
                result: dict[str, Any] = {"tickets": {}, "fleet_totals": {}}
                self._summary_cache = result
                self._update_cache_metadata()
                return result
            try:
                for raw in self._read_recent_lines():
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    tid = entry.get("ticket_id", "unknown")
                    if tid not in summary:
                        summary[tid] = {
                            "total_tokens": 0,
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "phases": {},
                            "agents": set(),
                            "models": set(),
                            "attempts": 0,
                            "wall_clock_total": 0.0,
                        }
                    s = summary[tid]
                    s["total_tokens"] += entry.get("total_tokens", 0)
                    s["prompt_tokens"] += entry.get("prompt_tokens", 0)
                    s["completion_tokens"] += entry.get("completion_tokens", 0)
                    phase = entry.get("phase", "unknown")
                    s["phases"][phase] = s["phases"].get(phase, 0) + entry.get("total_tokens", 0)
                    s["agents"].add(entry.get("agent", "unknown"))
                    s["models"].add(entry.get("model", "unknown"))
                    s["attempts"] = max(s["attempts"], entry.get("attempts", 1))
                    s["wall_clock_total"] += entry.get("wall_clock_seconds", 0.0)
            except OSError:
                pass
            serializable = {}
            fleet_prompt = 0
            fleet_completion = 0
            fleet_total = 0
            for tid, s in summary.items():
                s["agents"] = sorted(s["agents"])
                s["models"] = sorted(s["models"])
                serializable[tid] = s
                fleet_prompt += s["prompt_tokens"]
                fleet_completion += s["completion_tokens"]
                fleet_total += s["total_tokens"]
            result = {
                "generated_at": time.time(),
                "tickets": serializable,
                "fleet_totals": {
                    "prompt_tokens": fleet_prompt,
                    "completion_tokens": fleet_completion,
                    "total_tokens": fleet_total,
                    "ticket_count": len(serializable),
                },
            }
            tmp = self._summary_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(result, indent=2), encoding="utf-8")
            tmp.replace(self._summary_path)
            self._summary_cache = result
            self._update_cache_metadata()
            return result
