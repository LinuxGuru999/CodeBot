# rl_engine.py

Shared kernel for the RL-based alignment loop. Provides reward normalization, per-agent bandit state (Q-values, epsilon, history), epsilon-greedy selection, Q-value updates, and event/score helpers. Consumed by the orchestrator's alignment pipeline (reward producer) and prompt evolution triggers (RL agent). Also exposes scoring helpers to turn exit events + log observables into 0..100 scores.

## Key Exports
- `choose_pattern()`: Function
- `decay_epsilon()`: Function
- `ensure_bot()`: Function
- `heartbeat_timeout_for()`: Function
- `list_pending_events()`: Function
- `mark_event_processed()`: Function
- `record_event_reward()`: Function
- `reward_from_score()`: Function
- `score_event()`: Function
- `update_q_value()`: Function
- `write_trigger()`: Function
- `set_project_adapter()`: Function
- `get_adapter()`: Function
- `is_self_target()`: Function
- `load_rl_state()`: Function
- `save_rl_state()`: Function

## Invariants
- stdlib-only (json, pathlib, time, random, re, logging).
- All JSON writes are atomic via tmp->replace.
- Reward in [0,1]; score in [0,100]; Q in [0,1]; epsilon in [0,1].
- Never modifies the prompt_optimizer's own prompt (guard is in callers,
- but helpers expose is_self_target for defense-in-depth).
- reward_history capped at 100 per bot; rl_state.json < 50KB.
