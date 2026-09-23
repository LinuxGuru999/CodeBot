# Workforce Allocation

CodeBot assigns engineering-worker capacity from one global `max_slots` budget.
Supervisor and control processes are outside this budget. A worker is counted
while it is running or reserved for startup; demand work receives no extra
concurrency allowance.

## Control Loop

Each health tick builds a `PipelineState`, then `WorkforceAllocator` produces a
`WorkforceTarget`. The target reserves downstream capacity before admitting new
implementation work:

1. Rework uses capacity first.
2. Verification and review receive their configured floors when existing or
   predicted upstream work can reach them.
3. Their queue depth and observed service rate raise capacity when expected
   drain time exceeds `target_drain_seconds`.
4. Decomposition and planning receive remaining capacity for their own queues.
5. Implementation is constrained by remaining capacity, its configured share,
   and available review plus verification capacity.
6. Discovery uses otherwise idle capacity only when the engineering pipeline
   is empty.

The current dispatcher applies dynamic implementation, review, planning, and
decomposer limits. Existing atomic claim creation remains immediately before
process startup, and startup failures release that claim. `WorkforceReservations`
provides the matching reservation abstraction for callers that reserve starts
before invoking a launcher.

Clean implementation, review, and verification exits append a bounded worker
duration sample to `.codebot/state/workforce-flow.json`. The next allocation
tick reloads those samples and uses their per-worker service rates to estimate
how many downstream workers are needed to meet the drain-time target. Missing
or corrupt history starts empty and falls back to queue-depth allocation.

Every allocation decision also overwrites
`.codebot/state/workforce-status.json` atomically. It records the decision
timestamp, active and maximum worker capacity, actionable pipeline queue
counts, the observed flow rates, and the complete stage-by-stage target. This
is an inspectable operational snapshot rather than a history; use
`workforce-flow.json` for the bounded duration sample history.

## Configuration

The `scheduler.workforce` section accepts these values:

```yaml
scheduler:
  max_slots: 30
  workforce:
    implementation_maximum_fraction: 0.60
    review_floor: 1
    verification_floor: 1
    decomposition_floor: 1
    planning_floor: 0
    discovery_maximum_fraction: 0.25
    target_drain_seconds: 900
```

Floors may not exceed `max_slots`. Lower `target_drain_seconds` to prioritize
latency at higher cost; raise it to consolidate work into fewer workers.

## Operations

Use scheduler decisions and pipeline counts to identify a bottleneck. A growing
review or verification queue should increase downstream capacity and reduce new
implementation admissions. A healthy scale-down is graceful: workers with
active assignments are marked `draining` and finish rather than being killed.

Inspect `.codebot/state/workforce-status.json` to compare queue pressure with
the latest allocation target. The `capacity` section shows live active workers
against `max_slots`; `pipeline`, `flow`, and `target` explain the inputs and
stage allocations for that tick. `python -m codebot.botop status` prints a
compact capacity and target summary, while `python -m codebot.botop status
--json` exposes the complete snapshot under `workforce`.

If a launch fails, release its claim and reservation before the next tick. If a
worker exits or stalls, the existing lifecycle handlers return its ticket to an
actionable state and the next target may consume the freed slot.
