# observability — diagnostics and metrics

## Summary
Centralizes how CodeBot exposes operational health: HTTP diagnostics endpoints on the control server, `botop` CLI diagnostics commands, and the metrics collected by `metrics_collector` / `scheduler_metrics` / `quality_metrics` / `quality_gate` / `token_budget`. This doc is the index; canonical schemas live in code.

## Why
Operators need a single place to answer “is the fleet healthy, is it making progress, and what’s it costing?” Without it, signals are scattered across `bot_metrics.json`, `token_ledger.json`, `gate_results.jsonl`, `scheduler.status.json`, and per-bot heartbeats/status files — forcing manual correlation. This module makes the mapping from metric → source → access path explicit, and explains invariants (bounded I/O, fail-open per source, atomic writes) so dashboards don’t mislead.

## Invariants
- **No aspirational endpoints**: `GET /diagnostics/*` is not a separate route. Diagnostics are served as `GET /gates/metrics` (gate performance) and `GET /scheduler/status` (budget/drain/lease/queue health). Local aggregation is `botop health|diagnostics --json`.
- **Fail-open per source** (metrics_collector, scheduler_status, botop collectors): one corrupt file skips that source, never the whole snapshot.
- **Bounded + atomic**: JSONL tails capped (gate_results 5000 lines, bot_metrics history 5000 lines, quality_metrics 10000 snapshots); writes via `tmp → replace`.
- **Read-only diagnostics**: `botop status|health|metrics|budget` and `control_server GET /gates/metrics`, `GET /scheduler/status` never mutate state.

## Dependencies
- `codebot/control_server.py` — HTTP surface: `GET /health` (public), `GET /bots`, `GET /bots/{name}`, `GET /bots/{name}/logs`, `GET /scheduler/status`, `GET /scheduler/events`, `GET /scheduler/dead-letters`, `GET /gates/metrics` (see `docs/API.md` and `.codebot/api/contract.md`).
- `codebot/botop.py` — CLI surface: `status`, `throughput`, `metrics`, `budget`, `events`, `findings`, `leases`, `gatekeeper`, `health|doctor|diagnostics|diag`, `live`, `term` (see `docs/API.md`).
- `codebot/metrics_collector.py` — per-bot snapshot (`collect_all()`, `save_snapshot()`) written to `state/bot_metrics.json` + `bot_metrics_history.jsonl`.
- `codebot/scheduler_metrics.py` — `SchedulerMetrics` (slot/queue/throughput/quality/discovery/cost/utilization/economics) and `MetricsAccumulator`.
- `codebot/quality_metrics.py` — `QualitySnapshot` / `QualityMetricsTracker` (13 quality dimensions, 24h trends).
- `codebot/quality_gate.py` — gate metrics (`compute_gate_metrics()`, `get_gate_metrics()`, `GateMetricsSummary`, `GateAlertConfig`, `check_gate_alerts()`).
- `codebot/bot_metrics.py` — append-only `bot_metrics_events.jsonl` + compacted `bot_metrics.json` (capped 500 runs/bot, 50 KB).
- `codebot/token_budget.py` — `token_ledger.json` (`day_utc`, `total_actual`, `by_model{prompt_actual, completion_actual}`), `get_budget_state()` thresholds.
- `codebot/scheduler_metrics.py` + `codebot/anomaly_alerts.py` — `evaluate()` rules A1–A6, `write_daily_digest()`.

## Module docs: `docs/modules/observability.md` vs source
This file indexes *collected metrics*; per-module schemas are authoritative in their `docs/modules/*.md`:
- metrics_collector: `docs/modules/metrics_collector.md`
- scheduler_metrics: `docs/modules/scheduler_metrics.md`
- quality_metrics: `docs/modules/quality_metrics.md`
- quality_gate: `docs/modules/quality_gate.md`
- bot_metrics: `docs/modules/bot_metrics.md`
- anomaly_alerts: `docs/modules/anomaly_alerts.md`
- control_server / botop: `docs/modules/control_server.md`, `docs/modules/botop.md`

---

## Metrics collected

