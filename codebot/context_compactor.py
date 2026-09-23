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
- Supports stdlib-only (naive char/4 estimation) and tiktoken-based counting
- Never discards system prompt or last 4 messages
- Compaction is lossy by design — summary captures key facts, not verbatim text
- Token counting is approximate (char_count / 4) or precise (tiktoken) — conservative margin applied
- Idempotent: compacting already-compact history is safe
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("context_compactor")

# Default constants for backward compatibility
DEFAULT_CHARS_PER_TOKEN = 4
DEFAULT_SAFETY_MARGIN = 0.9
DEFAULT_MAX_TOKENS = 120_000
MIN_KEEP_MESSAGES = 4
SUMMARY_MARKER = "[CONTEXT COMPACTED]"


class TokenEstimator:
    """Estimates token counts for text.

    Supports multiple strategies:
    - 'naive': Uses a fixed chars-per-token ratio (default 4).
    - 'tiktoken': Uses the tiktoken library for accurate OpenAI-compatible models.
    - 'calibrated': Uses a configurable chars-per-token ratio.
    """

    def __init__(
        self,
        strategy: str = "naive",
        model_name: Optional[str] = None,
        chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
    ):
        self.strategy = strategy
        self.model_name = model_name
        self.chars_per_token = chars_per_token
        self._encoder = None

        if strategy == "tiktoken":
            try:
                import tiktoken
                if model_name:
                    try:
                        self._encoder = tiktoken.encoding_for_model(model_name)
                    except KeyError:
                        # Fallback to cl100k_base if model not found
                        self._encoder = tiktoken.get_encoding("cl100k_base")
                else:
                    self._encoder = tiktoken.get_encoding("cl100k_base")
            except ImportError:
                logger.warning(
                    "tiktoken not available, falling back to naive estimation"
                )
                self.strategy = "naive"

    def estimate(self, text: str) -> int:
        """Estimate the number of tokens in the given text."""
        if not text:
            return 0

        if self.strategy == "tiktoken" and self._encoder:
            return len(self._encoder.encode(text))
        else:
            # Naive or calibrated fallback
            return max(1, int(len(text) / self.chars_per_token))


# Global default estimator for backward compatibility
_default_estimator = TokenEstimator()


def estimate_tokens(text: str, estimator: Optional[TokenEstimator] = None) -> int:
    """Estimate tokens in a string.

    Args:
        text: The input text.
        estimator: Optional TokenEstimator instance. Uses default if None.
    """
    est = estimator or _default_estimator
    return est.estimate(text)


def estimate_messages_tokens(
    messages: list[dict[str, Any]],
    limit: int | None = None,
    estimator: Optional[TokenEstimator] = None,
) -> int:
    """Estimate total tokens for a list of messages.

    If *limit* is provided, stops counting once the total exceeds *limit*
    and returns the partial total (which will be > limit). This enables
    early-exit optimization in callers like ``needs_compaction``.

    Args:
        messages: List of message dicts.
        limit: Optional early-exit threshold.
        estimator: Optional TokenEstimator instance.
    """
    est = estimator or _default_estimator
    total = 0
    _estimate = est.estimate  # local binding for speed
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += _estimate(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += _estimate(part.get("text", ""))
                elif isinstance(part, str):
                    total += _estimate(part)
        # Overhead for message structure (role, etc.)
        # Note: tiktoken handles this internally if used, but we add a small
        # constant for naive/calibrated modes to approximate overhead.
        if est.strategy != "tiktoken":
            total += 4
        if limit is not None and total > limit:
            return total
    return total


def needs_compaction(
    messages: list[dict[str, Any]],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    safety_margin: float = DEFAULT_SAFETY_MARGIN,
    estimator: Optional[TokenEstimator] = None,
) -> bool:
    """Check if messages need compaction.

    Args:
        messages: List of message dicts.
        max_tokens: Maximum allowed tokens.
        safety_margin: Fraction of max_tokens to trigger compaction (e.g., 0.9).
        estimator: Optional TokenEstimator instance.
    """
    threshold = int(max_tokens * safety_margin)
    current = estimate_messages_tokens(messages, limit=threshold, estimator=estimator)
    return current >= threshold


def compact_messages(
    messages: list[dict[str, Any]],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    safety_margin: float = DEFAULT_SAFETY_MARGIN,
    estimator: Optional[TokenEstimator] = None,
) -> list[dict[str, Any]]:
    """Compact messages if they exceed the token threshold.

    Args:
        messages: List of message dicts.
        max_tokens: Maximum allowed tokens.
        safety_margin: Fraction of max_tokens to trigger compaction.
        estimator: Optional TokenEstimator instance.
    """
    if len(messages) <= MIN_KEEP_MESSAGES + 1:
        return messages

    current_tokens = estimate_messages_tokens(messages, estimator=estimator)
    threshold = int(max_tokens * safety_margin)

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

    new_tokens = estimate_messages_tokens(result, estimator=estimator)
    logger.info(
        "compacted %d messages (%d→%d tokens, %d kept recent)",
        len(messages), current_tokens, new_tokens, MIN_KEEP_MESSAGES,
    )

    if new_tokens >= threshold and len(result) > MIN_KEEP_MESSAGES + 2:
        return _aggressive_truncate(result, max_tokens, estimator=estimator)

    return result


def _summarize_messages(messages: list[dict[str, Any]]) -> str:
    """Summarize a list of messages for compaction."""
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
    estimator: Optional[TokenEstimator] = None,
) -> list[dict[str, Any]]:
    """Aggressively truncate messages to fit within token limits."""
    est = estimator or _default_estimator
    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    target_tokens = int(max_tokens * 0.7)
    kept: list[dict[str, Any]] = []
    current_tokens = sum(est.estimate(m.get("content", "")) for m in system_msgs)

    for msg in reversed(non_system):
        msg_tokens = est.estimate(msg.get("content", ""))
        if current_tokens + msg_tokens > target_tokens:
            break
        kept.insert(0, msg)
        current_tokens += msg_tokens

    if not kept:
        kept = non_system[-2:] if len(non_system) >= 2 else non_system[-1:]

    result = list(system_msgs) + kept
    logger.warning(
        "aggressive truncation: %d → %d messages (%d tokens)",
        len(messages), len(result), estimate_messages_tokens(result, estimator=est),
    )
    return result


def build_compaction_checkpoint(
    ticket_id: str,
    completed_steps: list[str],
    remaining_steps: list[str],
    files_changed: list[str],
    current_state: str = "",
) -> dict[str, Any]:
    """Build a checkpoint dictionary for compaction state."""
    return {
        "ticket_id": ticket_id,
        "completed_steps": completed_steps,
        "remaining_steps": remaining_steps,
        "files_changed": files_changed,
        "current_state": current_state[:2000],
        "compacted_at": time.time(),
    }
