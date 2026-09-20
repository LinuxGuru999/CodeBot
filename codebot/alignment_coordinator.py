"""Alignment Coordinator — Post-exit alignment events and pipeline coordination."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

def write_alignment_event(bot_name: str, exit_code: int | None, exit_reason: str, started_at: float | None = None) -> None:
    try:
        from codebot.alignment_service import _write_alignment_event as _wae
        _wae(bot_name, exit_code, exit_reason, started_at)
    except Exception as e:
        logger.warning(f"Failed to write alignment event for {bot_name}: {e}")
