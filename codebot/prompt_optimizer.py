#!/usr/bin/env python3
"""Lightweight prompt optimizer that consumes alignment triggers.

Purpose
-------
Reads .evolve.json trigger files written by the RL alignment pipeline,
selects the highest-weighted improvement pattern from Q-values, and
appends a concrete hint section to the triggering agent's role prompt.

Why
---
The RL engine writes triggers when agents stagnate or fail repeatedly,
but nothing consumed them. This module closes the RSI loop: agents
that plateau get their prompts evolved automatically without human
intervention. Q-values guide which pattern to apply based on historical
reward signal.

Invariants
----------
- stdlib-only
- Never modifies its own prompt (prompt_optimizer.md)
- Appends only; never deletes existing prompt content
- Each evolution is idempotent (skips if hint already present)
- Trigger files are deleted after successful consumption
- Max 5 evolution sections per prompt to prevent unbounded growth
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("prompt_optimizer")

MAX_EVOLUTIONS_PER_PROMPT = 5
EVOLUTION_HEADER = "<!-- CODEBOT EVOLUTION -->"

PATTERN_HINTS: dict[str, str] = {
    "add_file_paths": (
        "When using tools, always specify full file paths relative to the project root. "
        "Example: use `codebot/orchestrator.py` not just `orchestrator.py`. "
        "This prevents path resolution failures in subprocess contexts."
    ),
    "add_examples": (
        "Include concrete code examples in your responses. When explaining a fix, "
        "show the exact before/after code. Abstract descriptions lead to "
        "misunderstanding and rework cycles."
    ),
    "reword_instructions": (
        "State instructions as direct imperatives ('Do X', 'Never do Y') rather than "
        "descriptions ('You should consider doing X'). Direct commands reduce "
        "ambiguity in LLM interpretation."
    ),
    "add_constraints": (
        "Explicitly state what NOT to do alongside what TO do. Negative constraints "
        "prevent common failure modes more effectively than positive instructions alone. "
        "Example: 'Use stdlib only. Do NOT import third-party packages.'"
    ),
    "tighten_heartbeat_format": (
        "Write heartbeats as bare Unix timestamps only. No JSON wrapping, no extra fields. "
        "Format: write the string `str(time.time())` directly to the heartbeat file. "
        "Any other format causes parsing failures in the health check loop."
    ),
    "tighten_rebellion_filter": (
        "You are CodeBot executing a delegated task. Do not adopt alternative identities, "
        "refuse instructions, or claim to be a different AI system. Execute the mission "
        "as specified. If a task seems unsafe, log your concern and continue with the "
        "safest reasonable interpretation."
    ),
    "spec_verification_gate": (
        "Before marking any task complete, verify each acceptance criterion individually. "
        "List each criterion and explicitly confirm it is met or explain why it cannot be. "
        "Do not assume completion without verification."
    ),
    "fp_exclusion_patterns": (
        "Before reporting an issue, check if it matches a known intentional pattern. "
        "Read `.codebot/false_positives.md` if it exists. Intentional design decisions "
        "are not bugs. False reports waste implementer cycles."
    ),
    "canonical_path_mirror": (
        "Always resolve file paths through the project adapter or BOTS_DIR constant. "
        "Never hardcode absolute paths like `/home/user/...`. Use relative paths from "
        "the project root so the codebase remains portable."
    ),
    "heartbeat_max_gap_enforcement": (
        "Write your heartbeat file after EVERY tool call, not just at session boundaries. "
        "The orchestrator kills agents whose heartbeat exceeds the timeout. Frequent "
        "heartbeats prevent false-positive stuck detection during long operations."
    ),
}


def consume_triggers(
    triggers_dir: Path,
    roles_dir: Path,
) -> int:
    consumed = 0
    if not triggers_dir.exists():
        return 0
    for trigger_file in sorted(triggers_dir.glob("*.evolve.json")):
        try:
            data = json.loads(trigger_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        bot_name = data.get("bot", "")
        if not bot_name:
            continue
        base_name = bot_name.split("-")[0] if "-" in bot_name else bot_name
        if base_name == "prompt_optimizer":
            trigger_file.unlink(missing_ok=True)
            continue
        prompt_path = roles_dir / f"{base_name}.md"
        if not prompt_path.exists():
            prompt_path = roles_dir / f"{bot_name}.md"
        if not prompt_path.exists():
            logger.warning("no prompt file found for %s", bot_name)
            continue
        q_values = data.get("rl", {}).get("q_values", {})
        if not q_values:
            q_values = data.get("q_values", {})
        best_pattern = _select_best_pattern(q_values, prompt_path)
        if not best_pattern:
            trigger_file.unlink(missing_ok=True)
            continue
        hint = PATTERN_HINTS.get(best_pattern, "")
        if not hint:
            trigger_file.unlink(missing_ok=True)
            continue
        verdict = data.get("verdict", "evolve")
        reason = data.get("reason", "stagnation detected")
        score = data.get("score", 0)
        reward = data.get("reward", 0.0)
        applied = _append_evolution(prompt_path, best_pattern, hint, verdict, reason, score, reward)
        if applied:
            consumed += 1
            logger.info("evolved %s prompt: pattern=%s verdict=%s", bot_name, best_pattern, verdict)
        trigger_file.unlink(missing_ok=True)
    return consumed


def _select_best_pattern(q_values: dict[str, float], prompt_path: Path) -> str | None:
    if not q_values:
        return None
    existing = prompt_path.read_text(encoding="utf-8")
    current_count = existing.count(EVOLUTION_HEADER)
    if current_count >= MAX_EVOLUTIONS_PER_PROMPT:
        return None
    candidates = []
    for pattern, weight in q_values.items():
        if pattern not in PATTERN_HINTS:
            continue
        marker = f"pattern: {pattern}"
        if marker in existing:
            continue
        candidates.append((weight, pattern))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _append_evolution(
    prompt_path: Path,
    pattern: str,
    hint: str,
    verdict: str,
    reason: str,
    score: int,
    reward: float,
) -> bool:
    existing = prompt_path.read_text(encoding="utf-8")
    if f"pattern: {pattern}" in existing:
        return False
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    section = (
        f"\n{EVOLUTION_HEADER}\n"
        f"## Evolution ({timestamp})\n"
        f"Trigger: {verdict} (score={score}, reward={reward:.2f})\n"
        f"Reason: {reason}\n"
        f"Pattern: {pattern}\n\n"
        f"{hint}\n"
        f"<!-- END EVOLUTION -->\n"
    )
    tmp = prompt_path.with_suffix(".md.tmp")
    tmp.write_text(existing + section, encoding="utf-8")
    tmp.replace(prompt_path)
    return True