### 1. Scheduler metrics (`codebot/scheduler_metrics.py` — `SchedulerMetrics`)
Point-in-time snapshot (spec §30–31). All fields are computed pure from event history; I/O isolated to load/save.

| Group | Metric | Meaning | Source |
|-------|--------|---------|--------|
| **Slots** | `slots_total=30`, `slots_active`, `slots_idle`, `slots_by_role{role:count}` | 30-slot pool utilization; idle = total − active | heartbeats + `PipelineState` |
| | `slot_utilization`, `productive_slot_utilization` | raw vs *productive* (non-duplicate) utilization | `MetricsAccumulator` |
| **Queues** | `tickets_ready`, `tickets_implementing`, `tickets_reviewing`, `tickets_verifying`, `tickets_rework`, `tickets_blocked`, `discovery_candidates`, `validated_candidates`, `duplicate_candidates` | depth per lifecycle state | `TicketStore` state counts |
| **Throughput** | `tickets_completed_per_hour`, `tickets_completed_per_day`, `mean_ticket_cycle_time_seconds` | rate + latency (created→completed for `CompletionRecord`) | `MetricsAccumulator.completions` |
| **Quality** | `first_pass_review_rate`, `rework_rate`, `human_intervention_rate`, `review_queue_age_seconds` | review health | `total_reviews/first_pass_reviews`, `total_reworks`, queue age |
| **Discovery** | `discovery_yield_rate`, `validated_candidates`, `duplicate_candidates` | signal vs noise | `total_discovery_scans` vs validated/duplicates |
| **Cost** | `cost_per_hour_usd`, `cost_per_ticket_usd` | token → USD via `pricing_table` | `total_cost_tokens` |
| **Economics** | `budget_state ∈ {ok,warn,shed_tier3,stop}`, `budget_exhausted`, `hourly/daily_spend_usd`, `hourly/daily_limit_usd`, `burn_rate`, `remaining_*_usd` | fleet-wide budget health | `token_budget` + `SchedulerMetrics.economics` |
| **Mode** | `scheduler_mode` | `BALANCED` etc. | `scheduler_config` |

*Access*: `GET /scheduler/status` (ops view), `botop throughput --json`, `botop status/throughput/health`.

### 2. Quality metrics (`codebot/quality_metrics.py` — `QualitySnapshot`)
Append-only `state/quality_metrics.jsonl` (capped 10 000 lines, 300 s interval). 13 dimensions:

| Metric | Definition |
|--------|------------|
| `total_tickets`, `complete_tickets`, `rework_tickets`, `rejected_tickets`, `deferred_tickets`, `decompose_tickets`, `planning_tickets`, `implementing_tickets`, `reviewing_tickets` | state counts from `TicketStore` |
| `independent_test_failures` | `grep FAILED|AssertionError` across `logs/*.log` |
| `escaped_defects` | COMPLETE tickets with `rework_count==0` whose title/problem contains `regression|broke|broken` |
| `complete_reopened_rate` | `reopened COMPLETE / total COMPLETE` |
| `tests_added_per_change` | `COMPLETE test-class tickets / COMPLETE feature|bug|refactor` |
| `coverage_delta_pct`, `static_findings_delta`, `complexity_delta`, `duplication_delta` | stubbed `0.0/0` (placeholder for coverage/lint runners) |
| `unnecessary_ticket_rate` | `(REJECTED+DUPLICATE+DEFERRED)/total` |
| `cost_per_accepted_change` | `Σ final_cost_tokens / COMPLETE` × `$0.000002` |
| `tokens_per_accepted_change` | `Σ final_cost_tokens / COMPLETE` |
| `changes_surviving_24h`, `total_commits_24h` | `git log --since="24 hours ago"` minus reverts |
| `cross_ticket_regression_rate` | `REWORK` with `another ticket|caused by|side effect` / `COMPLETE` |
| `rework_rate` | `REWORK / non-terminal` |
| `completion_rate` | `COMPLETE / total` |
| `median_lifecycle_minutes` | median `(updated_at − created_at)/60` for COMPLETE |
| `completions_last_hour` | `updated_at > now−3600` and `COMPLETE` |

