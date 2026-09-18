# metrics_collector.py

Aggregates every available signal source into one per-bot snapshot so the RSI (self-improvement) and RL (bandit prompt optimization) strategies can be evaluated from a single file. Supplements (never replaces) the existing rl_state.json, measurements.json, and token_ledger.json pipelines.  Metric dimensions (all bots, all sources): execution  — runs, completions, errors, timeouts, restarts, avg iterations tokens     — prompt/completion actuals per model, cost proxy alignment  — RL reward, avg reward, score, epsilon, Q-values, triggers progress   — tasklog lines, scratchpad lines, checkpoint recency, findings quality    — FP rate, coverage %, error rate from measurements.json liveness   — heartbeat age, log age, process alive, next run ETA

## Key Exports
- `collect_all()`: Function
- `save_snapshot()`: Function
- `main()`: Function

## Invariants
- stdlib-only; read-only (never mutates state files).
- Fail-open per source: one corrupt file skips that source, not the bot.
- Output: state/bot_metrics.json (atomic tmp->replace) + bot_metrics_history.jsonl.
- Bounded: tasklog tail 200 lines scanned, scratchpad tail 100 lines.
