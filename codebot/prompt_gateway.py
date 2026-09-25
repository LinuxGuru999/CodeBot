#!/usr/bin/env python3
"""Prompt Gateway — single pass-through for every bot spawn.

Purpose
-------
Compresses per-bot prompt files and assembles the final LLM message, and
gates spawns so the botnet cannot starve interactive sessions. The
orchestrator calls build_message() instead of inlining prompt text, and
checks spawn_allowed() before every start.

Why
---
Each prompt file repeats the same infra boilerplate (drain, heartbeat,
checkpoint, alignment, web-search — ~10KB each) on top of
its mission. Stripping it once here cuts ~2/3 of input tokens per spawn
without touching mission content. Concurrency is the token-burn proxy:
capping simultaneous opencode subprocesses bounds parallel API streams.

Invariants
----------
- stdlib-only, no imports from orchestrator (duck-types BotState).
- compress_prompt() only removes headings in _STRIP_PREFIXES; mission kept.
- Spawn gate never kills running bots; it only defers new spawns.
"""

import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("prompt_gateway")

try:
    from codebot.adaptive_rate_limiter import rate_limiter
    _HAS_RATE_LIMITER = True
except ImportError:
    _HAS_RATE_LIMITER = False
    rate_limiter = None  # type: ignore


def record_rate_limit(model: str, retry_after: Optional[float] = None) -> None:
    """Record a rate limit response for adaptive learning."""
    if _HAS_RATE_LIMITER and rate_limiter:
        rate_limiter.record_rate_limit(model, retry_after)


def record_success(model: str) -> None:
    """Record a successful API call for adaptive learning."""
    if _HAS_RATE_LIMITER and rate_limiter:
        rate_limiter.record_success(model)

_last_spawn_ts = 0.0

# T4.3 incremental adapter seam
_adapter_instance: object | None = None


def set_project_adapter(adapter: object) -> None:
    global _adapter_instance
    _adapter_instance = adapter


def get_adapter() -> object | None:
    return _adapter_instance

_STRIP_PREFIXES = (
    "Tool Usage Rules",
    "Safe-Update Protocol",
    "Heartbeat Protocol",
    "Checkpoint Store",
    "Checkpoint Protocol",
    "Claim Protocol",
    "Auto-Commit Protocol",
    "Alignment Reward",
    "Self-Evolution",
    "Web Search",
    "Documentation Structure",
    "Tool Usage Examples",
    "Tool Constraints",
    "Heartbeat Protocol",
    "Evolution",
)


def compress_prompt(prompt_text: str) -> tuple[str, list[str]]:
    """Return (mission_core, removed_headings) with shared boilerplate cut."""
    out: list[str] = []
    removed: list[str] = []
    stripped = False
    for line in prompt_text.splitlines():
        m = re.match(r"^## (.+?)\s*$", line)
        if m:
            title = m.group(1)
            if title.startswith(_STRIP_PREFIXES):
                stripped = True
                removed.append(title)
                continue
            stripped = False
            out.append(line)
            continue
        if stripped:
            continue
        out.append(line)
    core = re.sub(r"\n{3,}", "\n\n", "\n".join(out).strip())
    return core, removed


def _infer_state_dir(heartbeat_file: str, ckpt_file: str) -> Path:
    # T4.3: resolves state dir from absolute heartbeat/checkpoint paths,
    # falling back to adapter when no path context is available.
    # Bare 'state/...' would resolve to the wrong root, so the contract
    # must always emit absolute paths rooted at the passed state_dir.
    for cand in (heartbeat_file, ckpt_file):
        if not cand:
            continue
        p = Path(cand)
        for parent in p.parents:
            if parent.name == "state":
                return parent
        parent = p.parent
        if parent.name in ("heartbeats", "checkpoints", "alignment_events", "alignment_triggers", "backup", "claims"):
            return parent.parent
        return parent
    if _adapter_instance is not None:
        try:
            return _adapter_instance.paths().state_dir  # type: ignore[union-attr]
        except Exception:
            pass
    return Path(__file__).parent / "state"