Windowed summary: `tracker.summary(window_hours=24)` returns `throughput{completions_per_hour,total_completed,total_tickets,completions_last_hour}`, `quality{rework_rate,completion_rate,escaped_defects,…}`, `economics{cost_per_accepted,tokens_per_accepted}`, `stability{survival_rate}`, `pipeline{decompose/planning/implementing/reviewing_queue,median_lifecycle_min}`, `trends{rework_rate,completion_rate,escaped_defects,cost_per_accepted,throughput}` (`improving|degrading|stable`).

*Access*: `botop metrics --json`, programmatic `QualityMetricsTracker.maybe_record()`/`summary()`.

### 3. Gate metrics (`codebot/quality_gate.py` — `GateMetricsSummary`)
Aggregates `state/gate_results.jsonl` (bounded tail 5000 lines) written by `record_gate_results()` on every `run_quality_gates()`.

Per gate (keyed by `gate_name`):

| Field | Definition |
|-------|------------|
| `total`, `passes`, `fails`, `errors`, `skips` | counts of `GateEvaluation.result ∈ {pass,fail,error,skip}` |
| `failure_rate = fails/total`, `error_rate = errors/total` | rates |
| `avg_ms`, `p95_ms` | mean + 95th percentile of `duration_ms` |
| `last_result`, `last_timestamp` | most recent evaluation |

Alerts (`GateAlertConfig` + `check_gate_alerts()`):

| Rule | fires when | env |
|------|------------|-----|
| `failure_rate` | `failure_rate > max_failure_rate` | `CODEBOT_GATE_MAX_FAILURE_RATE` |
| `error_rate` | `error_rate > max_error_rate` | `CODEBOT_GATE_MAX_ERROR_RATE` |
| `avg_duration` | `avg_ms > max_avg_duration_ms` | `CODEBOT_GATE_MAX_AVG_MS` |

`GateAlertConfig` defaults to `min_samples=5`, `streak_window=5`; skipped if `total < min_samples`; all thresholds `None` = disabled.

Cache: `_refresh_gate_metrics_cache()` recomputes `state/gate_metrics.json` atomically after each `record_gate_results()`; `get_gate_metrics()` serves cache if `version==1` else recomputes.

*Access*: `GET /gates/metrics` (also `/api/gates/metrics`), `botop gatekeeper`, `botop health`, programmatic `get_gate_metrics(state_dir)`.

**Example**:
```bash
curl -H "Authorization: Bearer $CONTROL_TOKEN" http://localhost:8081/gates/metrics | jq .
python3 -m codebot.botop gatekeeper --json
python3 -m codebot.botop health --json
```
```python
from pathlib import Path
from codebot.quality_gate import get_gate_metrics
payload = get_gate_metrics(Path(".codebot/state"))
print(payload["metrics"][0]["failure_rate"], payload["alerts"])
```

### 4. Per-bot unified snapshot (`codebot/metrics_collector.py` — `collect_all()`)
Single read-only aggregate for RSI/RL evaluation (`state/bot_metrics.json` + `bot_metrics_history.jsonl`). Fail-open per source; tasklog tail 200 lines, scratchpad tail 100 lines; `docs/` glob + `QUEUE.md` parse each happen **once** per `collect_all()` (not per bot).

Per bot:

