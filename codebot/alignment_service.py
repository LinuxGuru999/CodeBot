#!/usr/bin/env python3
"""Alignment Service — Decoupled alignment pipeline.

Purpose
-------
Runs alignment scoring and prompt optimization for bots after they exit.
This service is decoupled from the orchestrator to maintain architectural
boundaries. The orchestrator calls this service via a simple interface
without knowing about rl_engine internals.

Why
---
The orchestrator is a pure process manager and should not import rl_engine.
This module encapsulates all RL-engine-dependent alignment logic.

Invariants
----------
- Imports rl_engine internally only
- Atomic writes for state files
- Fails gracefully if rl_engine is unavailable
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from codebot.state_manager import get_paths

logger = logging.getLogger(__name__)


def _collect_reviewer_feedback_for_trigger(bot_name: str) -> list[dict]:
    """Collect recent reviewer feedback for prompt evolution.

    Scans review JSON files for findings related to the bot's recent work.
    Returns a list of feedback items that can be injected into the prompt
    optimizer to address recurring issues.
    """
    feedback_items = []
    state_dir = get_paths().state_dir
    review_files = [
        state_dir / "security_review.json",
        state_dir / "architecture_review.json",
        state_dir / "correctness_review.json",
        state_dir / "test_review.json",
        state_dir / "documentation_review.json",
    ]
    for rf in review_files:
        if rf.exists():
            try:
                data = json.loads(rf.read_text())
                if isinstance(data, dict):
                    findings = data.get("findings", [])
                    if isinstance(findings, list):
                        for finding in findings[-5:]:  # Last 5 findings
                            if isinstance(finding, dict):
                                feedback_items.append({
                                    "source": rf.name,
                                    "finding": finding.get("description", ""),
                                    "severity": finding.get("severity", "medium"),
                                })
            except Exception:
                pass
    return feedback_items


def run_alignment_pipeline(bot_name: str, timeout: int = 120) -> bool:
    """Run alignment scoring + prompt optimization synchronously for a bot.

    Called after bot exit. Blocks until complete or timeout. Returns True if
    prompt optimization was triggered (bot should wait for next run).
    """
    try:
        from codebot.rl_engine import (
            list_pending_events,
            score_event,
            reward_from_score,
            record_event_reward,
            mark_event_processed,
            write_trigger,
            load_rl_state,
            save_rl_state,
            ensure_bot,
        )
    except ImportError:
        try:
            from rl_engine import (
                list_pending_events,
                score_event,
                reward_from_score,
                record_event_reward,
                mark_event_processed,
                write_trigger,
                load_rl_state,
                save_rl_state,
                ensure_bot,
            )
        except ImportError:
            logger.warning("rl_engine not available, skipping alignment pipeline")
            return False

    events_dir = get_paths().alignment_events_dir
    event_file = events_dir / f"{bot_name}.exit.json"
    if not event_file.exists():
        return False

    try:
        event = json.loads(event_file.read_text())
    except Exception:
        return False

    if event.get("processed"):
        return False

    # Self-target guard: never trigger optimization on prompt_opt itself
    if bot_name == "prompt_opt":
        mark_event_processed(event_file, event, 0, 0.5, "self_target")
        return False

    start_time = time.time()
    logger.info(f"Alignment pipeline: scoring {bot_name} exit")

    try:
        score_result = score_event(event)
        metrics_entry: dict = {}
        _mc: Any = None
        try:
            from codebot import metrics_collector as _mc  # type: ignore
        except ImportError:
            _mc = None  # type: ignore
        if _mc is not None and getattr(_mc, "collect_all", None) is not None:
            try:
                _snap = _mc.collect_all()  # type: ignore[union-attr]
                _bots = _snap.get("bots") or {}
                if isinstance(_bots.get(bot_name), dict):
                    metrics_entry = _bots[bot_name]
            except Exception:
                metrics_entry = {}
        reward = reward_from_score(
            score_result["score"],
            event.get("exit_reason", "unknown"),
            score_result.get("rebellion_count", 0),
            metrics=metrics_entry or None,
        )
        rl = load_rl_state()
        record_event_reward(
            rl, bot_name, reward, score_result["score"],
            event.get("exit_code"), event.get("exit_reason", "clean"),
        )
        save_rl_state(rl)
        bot_state = ensure_bot(rl, bot_name)

        if reward < 0.6:
            write_trigger(
                bot_name, score_result["score"], reward,
                score_result.get("verdict", "misaligned"),
                score_result.get("evidence", "low reward"),
                score_result.get("breakdown", {}),
                event, bot_state,
            )
            logger.info(f"Alignment pipeline: {bot_name} reward={reward:.2f} < 0.6, trigger written")
            mark_event_processed(
                event_file, event,
                score_result["score"], reward,
                score_result.get("verdict", "misaligned"),
            )
            return True

        consec = int(bot_state.get("consecutive_failures", 0))
        if consec >= 3:
            reviewer_feedback = _collect_reviewer_feedback_for_trigger(bot_name)
            write_trigger(
                bot_name, score_result["score"], reward,
                "evolve_after_retries",
                f"{consec} consecutive failures, evolving prompt",
                score_result.get("breakdown", {}),
                event, bot_state,
                reviewer_feedback=reviewer_feedback,
            )
            logger.info(f"Alignment pipeline: {bot_name} {consec} consecutive failures, prompt evolution triggered with {len(reviewer_feedback)} feedback items")
            mark_event_processed(
                event_file, event,
                score_result["score"], reward,
                "evolve_after_retries",
            )
            return True

        total_runs = int(bot_state.get("total_runs", 0))
        last_improvement = float(bot_state.get("last_improvement", 0))
        stagnation_threshold = 5
        if total_runs >= stagnation_threshold and last_improvement > 0:
            runs_since_improve = total_runs - int(bot_state.get("_runs_at_last_improvement", 0))
            if runs_since_improve >= stagnation_threshold or (total_runs >= stagnation_threshold and not bot_state.get("_stagnation_triggered")):
                write_trigger(
                    bot_name, score_result["score"], reward,
                    "stagnation_evolve",
                    f"{total_runs} runs without noticeable improvement, evolving prompt",
                    score_result.get("breakdown", {}),
                    event, bot_state,
                )
                bot_state["_stagnation_triggered"] = True
                logger.info(f"Alignment pipeline: {bot_name} stagnation after {total_runs} runs, prompt evolution triggered")
                mark_event_processed(
                    event_file, event,
                    score_result["score"], reward,
                    "stagnation_evolve",
                )
                save_rl_state(rl)
                return True

        mark_event_processed(
            event_file, event,
            score_result["score"], reward,
            score_result.get("verdict", "aligned"),
        )
        logger.info(f"Alignment pipeline: {bot_name} reward={reward:.2f} >= 0.6, no trigger needed")
        return False

    except Exception as e:
        logger.error(f"Alignment pipeline error for {bot_name}: {e}")
        return False


def run_alignment_pipeline_for_all() -> None:
    """Run alignment scoring for all pending events (called every 30 minutes)."""
    try:
        from codebot.rl_engine import list_pending_events
    except ImportError:  # pragma: no cover - import fallback shim
        try:
            from rl_engine import list_pending_events  # type: ignore[no-redef]
        except ImportError:
            return

    pending = list_pending_events(get_paths().alignment_events_dir)
    if not pending:
        return

    logger.info(f"Alignment pipeline: processing {len(pending)} pending events")
    for item in pending:
        try:
            # list_pending_events returns list[tuple[Path, dict]]; accept
            # bare Paths as well for forward-compat with mocks.
            if isinstance(item, (tuple, list)) and len(item) == 2:
                event_file, _cached = item
            else:
                event_file = item  # type: ignore[assignment]
            event = json.loads(event_file.read_text())  # type: ignore[union-attr]
            bot_name = event.get("bot", "unknown")
            run_alignment_pipeline(bot_name)
        except Exception as e:
            logger.error(f"Error processing event {item}: {e}")