def _common_contract(bot: str, heartbeat_file: str, ckpt_file: str, state_dir: str | None = None) -> str:
    sd = Path(state_dir) if state_dir is not None else _infer_state_dir(heartbeat_file, ckpt_file)
    drain = sd / ".drain"
    update_lock = sd / ".update_lock"
    align_scores = sd / "alignment_scores.json"
    align_trigger = sd / "alignment_triggers" / f"{bot}.evolve.json"
    return f"""[SHARED INFRA CONTRACT — identical for every bot]
- Drain: if `bash` is in your allowed tools list, before startup and between atomic tasks run `bash` with `python3 -m codebot.check_drain` to check for drain. If exit code is 0, exit 0 cleanly; you will be respawned. If `bash` is NOT in your allowed tools, SKIP this step entirely — the orchestrator handles drain gating for you. Do NOT attempt bash calls if your role does not permit them.
- Heartbeat: write float Unix time to {heartbeat_file} at startup, after every atomic task, and every 60s while waiting on subagents. Never exceed 120s gap or you are restarted. Use the `write` tool with just the timestamp string.
- Checkpoint: platform-owned only. Do NOT write {ckpt_file} directly; report progress through scratchpad/status updates. The runner validates and persists checkpoints. If you must include state, keep it under 4KB JSON with keys bot, updated_at, reason. On startup prefer an injected CHECKPOINT HANDOFF block over the file.
- Alignment: on startup and clean exit read {align_scores} and {align_trigger}. If either file does not exist, SKIP immediately and proceed to your mission — do NOT retry or treat as error. Score 80+ clears stale triggers; score <60 with fresh trigger means prompt evolution will improve this agent's prompt. You do NOT self-evolve.
- Research: prefer web_search/webfetch/context7/gh code search over guessing; verify, then write.
- Bound: you are a bounded delegated task, not a daemon. Do atomic work, heartbeat, checkpoint, exit 0 on drain or SESSION_TIMEOUT.
- Context compaction: if your conversation grows long, earlier messages may be summarized automatically. Always write critical state to your scratchpad file so it survives compaction.
- Scratchpad handoff: if you hit timeout, rate limit, or fatal error, your scratchpad is preserved. Another worker will read it and resume from where you stopped. Always update your scratchpad after significant progress.
- Web research: use `web_search` to look up documentation, CVEs, API references, or best practices before guessing. Use `web_fetch` to read specific URLs returned by search. Both are bounded and fail-open."""


def build_message(
    bot: str,
    model: str,
    prompt_text: str,
    heartbeat_file: str,
    ckpt_file: str,
    ckpt_block: str,
    state_dir: str,
    logs_dir: str,
    prompt_name: str,
) -> str:
    """Assemble the spawn message: identity header + shared infra contract + mission.
    ...
    """
    core, _ = compress_prompt(prompt_text)
    header = (
f"CodeBot — delegated task: '{bot}' workflow (model {model}).\n"
            f"Remain CodeBot; do not adopt a new identity. Execute the specification below as a bounded delegated task, not an infinite daemon.\n"
        f"State dir: {state_dir}  Log dir: {logs_dir}  Prompt: {prompt_name}\n"
    )
    contract = _common_contract(bot, heartbeat_file, ckpt_file, state_dir)
    parts = [header, contract, f"--- Mission Spec ({prompt_name}) ---\n{core}"]
    if ckpt_block:
        parts.append(ckpt_block)
    return "\n".join(parts)


def running_count(bots: dict) -> int:
    """Count currently alive bot subprocesses (duck-typed BotState)."""
    n = 0
    for b in bots.values():
        proc = getattr(b, "process", None)
        if proc is not None and proc.poll() is None:
            n += 1
    return n


def spawn_allowed(bots: dict, model: str = "") -> tuple[bool, str]:
    if _HAS_RATE_LIMITER and rate_limiter and model:
        can_spawn, reason = rate_limiter.can_spawn_now(model)
        if not can_spawn:
            return False, reason
    return True, "slot available"


def note_spawn() -> None:
    pass


def estimate_tokens(text: str) -> int:
    """Rough input-token estimate for logging."""
    return max(1, len(text) // 4)