| Dimension | Fields |
|-----------|--------|
| `execution` | `runs`, `completed`, `errors`, `restarts`, `completion_rate`, `error_rate`, `max_iterations`, `last_reason` (from `*.checkpoint.json` + `alignment_events/*.exit.json`) |
| `tokens` | `prompt_tokens`, `completion_tokens`, `total_tokens`, `model`, `api_calls`, `source ∈ {stream_usage,char_estimate,none}` (from `logs/{bot}.stream.json` usage block + fleet fallback from `token_ledger.json`) |
| `alignment` | `avg_reward`, `last_reward`, `last_score`, `total_runs`, `epsilon`, `q_arms`, `trigger_active`, `alignment_score` (from `rl_state.json`, `alignment_scores.json`, `alignment_triggers/*.evolve.json`) |
| `progress` | `tasklog_lines`, `tasklog_last`, `tasklog_age_s`, `scratchpad_lines`, `checkpoint_age_s`, `output_files` |
| `quality` | `fp_rate`, `coverage_pct`, `meas_error_rate`, `findings`, `fp_source ∈ {measurements,rl_failures,no_runs,fallback}` |
| `liveness` | `heartbeat_age_s`, `log_age_s`, `status_age_s`, `current_task`, `iteration` |
| `throughput` | `claimed`, `completed`, `completion_rate`, `abandoned`, `checkpoint_age_s` |
| `output_quality` | `build_gate_pass`, `finding_precision`, `duplicate_rate` |
| `economics` | `tokens_per_completion`, `est_cost_usd` (via per-model `$ / 1K`), `model`, `noop_flag` |
| `autonomy` | `human_interventions`, `stuck_restarts`, `sandbox_denied`, `failure_streak`, `auto_disabled (streak≥3)` |
| `learning` | `epsilon`, `q_arms`, `q_spread`, `needs_exploration_reset (ε<0.05)` |
| `reward_trend` | `improving | regressing | flat` (`last_reward` vs `avg_reward ±0.05`) |

Fleet summary: `bots_tracked`, `active (hb<300s)`, `idle (<3600s)`, `stale`, `erroring (error_rate>0.5 ∧ runs≥2)`, `improving`, `regressing`; `fleet_tokens{fleet_prompt_actual,fleet_completion_actual}`, `ledger_day`, `ledger_total_actual`, `scrutiny{flagged_count,human_approved_count,mode}`.

*Access*: `botop metrics`, `botop health`, `python3 -m codebot.metrics_collector --incremental`.

### 5. Bot lifecycle metrics (`codebot/bot_metrics.py`)
Append-only `state/bot_metrics_events.jsonl` (O(1) append) + compacted `state/bot_metrics.json` (tmp→replace, pruned when >50 KB or >500 runs/bot, 7-day window).

Per bot entry: `runs: list[float]` (timestamps within window), `successes`, `failures`, `total_tokens`, `total_duration_s`. API: `record_bot_metric(name,alive,exit_code,tokens_this_run,started_at)`, `get_bot_metrics(name)`, `get_all_metrics()`, `get_success_rate(name)`, `get_token_burn_rate(name)`.

*Access*: `botop metrics`, `bot_metrics.py` API.

### 6. Token budget (`codebot/token_budget.py` + `state/token_ledger.json`)
Fleet-wide UTC-day accounting:

```json
{"day_utc":"2026-09-20","total_actual":123456,"by_model":{"qwen-3.5-plus":{"prompt_actual":1000,"completion_actual":500}}}
```

`get_budget_state(total)` → budget tier; `scheduler_status()` redacts to `budget_state`, `budget_total_actual`, `per_model_actual` (bounded, truncated, no secrets).

*Access*: `GET /scheduler/status`, `botop budget --json`, `botop throughput`, `botop health`.

### 7. Anomaly rules (`codebot/anomaly_alerts.py`)
Evaluates `bot_metrics.json` snapshot + history (10-line tail) + `QUEUE.md` / `token_ledger.json`:

| Rule | Severity | Fires when |
|------|----------|------------|
| A1 | `page` | `erroring >0` for 3 consecutive snapshots |
| A2 | `page` | queue depth strictly growing for 3 snapshots |
| A3 | `page` | today tokens > 2× trailing 7-day daily avg |
| A4 | `page` | T4+ approval backlog older than 48 h |
| A5 | `digest` | `stale >0` (heartbeat >1 h) |
| A6 | `digest` | `regressing ≥3` |

Writes `state/anomaly_alerts.json` (capped 50) and `state/daily_digest.md` once per UTC day.

*Access*: `botop health` (Signals/Doctor sections), `anomaly_alerts.evaluate()`, `write_daily_digest()`.

---

## Diagnostics API (control server)

All diagnostics are **read-only** and **versioned** (`version: 1`). Auth: `Authorization: Bearer $CONTROL_TOKEN` except `GET /health`.

