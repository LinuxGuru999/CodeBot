"""Alignment Service — RL pipeline and alignment scoring.

Purpose
-------
Owns the alignment/RL scoring pipeline logic. Orchestrator delegates to
this module instead of importing rl_engine or alignment_coordinator directly.

Invariants
----------
- Exposes run_alignment_pipeline and run_alignment_pipeline_for_all
- Never raises exceptions to callers (fail-open)
- Delegates all path resolution to state_manager.get_paths() (single source of truth)
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from codebot.state_manager import get_paths

logger = logging.getLogger(__name__)

_ALIGNMENT_SCORE_THRESHOLD = 60


def _ensure_rl_adapter() -> None:
    try:
        from codebot import rl_engine
        if rl_engine._adapter_instance is not None:
            return
        paths = get_paths()
        rl_engine.set_project_adapter(paths)
    except Exception:
        pass


def run_alignment_pipeline(bot_name: str) -> bool:
    _ensure_rl_adapter()
    paths = get_paths()
    state_dir = paths.state_dir
    events_dir = paths.alignment_events_dir
    if not events_dir.exists():
        return False

    rl_state_path = state_dir / "rl_state.json"
    try:
        from codebot.rl_engine import (
            load_rl_state, save_rl_state, ensure_bot,
            score_event, reward_from_score, record_event_reward,
            choose_pattern, update_q_value, decay_epsilon,
            write_trigger, DEFAULT_Q_VALUES,
        )
    except ImportError:
        return False

    base_role = bot_name.split("-")[0] if "-" in bot_name else bot_name

    state = load_rl_state(rl_state_path)
    processed = 0

    for f in events_dir.glob(f"{bot_name}*.exit.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("processed"):
            continue

        scored = score_event(data)
        score = scored["score"]
        exit_reason = data.get("exit_reason", "clean")
        bot_state = ensure_bot(state, base_role)
        prev_avg = bot_state.get("avg_reward", 0.0)

        reward = reward_from_score(
            score=score,
            exit_reason=exit_reason,
            rebellion_count=0,
            bot_avg_reward=prev_avg,
        )

        pattern, strategy = choose_pattern(bot_state)
        update_q_value(bot_state, pattern, reward)
        decay_epsilon(bot_state, reward, prev_avg)
        record_event_reward(
            state, base_role, reward, score,
            failure_count=1 if exit_reason != "clean" else 0,
            exit_reason=exit_reason,
        )

        if score < _ALIGNMENT_SCORE_THRESHOLD:
            try:
                triggers_dir = state_dir / "alignment_triggers"
                triggers_dir.mkdir(parents=True, exist_ok=True)
                trigger_path = triggers_dir / f"{base_role}.evolve.json"
                trigger_data = {
                    "bot": base_role,
                    "score": score,
                    "reward": reward,
                    "verdict": "evolve",
                    "reason": f"score {score} below threshold {_ALIGNMENT_SCORE_THRESHOLD}",
                    "pattern": pattern,
                    "strategy": strategy,
                    "timestamp": time.time(),
                }
                tmp = trigger_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(trigger_data, indent=2), encoding="utf-8")
                tmp.replace(trigger_path)
            except OSError:
                pass

        data["processed"] = True
        data["processed_at"] = time.time()
        data["reward"] = reward
        data["pattern"] = pattern
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(f)
        processed += 1

    if processed > 0:
        save_rl_state(state, rl_state_path)

    return processed > 0


def run_alignment_pipeline_for_all() -> None:
    paths = get_paths()
    state_dir = paths.state_dir
    events_dir = paths.alignment_events_dir
    if not events_dir.exists():
        return

    seen_bots: set[str] = set()
    for f in events_dir.glob("*.exit.json"):
        bot_name = f.stem.replace(".exit", "")
        if bot_name not in seen_bots:
            seen_bots.add(bot_name)
            try:
                run_alignment_pipeline(bot_name)
            except Exception as e:
                logger.warning("Alignment pipeline failed for %s: %s", bot_name, e)

    try:
        from codebot.prompt_optimizer import consume_triggers
        roles_dir = state_dir.parent / "roles"
        if not roles_dir.exists():
            from codebot.process_manager import BOTS_DIR
            roles_dir = BOTS_DIR / "roles"
        triggers_dir = state_dir / "alignment_triggers"
        consumed = consume_triggers(triggers_dir, roles_dir)
        if consumed:
            logger.info("Prompt optimizer consumed %d triggers", consumed)
    except Exception as e:
        logger.debug("consume_triggers failed: %s", e)
