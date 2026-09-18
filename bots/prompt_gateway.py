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

logger = logging.getLogger("prompt_gateway")

MAX_CONCURRENT = int(os.getenv("BOTNET_MAX_CONCURRENT", "10"))
MIN_SPAWN_GAP = int(os.getenv("BOTNET_MIN_SPAWN_GAP", "20"))

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
    "Alignment Reward",
    "Self-Evolution",
    "Web Search",
    "Documentation Structure",
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
- Drain: before startup and between atomic tasks, check `{drain}` or `{update_lock}` via `read` tool. If either exists, exit 0 cleanly; you will be respawned.
- Heartbeat: write float Unix time to {heartbeat_file} at startup, after every atomic task, and every 60s while waiting on subagents. Never exceed 120s gap or you are restarted. Use the `write` tool with just the timestamp string.
- Checkpoint: after every atomic task (and at startup) write <4KB JSON to {ckpt_file} via tmp+replace. Keys: bot, scan_iteration, current_task, completed_tasks, queue_remaining, findings_so_far, updated_at, reason. On startup resume current_task/queue_remaining, never redo completed_tasks; prefer an injected CHECKPOINT HANDOFF block over the file.
- Alignment: on startup and clean exit read {align_scores} and {align_trigger}. Score 80+ clears stale triggers; score <60 with fresh trigger means PROMPT_OPTIMIZER will improve this bot's prompt. You do NOT self-evolve.
- Research: prefer web_search/webfetch/context7/gh code search over guessing; verify, then write.
- Bound: you are a bounded delegated task, not a daemon. Do atomic work, heartbeat, checkpoint, exit 0 on drain or SESSION_TIMEOUT."""


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
    """Assemble the final spawn message with compressed mission core."""
    core, removed = compress_prompt(prompt_text)
    contract = _common_contract(bot, heartbeat_file, ckpt_file, state_dir)
    parts = [
        f"Sisyphus — delegated task: '{bot}' workflow (model {model}).",
        "Remain Sisyphus; do not adopt a new identity. Execute the specification below as a bounded delegated task, not an infinite daemon.",
        contract,
        f"- State dir: {state_dir}  Log dir: {logs_dir}  Prompt: {prompt_name}",
    ]
    if ckpt_block:
        parts.append(ckpt_block)
    parts.append(
        f"--- Mission Spec ({prompt_name}) — compressed by gateway, shared boilerplate removed ---\n{core}"
    )
    message = "\n".join(parts)
    orig_tok, new_tok = len(prompt_text) // 4, len(message) // 4
    logger.info(
        f"Gateway '{bot}': stripped {len(removed)} sections, "
        f"prompt {len(prompt_text)}→{len(message)} chars (~{orig_tok}→~{new_tok} tok)"
    )
    return message


def running_count(bots: dict) -> int:
    """Count currently alive bot subprocesses (duck-typed BotState)."""
    n = 0
    for b in bots.values():
        proc = getattr(b, "process", None)
        if proc is not None and proc.poll() is None:
            n += 1
    return n


def spawn_allowed(bots: dict) -> tuple[bool, str]:
    """Check the global spawn gate without mutating state."""
    running = running_count(bots)
    if running >= MAX_CONCURRENT:
        return False, f"{running} running at cap {MAX_CONCURRENT}"
    gap = time.time() - _last_spawn_ts
    if gap < MIN_SPAWN_GAP:
        return False, f"spawn gap {gap:.0f}s < {MIN_SPAWN_GAP}s"
    return True, "slot available"


def note_spawn() -> None:
    """Record a successful spawn for gap pacing."""
    global _last_spawn_ts
    _last_spawn_ts = time.time()


def estimate_tokens(text: str) -> int:
    """Rough input-token estimate for logging."""
    return max(1, len(text) // 4)