| Method | Path | Purpose | Auth |
|--------|------|---------|------|
| `GET` | `/health` (`/api/health`) | liveness | no |
| `GET` | `/bots` (`/api/bots`) | all agents (`bot_status()` × `BOT_REGISTRY`) | yes |
| `GET` | `/bots/{name}` (`/api/bots/{name}`) | single agent + ETag/`If-None-Match`/`If-Modified-Since` → `304` | yes |
| `GET` | `/bots/{name}/logs?lines=200` | tail `logs/{name}.log` (bounded 1–2000) | yes |
| `GET` | `/scheduler/status` | `scheduler_status()` — budget/drain/dead-letters/queue/leases/paused/disabled/starvation/batch caps/per-model | yes |
| `GET` | `/scheduler/events?limit=N&type=TYPE` | tail `state/events.jsonl` sanitized, bounded to `MAX_EVENTS`, `type` ≤64 chars | yes |
| `GET` | `/scheduler/dead-letters` | bounded `{id,reason}` list ≤20 + `count` | yes |
| `GET` | `/gates/metrics` (`/api/gates/metrics`) | `get_gate_metrics()` payload (see §3) | yes |
| `POST` | `/scheduler/dead-letters/{id}/retry` | idempotent dead-letter retry + `dead-letter-retry` event | yes |

Rate limiting: token-bucket on auth (5 attempts / 60 s → 300 s cooldown); `GET /health` also rate-limited; excess → `429 {error:"too many requests", reason}` + `Retry-After: 300` (+ `retry_in_seconds` in JSON where applicable).

