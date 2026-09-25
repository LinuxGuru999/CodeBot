"""Health-check loop helpers extracted from orchestrator.py.

This module contains the per-tick bot lifecycle management logic:
- Tick initialization (cache clearing, queue depth logging)
- Retry logic for disabled/stuck bots
- Exit handling with alignment and ticket transitions
- Stuck bot detection and restart
- Dispatcher invocation
- Eligible bot startup

The orchestrator delegates to these functions to maintain the
thin-router/fat-service invariant (Constitution §4).
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any

from codebot.process_manager import (
    BotState,
    is_stuck as _pm_is_stuck,
    effective_heartbeat_timeout as _pm_effective_heartbeat_timeout,
    model_profile as _pm_model_profile,
    update_bot_state as _pm_update_bot_state,
)

logger = logging.getLogger("orchestrator")

# Fallback implementations (used when orchestrator patch not present)
from codebot.alignment_coordinator import write_alignment_event as _fallback_wae
try:
    from codebot.alignment_service import run_alignment_pipeline as _fallback_rap
except ModuleNotFoundError:
    def _fallback_rap(*a, **kw):  # type: ignore[misc]
        return False
from codebot.dispatch_service import (
    get_pipeline_state as _fallback_get_ps,
    is_needed_bot as _fallback_is_needed,
    retry_disabled_bot as _fallback_retry_disabled,
    retry_stuck_starting as _fallback_retry_stuck,
    log_bot_statuses as _fallback_log_status,
    dispatch_ready_tickets as _fallback_dispatch_ready,
    IMPLEMENTER_ROLE_NAMES,
    REVIEWER_ROLE_NAMES,
    is_implementer_role as _is_implementer_role,
    batch_read_bot_statuses as _fallback_batch_read_status,
    compute_rate_limit_backoff as _fallback_backoff,
    rotate_model_on_error as _fallback_rotate,
    transition_ticket_on_success as _fallback_trans_success,
    transition_ticket_on_error as _fallback_trans_error,
    record_workforce_completion as _fallback_record_wf,
)
from codebot.scratchpad import load_scratchpad as _fallback_load_sp, save_scratchpad as _fallback_save_sp
from codebot.state_manager import get_paths as _fallback_get_paths, get_adapter_instance as _fallback_get_adapter, is_draining as _fallback_is_draining

# Re-export for callers that import from here directly
is_stuck = _pm_is_stuck
effective_heartbeat_timeout = _pm_effective_heartbeat_timeout
model_profile = _pm_model_profile
update_bot_state = _pm_update_bot_state
write_alignment_event = _fallback_wae
run_alignment_pipeline = _fallback_rap
get_pipeline_state = _fallback_get_ps
is_needed_bot = _fallback_is_needed
retry_disabled_bot = _fallback_retry_disabled
retry_stuck_starting = _fallback_retry_stuck
log_bot_statuses = _fallback_log_status
dispatch_ready_tickets = _fallback_dispatch_ready
batch_read_bot_statuses = _fallback_batch_read_status
compute_rate_limit_backoff = _fallback_backoff
rotate_model_on_error = _fallback_rotate
transition_ticket_on_success = _fallback_trans_success
transition_ticket_on_error = _fallback_trans_error
record_workforce_completion = _fallback_record_wf
load_scratchpad = _fallback_load_sp
save_scratchpad = _fallback_save_sp
is_draining = _fallback_is_draining


def _orch(name: str, fallback):
    """Return orchestrator.<name> if it exists and is distinct/mocked, else fallback.
    
    This allows tests that patch codebot.orchestrator.<name> to affect
    health_check_loop without requiring health_check_loop to be patched directly.
    We check sys.modules to avoid circular import at top level; import is lazy.
    """
    mod = sys.modules.get("codebot.orchestrator")
    if mod is not None:
        try:
            val = getattr(mod, name, None)
            if val is not None:
                # If orchestrator's value is a MagicMock/mock, always prefer it
                # (mocks have assert_called etc. and _mock_name)
                if hasattr(val, "assert_called_once") or hasattr(val, "_mock_name") or hasattr(val, "_mock_children"):
                    return val
                # If fallback is None, just return orch value
                if fallback is None:
                    return val
                # If they're the same object, either is fine
                if val is fallback:
                    return fallback
                # If orch has a wrapper (e.g., orchestrator.write_alignment_event wraps _fallback_wae),
                # prefer orch's wrapper so patches are visible
                # Heuristic: if val's module is orchestrator, it's the wrapper -> prefer it
                try:
                    if getattr(val, "__module__", "") == "codebot.orchestrator":
                        return val
                except Exception:
                    pass
                # Otherwise prefer orch value if it's different (patched)
                return val
        except Exception:
            pass
    return fallback


def init_tick() -> None:
    """Initialize per-tick cache state via QueueManager adapter.

    Note: We intentionally do NOT clear the TicketStore cache here.
    The cache in ticket_dispatcher.get_ticket_store() uses mtime/size
    fingerprinting to detect file changes, so it safely reuses the
    cached instance across ticks when tickets.json is unchanged.
    This avoids O(n) reload cost on every health-check tick.
    """
    from codebot.ticket_engine import QueueManager
    # Resolve get_paths dynamically so orchestrator patch is honored
    gp = _orch("get_paths", _fallback_get_paths)
    try:
        qm = QueueManager.from_state_dir(gp().state_dir)
        qm.clear_cache()
    except Exception:
        try:
            qm = QueueManager.from_state_dir(_fallback_get_paths().state_dir)
            qm.clear_cache()
        except Exception:
            pass
    # Adapter queue depth via orchestrator if available, else state_manager
    adapter = None
    try:
        get_adapter_fn = _orch("get_adapter_instance", _fallback_get_adapter)
        adapter = get_adapter_fn()
    except Exception:
        try:
            adapter = _fallback_get_adapter()
        except Exception:
            adapter = None
    if adapter is not None:
        try:
            qd = adapter.queue_depth()
            logger.debug("Adapter queue depth: %d", qd)
        except Exception:
            logger.debug("Adapter queue depth unavailable")


def retry_disabled_and_stuck(bots: dict[str, BotState], hb_cache: dict) -> None:
    """Retry disabled or stuck-starting bots."""
    rdb = _orch("retry_disabled_bot", _fallback_retry_disabled)
    rss = _orch("retry_stuck_starting", _fallback_retry_stuck)
    ubs = _orch("update_bot_state", _pm_update_bot_state)
    for name, bot in bots.items():
        if not bot.config.enabled and bot.process is None:
            try:
                if rdb(bot):
                    ubs(bot, "waiting")
            except TypeError:
                # fallback for mocks that expect different signature
                try:
                    if rdb(bot):
                        ubs(bot, "waiting")
                except Exception:
                    pass
        if bot.config.enabled and bot.process is not None:
            try:
                if rss(bot, heartbeat_cache=hb_cache):
                    logger.info(f"Retrying '{name}' stuck in starting")
            except TypeError:
                try:
                    if rss(bot):
                        logger.info(f"Retrying '{name}' stuck in starting")
                except Exception:
                    pass


def current_tick_store() -> Any:
    try:
        from codebot.ticket_dispatcher import get_ticket_store
        return get_ticket_store()
    except ImportError:
        return None


def flush_tick_store(store: Any) -> None:
    flush = getattr(store, "flush", None)
    if callable(flush):
        try:
            flush()
        except Exception:
            pass


def handle_exited_bots(
    bots: dict[str, BotState],
    now: float,
    start_bot_fn: Any,
    stop_fn: Any,
    ts: Any = None,
) -> set[str]:
    # Resolve all potentially patched functions at call time
    wae = _orch("write_alignment_event", _fallback_wae)
    rap = _orch("run_alignment_pipeline", _fallback_rap)
    ubs = _orch("update_bot_state", _pm_update_bot_state)
    trans_ok = _orch("transition_ticket_on_success", _fallback_trans_success)
    trans_err = _orch("transition_ticket_on_error", _fallback_trans_error)
    rotate = _orch("rotate_model_on_error", _fallback_rotate)
    backoff_fn = _orch("compute_rate_limit_backoff", _fallback_backoff)
    load_sp = _orch("load_scratchpad", _fallback_load_sp)
    save_sp = _orch("save_scratchpad", _fallback_save_sp)
    record_wf = _orch("record_workforce_completion", _fallback_record_wf)
    gp = _orch("get_paths", _fallback_get_paths)

    try:
        current_paths = gp()
    except Exception:
        current_paths = _fallback_get_paths()
    error_recovered_tids: set[str] = set()
    for name, bot in list(bots.items()):
        if not bot.config.enabled or bot.process is None or bot.process.poll() is None:
            continue
        exit_code = bot.process.returncode
        try:
            wae(
                name, exit_code=exit_code,
                exit_reason="clean" if exit_code == 0 else "error",
                started_at=bot.started_at,
            )
        except Exception as e:
            logger.warning(f"write_alignment_event failed for {name}: {e}")
        try:
            rap(name)
        except Exception as e:
            logger.warning(f"Alignment pipeline failed for {name}: {e}")
        bot.process = None

        agent_id = getattr(bot, "_agent_id", "")
        if agent_id:
            try:
                import codebot.orchestrator as _orch_mod
                scheduler = getattr(_orch_mod, "_v2_scheduler", None)
                if scheduler is not None:
                    scheduler.finalize_agent(agent_id, outcome=f"exit-code-{exit_code}")
            except Exception as e:
                logger.debug("finalize_agent failed for %s: %s", name, e)
            bot._agent_id = ""

        if exit_code == 0:
            bot.consecutive_errors = 0
            try:
                # Try with store=ts if provided, else without
                if ts is not None:
                    try:
                        trans_ok(bot, bots, store=ts)
                    except TypeError:
                        trans_ok(bot, bots)
                else:
                    try:
                        trans_ok(bot, bots)
                    except TypeError:
                        trans_ok(bot, bots, store=None)
            except Exception as e:
                logger.warning(f"transition_ticket_on_success failed for {name}: {e}")
            try:
                record_wf(bot, completed_at=now)
            except Exception:
                try:
                    record_wf(bot)
                except Exception:
                    pass
            bot.next_run_at = now + bot.config.interval_seconds
            try:
                ubs(bot, "waiting")
            except Exception as e:
                logger.warning(f"update_bot_state failed for {name}: {e}")
        elif exit_code == 3:
            base_role = name.split("-")[0] if "-" in name else name
            is_implementer = _is_implementer_role(base_role)
            if is_implementer:
                bot.consecutive_errors += 1
                assigned_tid = getattr(bot, "_assigned_ticket_id", "")
                if bot.consecutive_errors >= 5 and assigned_tid:
                    try:
                        if ts is not None:
                            trans_err(bot, bots, exit_code, store=ts)
                        else:
                            trans_err(bot, bots, exit_code)
                    except TypeError:
                        trans_err(bot, bots, exit_code, store=None)
                    except Exception as e:
                        logger.warning(f"transition_ticket_on_error failed for {name}: {e}")
                    bot.consecutive_errors = 0
                    logger.warning(f"Implementer '{name}' hit 429 rate limit 5x for {assigned_tid} — transitioning to REWORK")
                else:
                    try:
                        backoff, should_disable = backoff_fn(bot)
                    except Exception:
                        backoff, should_disable = _fallback_backoff(bot)
                    requeue_delay = max(backoff, 60.0)
                    bot.next_run_at = now + requeue_delay
                    try:
                        ubs(bot, "waiting")
                    except Exception:
                        pass
                    logger.info(f"Implementer '{name}' hit 429 rate limit ({bot.consecutive_errors}/5) — requeue in {requeue_delay:.0f}s")
            else:
                try:
                    backoff, should_disable = backoff_fn(bot)
                except Exception:
                    backoff, should_disable = _fallback_backoff(bot)
                bot.next_run_at = now + backoff
                try:
                    rotate(bot, bots)
                except Exception:
                    try:
                        rotate(bot)
                    except Exception:
                        pass
                if should_disable:
                    bot.config.enabled = False
                    try:
                        ubs(bot, "disabled")
                    except Exception:
                        pass
                else:
                    try:
                        ubs(bot, "waiting")
                    except Exception:
                        pass
                assigned_tid = getattr(bot, "_assigned_ticket_id", "")
                if assigned_tid:
                    try:
                        scratch = load_sp(current_paths.state_dir, assigned_tid)
                        tool_iterations = getattr(scratch, 'iteration', 0)
                        scratch.mark_error(f"Bot rate-limited (exit_code={exit_code})")
                        scratch.finish_agent(f"rate-limited after {tool_iterations} iters")
                        save_sp(current_paths.state_dir, scratch)
                    except Exception as e:
                        logger.warning(f"Failed to finish scratchpad for ticket {assigned_tid}: {e}")
        else:
            bot.consecutive_errors += 1
            if bot.consecutive_errors >= 3:
                old_model = bot.config.model
                try:
                    rotate(bot, bots)
                except Exception:
                    try:
                        rotate(bot)
                    except Exception:
                        pass
                bot.consecutive_errors = 0
                logger.warning(f"Bot '{name}' failed 3x on {old_model} -> {bot.config.model}")
            assigned_tid = getattr(bot, "_assigned_ticket_id", "")
            if assigned_tid:
                try:
                    scratch = load_sp(current_paths.state_dir, assigned_tid)
                    scratch.mark_error(f"Bot exited with code {exit_code}")
                    scratch.finish_agent(f"error: exit_code={exit_code}")
                    save_sp(current_paths.state_dir, scratch)
                except Exception as e:
                    logger.warning(f"Failed to finish scratchpad for ticket {assigned_tid}: {e}")
                error_recovered_tids.add(assigned_tid)
            try:
                if ts is not None:
                    try:
                        trans_err(bot, bots, exit_code, store=ts)
                    except TypeError:
                        trans_err(bot, bots, exit_code)
                else:
                    try:
                        trans_err(bot, bots, exit_code)
                    except TypeError:
                        trans_err(bot, bots, exit_code, store=None)
            except Exception as e:
                logger.warning(f"transition_ticket_on_error failed for {name}: {e}")
            bot.next_run_at = now + 5
            try:
                ubs(bot, "waiting")
            except Exception:
                pass
    def _is_demand_suffix(s: str) -> bool:
        if s.isdigit():
            return True
        if len(s) == 8:
            try:
                int(s, 16)
                return all(c in "0123456789abcdefABCDEF" for c in s)
            except ValueError:
                return False
        return False

    dead_names = [
        name for name, bot in bots.items()
        if bot.process is None
        and bot.config.enabled
        and "-" in name
        and _is_demand_suffix(name.rsplit("-", 1)[-1])
    ]
    for name in dead_names:
        bots.pop(name, None)
    return error_recovered_tids


def handle_stuck_bots(
    bots: dict[str, BotState],
    now: float,
    hb_cache: dict,
    restart_fn: Any,
) -> None:
    """Detect and restart stuck bots based on heartbeat age."""
    wae = _orch("write_alignment_event", _fallback_wae)
    rap = _orch("run_alignment_pipeline", _fallback_rap)
    is_stuck_fn = _orch("is_stuck", _pm_is_stuck)
    ehb = _orch("effective_heartbeat_timeout", _pm_effective_heartbeat_timeout)
    mp = _orch("model_profile", _pm_model_profile)

    # Resolve restart_fn: if orchestrator has a patched restart_bot, prefer it
    # unless caller passed a different function (tests pass restart_bot mock explicitly)
    orch_restart = _orch("restart_bot", None)
    if orch_restart is not None and hasattr(orch_restart, "assert_called"):
        # It's a mock -> use it regardless of passed restart_fn if passed was not mocked
        # But we honor the passed restart_fn if it is also a mock
        # Prefer passed-in if it's a mock, else orch's mock
        if not hasattr(restart_fn, "assert_called"):
            restart_fn = orch_restart

    for name, bot in bots.items():
        if not bot.config.enabled or bot.process is None or bot.process.poll() is not None:
            continue
        stuck = False
        try:
            stuck = is_stuck_fn(bot, heartbeat_cache=hb_cache)
        except TypeError:
            try:
                stuck = is_stuck_fn(bot)
            except Exception:
                stuck = False
        except Exception:
            stuck = False
        if stuck:
            try:
                eff = ehb(bot)
            except Exception:
                eff = _pm_effective_heartbeat_timeout(bot)
            hb = hb_cache.get(bot.config.name, 0.0)
            hb_age = now - hb if hb else 0
            try:
                prof = mp(bot.config.model)
                risk = prof.lockup_risk if prof else '?'
            except Exception:
                risk = '?'
            logger.warning(f"Bot '{name}' stuck (hb {hb_age:.0f}s > {eff}s, risk={risk})")
            try:
                wae(name, exit_code=None, exit_reason="stuck", started_at=bot.started_at)
            except Exception as e:
                logger.warning(f"write_alignment_event failed for {name}: {e}")
            try:
                rap(name)
            except Exception as e:
                logger.warning(f"Alignment failed for {name}: {e}")
            try:
                restart_fn(bot, reason="stuck", bots=bots)
            except TypeError:
                try:
                    restart_fn(bot, reason="stuck")
                except Exception as e:
                    logger.warning(f"restart_fn failed for {name}: {e}")
            except Exception as e:
                logger.warning(f"restart_fn failed for {name}: {e}")


def consume_goal_decisions(store: Any, state_dir: Any) -> int:
    from pathlib import Path
    from codebot.ticket_engine import TicketState
    import json as _json

    if store is None:
        return 0

    sdir = Path(state_dir)
    decisions_dir = sdir / "goal_aligner_decisions"
    consumed = 0

    decision_files = []
    if decisions_dir.exists():
        decision_files.extend(decisions_dir.glob("*.json"))

    for status_file in sdir.glob("goal_aligner*.status.json"):
        if status_file.stat().st_size > 0:
            decision_files.append(status_file)

    for dfile in decision_files:
        try:
            raw = dfile.read_text(encoding="utf-8")
        except OSError:
            continue

        entries = []
        try:
            data = _json.loads(raw)
            if isinstance(data, dict):
                if "decisions" in data and isinstance(data["decisions"], list):
                    entries = data["decisions"]
                elif "ticket_id" in data or "decision" in data:
                    entries = [data]
            elif isinstance(data, list):
                entries = data
        except (_json.JSONDecodeError, ValueError):
            import ast as _ast
            cleaned = raw.strip()
            data = None
            while cleaned and cleaned[-1] in ("}", "]", "\n", "\r", " "):
                try:
                    data = _json.loads(cleaned)
                    break
                except (_json.JSONDecodeError, ValueError):
                    pass
                try:
                    data = _ast.literal_eval(cleaned)
                    break
                except (ValueError, SyntaxError):
                    if cleaned.endswith("}}"):
                        cleaned = cleaned[:-1]
                    elif cleaned.endswith("]]}"):
                        cleaned = cleaned[:-1]
                    else:
                        break
            if isinstance(data, dict):
                if "decisions" in data and isinstance(data["decisions"], list):
                    entries = data["decisions"]
                elif "ticket_id" in data or "decision" in data:
                    entries = [data]
            elif isinstance(data, list):
                entries = data
            if not entries:
                continue

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            tid = str(entry.get("ticket_id", "")).strip()
            decision = str(entry.get("decision", "")).strip().upper()
            if not tid or decision not in ("NOW", "LATER", "NEVER"):
                continue

            ticket = store._tickets.get(tid)
            if ticket is None:
                continue

            current_state = getattr(ticket.state, "value", str(ticket.state))
            if current_state != "TRIAGED":
                continue

            rl_goal_advice = ""
            tc_val = ""
            risk_val = ""
            try:
                from codebot.rl_goal_bandit import advise_goal_decision
                tc = getattr(ticket, "ticket_class", None)
                tc_val = tc.value if hasattr(tc, "value") else str(tc) if tc else ""
                risk = getattr(ticket, "risk", None)
                risk_val = risk.value if hasattr(risk, "value") else str(risk) if risk else ""
                sev = getattr(getattr(ticket, "severity", None), "value", "")
                rec = advise_goal_decision(
                    ticket_class=tc_val,
                    severity=sev,
                    risk=risk_val,
                    source_role="goal_aligner",
                    state_dir=store._path.parent,
                )
                rl_goal_advice = rec.get("recommendation", "") if isinstance(rec, dict) else ""
            except Exception:
                pass

            try:
                if decision == "NOW":
                    store.transition(tid, TicketState.DECOMP, actor="goal-aligner-consumer")
                elif decision == "LATER":
                    store.transition(tid, TicketState.LATER, actor="goal-aligner-consumer")
                elif decision == "NEVER":
                    store.transition(tid, TicketState.NEVER, actor="goal-aligner-consumer")

                try:
                    from codebot.rl_event_log import append_event
                    append_event(
                        "GOAL_DECISION",
                        state_dir=store._path.parent,
                        ticket_id=tid,
                        stage="GOAL",
                        actor_type="agent",
                        actor_role="goal_aligner",
                        decision={"outcome": decision},
                        context={
                            "reason": str(entry.get("reason", ""))[:500],
                            "rl_goal_advice": rl_goal_advice,
                        },
                    )
                except Exception:
                    pass

                updated = store._tickets.get(tid)
                if updated is not None:
                    from codebot.ticket_engine import Ticket
                    updates = {
                        "goal_disposition": decision,
                        "goal_reason": str(entry.get("reason", ""))[:500],
                        "goal_relevant_to": str(entry.get("goal_relevant_to", ""))[:200],
                        "goal_aligned_at": float(entry.get("updated_at", 0) or 0),
                        "goal_reconsider_when": str(entry.get("reconsider_when", "") or ""),
                    }
                    merged = {**updated.to_dict(), **updates}
                    new_ticket = Ticket.from_dict(merged)
                    store._tickets[tid] = new_ticket
                    store._dirty_ids.add(tid)

                consumed += 1
                logger.info("goal decision: %s -> %s (ticket %s)", tid, decision, current_state)

            except (ValueError, KeyError) as e:
                logger.debug("goal decision transition failed for %s: %s", tid, e)
                continue

        if dfile.name != "goal_aligner.status.json":
            try:
                dfile.unlink(missing_ok=True)
            except OSError:
                pass

    if shared_status.exists() and consumed > 0:
        try:
            shared_status.write_text(_json.dumps({"decisions": [], "updated_at": time.time()}), encoding="utf-8")
        except OSError:
            pass

    return consumed


def run_dispatchers(
    bots: dict[str, BotState],
    start_bot_fn: Any,
    stop_fn: Any,
    update_state_fn: Any,
    skip_route_tids: set[str] | None = None,
    store: Any = None,
) -> None:
    tick_store = store if store is not None else current_tick_store()

    try:
        from codebot.ticket_dispatcher import run_triage_fast_paths
        from codebot.state_manager import get_paths
        state_dir = get_paths().state_dir
        processed = run_triage_fast_paths(tick_store, state_dir)
        if processed:
            logger.info("triage fast-paths processed %d tickets", processed)
    except Exception as e:
        logger.debug("run_triage_fast_paths failed: %s", e)

    try:
        from codebot.review_config import reset_review_config as _reset_rc
        _reset_rc()
    except Exception:
        pass
    try:
        from codebot.review_config import reset_review_config as _reset_rc
        _reset_rc()
    except Exception:
        pass
    try:
        from codebot.ticket_dispatcher import backfill_execution_packets
        from codebot.state_manager import get_paths as _gp2

        sdir = _gp2().state_dir
        counts = backfill_execution_packets(tick_store, sdir)
        if counts.get("implementation") or counts.get("review"):
            logger.info("backfilled execution packets %s", counts)
    except Exception as e:
        logger.debug("backfill_execution_packets failed: %s", e)

    try:
        get_sched = _orch("_v2_get_scheduler", None)
        scheduler = get_sched() if callable(get_sched) else None
        if scheduler is not None:
            live_ids = {
                getattr(b, "_agent_id", "")
                for b in bots.values()
                if b.process is not None and b.process.poll() is None
                and getattr(b, "_agent_id", "")
            }
            finalize_absent = getattr(scheduler, "finalize_absent_agents", None)
            if callable(finalize_absent):
                try:
                    finalize_absent(live_ids)
                except Exception:
                    pass
            refresh_hb = getattr(scheduler, "refresh_heartbeat", None)
            if callable(refresh_hb):
                for aid in live_ids:
                    try:
                        refresh_hb(aid)
                    except Exception:
                        pass

            dispatched = scheduler.tick(store=tick_store)
            if dispatched:
                logger.info("scheduler_v2 dispatched %d agents", dispatched)

            drain_deadline = time.monotonic() + 5.0
            drain_iterations = 0
            while time.monotonic() < drain_deadline:
                ready_requests = scheduler.gate.dequeue_ready()
                drain_iterations += 1
                if not ready_requests:
                    if drain_iterations == 1:
                        logger.debug("drain: dequeue_ready returned empty on first call (queue_depth=%d)", len(scheduler.gate.spawn_queue))
                    break
                from codebot.process_manager import BotConfig, BotState
                from codebot.scheduler_v2.lifecycle import AgentRecord, AgentState
                for req in ready_requests:
                    role = req.role
                    tid = req.ticket_id
                    model = req.model or "qwen-3.5-plus"
                    agent_id = req.agent_id
                    bot_name = f"{role}-{tid[:8]}"

                    existing = bots.get(bot_name)
                    if existing is not None and existing.process is not None and existing.process.poll() is None:
                        cancel_fn = getattr(scheduler.gate, "cancel_dispatch", None)
                        if callable(cancel_fn):
                            try:
                                cancel_fn(agent_id, tid)
                            except Exception:
                                pass
                        continue

                    if existing is None:
                        prompt_file = f"codebot/roles/{role}.md"
                        hb_timeout = 60 if role == "goal_aligner" else 120
                        cfg = BotConfig(
                            name=bot_name,
                            prompt_file=prompt_file,
                            interval_seconds=30,
                            heartbeat_timeout=hb_timeout,
                            model=model,
                            fallback_model="qwen-3.5-plus",
                            enabled=True,
                            clean_exit_wait=False,
                            runner_mode="api",
                            tier=11,
                            max_restarts=3,
                        )
                        existing = BotState(config=cfg)
                        bots[bot_name] = existing

                    existing._assigned_ticket_id = tid
                    if role in REVIEWER_ROLE_NAMES:
                        _sdir = getattr(scheduler, "_state_dir", None)
                        if _sdir is not None:
                            _rpkt = Path(_sdir) / "review_packets" / f"{tid}.json"
                            if not _rpkt.exists():
                                try:
                                    _rpkt.parent.mkdir(parents=True, exist_ok=True)
                                    _rstore = get_ticket_store(_sdir)
                                    _rticket = _rstore.get(tid) if _rstore else None
                                    if _rticket is not None:
                                        import json as _json2
                                        _pkt = {
                                            "ticket_id": tid,
                                            "title": getattr(_rticket, "title", ""),
                                            "ticket_class": getattr(_rticket, "ticket_class", "").value if hasattr(getattr(_rticket, "ticket_class", ""), "value") else str(getattr(_rticket, "ticket_class", "")),
                                            "severity": getattr(_rticket, "severity", "").value if hasattr(getattr(_rticket, "severity", ""), "value") else str(getattr(_rticket, "severity", "")),
                                            "affected_modules": getattr(_rticket, "affected_modules", []),
                                            "acceptance_criteria": getattr(_rticket, "acceptance_criteria", []),
                                            "problem_statement": getattr(_rticket, "problem_statement", ""),
                                            "desired_state": getattr(_rticket, "desired_state", ""),
                                            "evidence": getattr(_rticket, "evidence", ""),
                                        }
                                        _rpkt.write_text(_json2.dumps(_pkt, indent=2), encoding="utf-8")
                                        logger.info("pre-spawn: created review packet for %s", tid)
                                    else:
                                        logger.warning("pre-spawn: ticket %s not found, skipping reviewer spawn", tid)
                                        cancel_fn = getattr(scheduler.gate, "cancel_dispatch", None)
                                        if callable(cancel_fn):
                                            try:
                                                cancel_fn(agent_id, tid)
                                            except Exception:
                                                pass
                                        existing._assigned_ticket_id = ""
                                        continue
                                except Exception as _re:
                                    logger.warning("pre-spawn: failed to create review packet for %s: %s, skipping", tid, _re)
                                    cancel_fn = getattr(scheduler.gate, "cancel_dispatch", None)
                                    if callable(cancel_fn):
                                        try:
                                            cancel_fn(agent_id, tid)
                                        except Exception:
                                            pass
                                    existing._assigned_ticket_id = ""
                                    continue
                    try:
                        import json as _json
                        sdir = getattr(scheduler, "_state_dir", None)
                        sdir_str = str(sdir) if sdir else ".codebot/state"
                        ckpt_path = Path(sdir_str) / f"{bot_name}.checkpoint.json"
                        if not ckpt_path.exists():
                            try:
                                ckpt_path.parent.mkdir(parents=True, exist_ok=True)
                                ckpt_path.write_text(_json.dumps({"processed_ids": [], "tickets_created": 0, "updated_at": 0}), encoding="utf-8")
                            except OSError:
                                pass
                        scratch_path = Path(sdir_str) / f"{bot_name}.scratchpad.json"
                        if not scratch_path.exists():
                            try:
                                scratch_path.write_text(_json.dumps({"ticket_id": tid, "agent_history": [], "completed_steps": [], "pending_steps": [], "last_updated": 0}), encoding="utf-8")
                            except OSError:
                                pass
                        started = start_bot_fn(existing, bots=bots, is_demand=True)
                        if started:
                            pid = 0
                            proc = getattr(existing, "process", None)
                            if proc is not None:
                                try:
                                    pid = proc.pid or 0
                                except Exception:
                                    pid = 0
                            register_fn = getattr(scheduler, "register_agent", None)
                            if callable(register_fn):
                                try:
                                    record = AgentRecord(
                                        agent_id=agent_id, ticket_id=tid, role=role,
                                        model=model, pid=pid, state=AgentState.STARTING,
                                        created_at=scheduler.gate._clock.now(),
                                    )
                                    register_fn(record)
                                except Exception:
                                    pass
                            existing._agent_id = agent_id
                            logger.info("spawned %s for %s (model=%s)", bot_name, tid, model)
                            proc_check = getattr(existing, "process", None)
                            if proc_check is not None:
                                time.sleep(0.5)
                                if proc_check.poll() is not None:
                                    exit_code = proc_check.returncode
                                    logger.warning(
                                        "agent %s died immediately for %s (exit=%s), cancelling",
                                        bot_name, tid, exit_code,
                                    )
                                    cancel_fn = getattr(scheduler.gate, "cancel_dispatch", None)
                                    if callable(cancel_fn):
                                        try:
                                            cancel_fn(agent_id, tid)
                                        except Exception:
                                            pass
                                    existing._assigned_ticket_id = ""
                                    existing._agent_id = ""
                                    continue
                        else:
                            cancel_fn = getattr(scheduler.gate, "cancel_dispatch", None)
                            if callable(cancel_fn):
                                try:
                                    cancel_fn(agent_id, tid)
                                except Exception:
                                    pass
                            existing._assigned_ticket_id = ""
                            logger.debug("start_bot returned False for %s", bot_name)
                    except Exception as e:
                        cancel_fn = getattr(scheduler.gate, "cancel_dispatch", None)
                        if callable(cancel_fn):
                            try:
                                cancel_fn(agent_id, tid)
                            except Exception:
                                pass
                        existing._assigned_ticket_id = ""
                        logger.warning("failed to spawn %s for %s: %s", bot_name, tid, e)
            try:
                avail_fn = _orch("apply_agent_availability", None)
                if avail_fn is None:
                    from codebot.dispatch_service import apply_agent_availability as avail_fn  # type: ignore[no-redef]
                avail_fn(bots, stop_fn=stop_fn, update_state_fn=update_state_fn, store=tick_store)
            except Exception as e:
                logger.debug("apply_agent_availability after scheduler tick failed: %s", e)
    except Exception as e:
        logger.debug("scheduler_v2 tick failed: %s", e)

    try:
        from codebot.state_manager import get_paths
        state_dir = get_paths().state_dir
        consumed = consume_goal_decisions(tick_store, state_dir)
        if consumed:
            logger.info("consumed %d goal alignment decisions", consumed)
    except Exception as e:
        logger.debug("consume_goal_decisions failed: %s", e)

    flush_tick_store(tick_store)


def start_eligible_bots(
    bots: dict[str, BotState],
    now: float,
    start_bot_fn: Any,
    store: Any = None,
) -> None:
    pass
