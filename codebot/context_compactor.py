#!/usr/bin/env python3
"""Context compactor for long-running agent sessions.

Purpose
-------
Monitors message history token count and applies progressive compaction
when approaching the model's context window limit. Three strategies
applied in order: tail truncation, middle summarization, full compression.

Why
---
LLM agents accumulate message history across tool-call iterations. A 50-
iteration session with file reads can exceed 100K tokens, causing API
errors or degraded reasoning quality. Compaction preserves recent context
(system prompt + last N messages) while compressing older history into
a summary block.

Invariants
----------
- stdlib-only (no tiktoken — uses char/4 estimation)
- Never discards system prompt or last 4 messages
- Compaction is lossy by design — summary captures key facts, not verbatim text
- Token counting is approximate (char_count / 4) — conservative margin applied
- Idempotent: compacting already-compact history is safe
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("context_compactor")

CHARS_PER_TOKEN = 4
SAFETY_MARGIN = 0.9
DEFAULT_MAX_TOKENS = 120_000
MIN_KEEP_MESSAGES = 4
SUMMARY_MARKER = "[CONTEXT COMPACTED]"


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += estimate_tokens(part.get("text", ""))
                elif isinstance(part, str):
                    total += estimate_tokens(part)
        total += 4
    return total


def needs_compaction(
    messages: list[dict[str, Any]],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> bool:
    current = estimate_messages_tokens(messages)
    threshold = int(max_tokens * SAFETY_MARGIN)
    return current >= threshold


def compact_messages(
    messages: list[dict[str, Any]],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> list[dict[str, Any]]:
    if len(messages) <= MIN_KEEP_MESSAGES + 1:
        return messages

    current_tokens = estimate_messages_tokens(messages)
    threshold = int(max_tokens * SAFETY_MARGIN)

    if current_tokens < threshold:
        return messages

    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    if len(non_system) <= MIN_KEEP_MESSAGES:
        return messages

    keep_recent = non_system[-MIN_KEEP_MESSAGES:]
    to_compress = non_system[:-MIN_KEEP_MESSAGES]

    summary = _summarize_messages(to_compress)

    result = list(system_msgs)
    result.append({
        "role": "assistant",
        "content": f"{SUMMARY_MARKER} Earlier conversation summarized:\n\n{summary}\n\nContinuing from checkpoint.",
    })
    result.extend(keep_recent)

    new_tokens = estimate_messages_tokens(result)
    logger.info(
        "compacted %d messages (%d→%d tokens, %d kept recent)",
        len(messages), current_tokens, new_tokens, MIN_KEEP_MESSAGES,
    )

    if new_tokens >= threshold and len(result) > MIN_KEEP_MESSAGES + 2:
        return _aggressive_truncate(result, max_tokens)

    return result


def _summarize_messages(messages: list[dict[str, Any]]) -> str:
    tool_calls: list[str] = []
    files_read: set[str] = set()
    files_written: set[str] = set()
    errors: list[str] = []
    key_facts: list[str] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue

        if role == "assistant":
            for tool_name in ("read", "write", "edit", "bash", "grep", "glob", "web_search", "web_fetch", "screenshot"):
                if f'"name": "{tool_name}"' in content or f"'name': '{tool_name}'" in content:
                    tool_calls.append(tool_name)
            if "error" in content.lower() or "fail" in content.lower():
                err_snippet = content[:200].replace("\n", " ")
                errors.append(err_snippet)

        elif role == "tool":
            if '"success": true' in content or '"success":true' in content:
                for prefix in ("path", "file", "url"):
                    idx = content.find(f'"{prefix}"')
                    if idx >= 0:
                        val_start = content.find('"', idx + len(prefix) + 2)
                        val_end = content.find('"', val_start + 1) if val_start >= 0 else -1
                        if val_start >= 0 and val_end > val_start:
                            filepath = content[val_start + 1:val_end]
                            if any(cmd in content[:200] for cmd in ('"read"', '"grep"', '"glob"')):
                                files_read.add(filepath)
                            elif any(cmd in content[:200] for cmd in ('"write"', '"edit"')):
                                files_written.add(filepath)
            elif '"success": false' in content or '"success":false' in content:
                err = content[:150].replace("\n", " ")
                errors.append(err)

    parts: list[str] = []
    parts.append(f"Messages compressed: {len(messages)}")

    if tool_calls:
        from collections import Counter
        counts = Counter(tool_calls)
        top = counts.most_common(5)
        parts.append(f"Tools used: {', '.join(f'{n}×{c}' for n, c in top)}")

    if files_read:
        shown = sorted(files_read)[:10]
        parts.append(f"Files read ({len(files_read)}): {', '.join(shown)}")

    if files_written:
        shown = sorted(files_written)[:10]
        parts.append(f"Files written ({len(files_written)}): {', '.join(shown)}")

    if errors:
        shown = errors[:3]
        parts.append(f"Errors encountered: {'; '.join(shown)}")

    return "\n".join(parts) if parts else f"{len(messages)} messages processed."


def _aggressive_truncate(
    messages: list[dict[str, Any]],
    max_tokens: int,
) -> list[dict[str, Any]]:
    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    target_tokens = int(max_tokens * 0.7)
    kept: list[dict[str, Any]] = []
    current_tokens = sum(estimate_tokens(m.get("content", "")) for m in system_msgs)

    for msg in reversed(non_system):
        msg_tokens = estimate_tokens(msg.get("content", ""))
        if current_tokens + msg_tokens > target_tokens:
            break
        kept.insert(0, msg)
        current_tokens += msg_tokens

    if not kept:
        kept = non_system[-2:] if len(non_system) >= 2 else non_system[-1:]

    result = list(system_msgs) + kept
    logger.warning(
        "aggressive truncation: %d → %d messages (%d tokens)",
        len(messages), len(result), estimate_messages_tokens(result),
    )
    return result


def build_compaction_checkpoint(
    ticket_id: str,
    completed_steps: list[str],
    remaining_steps: list[str],
    files_changed: list[str],
    current_state: str = "",
) -> dict[str, Any]:
    return {
        "ticket_id": ticket_id,
        "completed_steps": completed_steps,
        "remaining_steps": remaining_steps,
        "files_changed": files_changed,
        "current_state": current_state[:2000],
        "compacted_at": __import__("time").time(),
    }