Security headers on every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`. Bot-name in paths validated to `^[a-zA-Z0-9_-]+$`; log tails reject `..`/`/`; pkill patterns `shlex.quote()`d.

*No* `POST /diagnostics/*` or `GET /diagnostics/*` route is registered. Use `GET /gates/metrics` for gate observability and `GET /scheduler/status` for scheduler observability. Local aggregation that needs no token: `botop health|diagnostics|diag --json` (reads state files directly).

Full contract: `.codebot/api/contract.md`; full endpoint docs: `docs/API.md#control-server-http-api`.

---

## CLI diagnostics (`codebot/botop.py`)

All commands accept `--project <path>` and `--no-color`; many accept `--json`.

| Command | What it shows | Key flags |
|---------|---------------|-----------|
| `botop status [--verbose] [--json] [--no-wrap]` | orchestrator PID, last spawn age, drain, per-agent `RUNNING/STALE/PAUSED/DEAD/UNKNOWN` + `HB(LOG) ITER TASK PID ERR MODEL` | `--verbose` full table, `--json` machine-readable |
| `botop throughput [--json]` | ticket flow (`total/READY/IMPLEMENTING/…` + 24h/7d throughput, `avg_age_h`, `rework_rate`), claims staleness, budget, RL `avg_reward`, leases | `--json` |
| `botop metrics [--json]` | per-agent `runs/succ/fail/avgR` + heartbeat/task, `bot_metrics.json` summary, `token_ledger.json` per-model | `--json` |
| `botop budget [--json]` | `total_actual`, `day_utc`, `budget_state`, per-model prompt/completion | `--json` |
| `botop events [--limit N] [--type TYPE] [--json]` | tail `events.jsonl` | filters by `type` |
| `botop findings [--limit N] [--json]` | tail `findings.jsonl` with severity coloring | |
| `botop leases [--json]` | active leases + dead letters + attempts | |
| `botop deadletters [--json]` | alias for dead-letter list | |
| `botop gatekeeper [--json]` | last gate decisions (`ticket_id → COMPLETE|REWORK`, rework count, failed gates) | |
| `botop health` / `doctor` / `diagnostics` / `diag [--json]` | **comprehensive diagnostics** — orchestrator + agents (incl. stale/erroring) + tickets + claims/leases + budget/RL/gatekeeper + events/findings/anomalies + logs health + Doctor recommendations | `--json` |
| `botop live [--interval S] [--once] [--json] [--view {both,agents,tickets}] [--limit N] [--offset N] [--no-clear]` | auto-refresh dashboard (`botop term` for interactive; `1` agents, `2` tickets, `3` both, `q` quit, `--once` for piped/static, `--no-clear` for screen readers) | aliases: `watch`, `top`, `dash`, `dashboard` |
| `botop claims [--json]` | active `state/claims/*.json` with ages, `stale>2h` warning | |
| `botop tickets [--json] [--limit N] [--state STATE]` | ticket pipeline `DISCOVERED…COMPLETE` + side states, colored by severity/state, READY sorted by severity | |
| `botop logs <agent> [--lines N] [--follow]` | `tail -F` of `logs/{agent}.log` (fallback `.tasklog/.mission`) | |

Interactive `botop term` (`terminal|shell|repl|interactive`) REPL supports `status`, `tickets`, `claims`, `throughput`, `metrics`, `budget`, `events`, `findings`, `leases`, `deadletters`, `gatekeeper`, `health|doctor|diagnostics|diag`, `logs <agent>`, `restart|pause|resume <agent>`, `drain|clear-drain`, `live`, `clear`, `! <shell>` (via `shlex.split`, no shell), history at `~/.botop_history`.

**Examples**:
```bash
python3 -m codebot.botop status --verbose
python3 -m codebot.botop health --json | jq .
python3 -m codebot.botop live --once --no-color
python3 -m codebot.botop throughput --json
python3 -m codebot.botop gatekeeper
python3 -m codebot.botop health
# control-server equivalents (require CONTROL_TOKEN):
python3 -m codebot.control_client health
python3 -m codebot.control_client scheduler-status
curl -H "Authorization: Bearer $CONTROL_TOKEN" http://localhost:8081/gates/metrics
```

---

## Access patterns

- **Ops dashboard (no token)**: `botop health` or `botop live --once --json` → single snapshot from state files; `--no-clear` preserves scrollback/screen-reader buffer.
- **Remote ops (token)**: `control_client` or `curl /scheduler/status`, `/gates/metrics`, `/bots`, `/bots/{name}`.
- **Trends**: `quality_metrics` JSONL (daily), `metrics_collector` history JSONL (5000 lines), `gate_results.jsonl` (5000 lines) → compute deltas with `quality_gate.compute_gate_metrics()` or `QualityMetricsTracker._compute_trends()`.
- **Cost attribution**: `codebot/cost_tracker.py` maps token ledger → per-ticket phase costs; see `docs/modules/cost_tracker.md`.

---

## Exports
- `codebot/metrics_collector.py:collect_all(only_fresh=False) → dict`, `save_snapshot(snapshot)`, `main()`
- `codebot/scheduler_metrics.py:SchedulerMetrics`, `CompletionRecord`, `MetricsAccumulator`, `summary()`
- `codebot/quality_metrics.py:QualitySnapshot`, `QualityMetricsTracker.(maybe_record,summary,_compute_trends)`
- `codebot/quality_gate.py:get_gate_metrics(state_dir,config)`, `compute_gate_metrics(records)`, `check_gate_alerts(metrics,config)`, `GateMetricsSummary`, `GateAlertConfig`
- `codebot/bot_metrics.py:record_bot_metric`, `get_bot_metrics`, `get_all_metrics`, `get_success_rate`, `get_token_burn_rate`, `set_state_dir`
- `codebot/botop.py:cmd_health`, `cmd_live`, `cmd_status`, `cmd_throughput`, `cmd_metrics`, `cmd_budget`, `cmd_gatekeeper` + aliases `health|doctor|diagnostics|diag`
- `codebot/control_server.py:scheduler_status()`, `bot_status(name)`, `ControlHandler.do_GET { /health, /bots, /scheduler/status, /scheduler/events, /scheduler/dead-letters, /gates/metrics }`

---

## Links
- API: `docs/API.md#control-server-http-api` — Bot Operations CLI (`botop`)
- Contract: `.codebot/api/contract.md` — Control Server Endpoints (Stable)
- Modules: `bot_metrics.md`, `metrics_collector.md`, `scheduler_metrics.md`, `quality_metrics.md`, `quality_gate.md`, `anomaly_alerts.md`, `control_server.md`, `botop.md`
- State dir: `.codebot/state/` — `bot_metrics.json`, `token_ledger.json`, `gate_results.jsonl`, `gate_metrics.json`, `quality_metrics.jsonl`, `anomaly_alerts.json`, `daily_digest.md`
