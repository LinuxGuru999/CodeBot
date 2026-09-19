# Role Prompt Standards

Last updated: 2026-09-18

Mandatory writing standards for all role prompts in `codebot/roles/*.md`. This document complements `docs/ROLES.md` (reference catalog) with authoring requirements. Non-compliant prompts cause silent production failures — agents that appear to run but produce zero output, mark themselves completed prematurely, or crash on malformed tool calls.

Every standard below was derived from debugging actual agent failures in this codebase.

---

## 1. Required Document Structure

Every role prompt MUST contain these sections in order:

| Section | Purpose |
|---------|--------|
| `# Role: {Name}` | Header with canonical role name matching `role_registry.py` |
| `## Persona` | One-paragraph behavioral identity |
| `## CRITICAL: First Action After Startup` | Exact first tool call after boilerplate checks |
| `## Identity` | Category, nickname, incentive, personality block |
| `## Mission` | Single-sentence purpose + hard minimum output requirement |
| `## Process` | Numbered steps from input → action → output |
| `## Tool Constraints` | Allowed tools, commands, filesystem scope, network, git |
| `## Anti-Patterns` | Numbered list of specific forbidden behaviors |
| `## Noop Rules` | Explicit definition of what counts and doesn't count as noop |
| `## Session Management` | Timeout, heartbeat path, checkpoint path, restart behavior |
| `## Safety Rules` | Invariants the agent must never violate |

Optional sections: Output Format, Complexity Tiers, Decomposition Rules, Strategic Priorities. Never omit required sections.

Reference implementation: `codebot/roles/feature_hunter.md`.

---

## 1.5 Prompt Size Constraints

Prompt length MUST scale with the model's context window. A prompt that fills too much of a small context window causes mid-session eviction — the model forgets earlier instructions as new tool results push them out.

| Context Size | Max Prompt Length | Guidance |
|-------------|------------------|----------|
| SMALL (cheap models) | ≤ 3,000 words | Minimal prose, rely on tables and templates |
| LARGE (standard/expensive) | ≤ 6,000 words | Can include more explanation but still prefer structure over prose |

Measure prompt size by word count, not line count. Tables are more token-efficient than paragraphs for cheap models.

---

## 2. Startup Sequence Requirements

Cheap models drift without explicit ordering. The startup sequence MUST:

### 2.1 Skip Boilerplate Checks

Explicitly tell the agent which files NOT to read. Discovery agents waste 6+ API calls per session reading nonexistent infrastructure files. The prompt MUST include:

```
SKIP all boilerplate checks. Do NOT read .drain, .update_lock,
alignment_scores.json, alignment_triggers/, false_positives.md,
project.yaml, constitution.md, or ROADMAP.md.
```

Evidence: `bug_hunter.log` shows consecutive failed reads for `.drain_bug_hunter`, `.update_lock`, `alignment_scores.json`, `alignment_triggers/bug_hunter.evolve.json`, `false_positives.md` before doing any real work. Each failed read costs an API round-trip.

### 2.2 Specify Exact First Action

The first real action MUST be stated as an exact tool call with absolute path:

```
Your VERY FIRST action must be:
read path=/home/kozuka/Work/CodeBot/.codebot/roadmap_index.json
```

Never say "read the roadmap index" — cheap models will search for it, glob for it, or read the wrong file.

### 2.3 Use Portable Path Variables for Critical Files

Hardcoded absolute paths (e.g., `/home/kozuka/Work/CodeBot/.codebot/...`) make prompts non-portable across projects, contradicting CodeBot's core value proposition (ROADMAP.md §18). Prompts MUST use path variables that resolve at runtime via the `ProjectAdapter`.

Define a variable convention in the prompt header:
```
PROJECT_ROOT = /home/kozuka/Work/CodeBot  # Resolved by adapter at startup
STATE_DIR = {PROJECT_ROOT}/.codebot/state
```

Then reference variables throughout:
```
read path={STATE_DIR}/feature_hunter.checkpoint.json
```

The orchestrator injects the resolved project root into the prompt assembly via `role_prompt.py`. When writing prompts, use `{PROJECT_ROOT}` as the placeholder — never hardcode user-specific or machine-specific paths.

Exception: source code scanning where the agent intentionally searches relative to project root using `grep`/`glob` with patterns rather than absolute file paths.

---

## 3. Tool Call Format Standards

This is the most failure-prone area. Get it wrong and agents silently produce empty results.

### 3.1 Arguments Must Be Valid JSON

`api_runner.py:1743-1752` parses tool arguments via `json.loads()`:

```python
args_raw = func.get("arguments", "{}")
if isinstance(args_raw, dict):
    args = args_raw
else:
    try:
        args = json.loads(args_raw) if args_raw else {}
    except Exception:
        args = {}
```

If the model outputs YAML-style formatting, `json.loads()` throws, `args` becomes `{}`, and the tool executes with all default parameters. The agent sees a success response with garbage output and continues.

**WRONG:**
```
Tool: create_ticket
Arguments:
  title: "My ticket"
  severity: "high"
  source: "bug_hunter"
```

**RIGHT:**
```
Tool: create_ticket
Arguments: {"title": "My ticket", "severity": "high", "source": "bug_hunter"}
```

The prompt MUST show at least one complete JSON-formatted example and MUST include an anti-pattern entry forbidding YAML format.

### 3.2 Parameter Names Must Match Exactly

`api_runner.py:818` unpacks arguments as `func(**args)`. Keys must exactly match function parameter names. For `create_ticket` (`api_runner.py:538-549`), the valid keys are:

`title`, `ticket_class`, `severity`, `source`, `evidence`, `problem_statement`, `desired_state`, `acceptance_criteria`, `affected_modules`, `risk`

Any extra key goes into `**kwargs` and is silently ignored. Any misspelled key causes `TypeError` at line 819-820, returning `{"success": False, "error": "bad args"}`.

### 3.3 String Parsing Conventions

Several fields have non-obvious parsing rules in `_create_ticket_tool`:

| Field | Parsing Rule | Code Location |
|-------|-------------|---------------|
| `acceptance_criteria` | Split on `;` → list of strings | `api_runner.py:569` |
| `affected_modules` | Split on `,` → list of strings | `api_runner.py:570` |
| `ticket_class` | `.lower()` mapped against `TicketClass` enum values | `api_runner.py:566` |
| `severity` | `.lower()` mapped against `Severity` enum values | `api_runner.py:567` |
| `risk` | `.lower()` mapped against `RiskLevel` enum values | `api_runner.py:568` |
| `title` | Truncated at 200 characters silently | `api_runner.py:553-554` |

### 3.4 Empty Field Fallback Behavior

Empty strings trigger fallback defaults that degrade ticket quality:

| Field | Fallback When Empty | Problem |
|-------|-------------------|---------|
| `title` | Falls back to `problem_statement` or `evidence` or `"Finding from {source}"` | Generic, unsearchable titles |
| `evidence` | Falls back to `title` string | Loses structured context |
| `acceptance_criteria` | Falls back to `[title]` | Single useless criterion |
| `problem_statement` | Falls back to `title` | No problem description |
| `desired_state` | Falls back to `f"Resolve: {title}"` | Generic |

The prompt MUST explicitly forbid leaving `evidence` and `acceptance_criteria` empty, documenting the specific bad fallback behavior.

---

## 4. Path Resolution Rules

All state files live under `.codebot/state/` at the repository root. Historical bugs placed files in three different directories simultaneously (`state/`, `.codebot/state/`, `codebot/.codebot/state/`), causing agents to read stale data or fail silently.

| Resource | Correct Path Template | Wrong Paths |
|----------|----------------------|-------------|
| Tickets | `{STATE_DIR}/tickets.json` | `state/tickets.json`, `codebot/.codebot/state/tickets.json` |
| Heartbeat | `{STATE_DIR}/{name}.heartbeat` | `state/{name}.heartbeat` |
| Checkpoint | `{STATE_DIR}/{name}.checkpoint.json` | `state/{name}.checkpoint.json` |
| Claims | `{STATE_DIR}/claims/` | `state/claims/` |
| Scratchpad | `{STATE_DIR}/{name}.scratchpad.json` | `state/{name}.scratchpad.json` |

Where `{STATE_DIR}` = `{PROJECT_ROOT}/.codebot/state`.

Resolution chain (`api_runner.py:590-597`):
1. Default: `Path.cwd() / ".codebot" / "state" / "tickets.json"`
2. Adapter override: `_adapter_instance.paths().state_dir / "tickets.json"`
3. Relative path resolution: `(WORK_ROOT / store_path).resolve()`

When `CodeBotAdapter` is loaded, `state_dir` resolves to `{repo_root}/.codebot/state`. Without adapter, CWD determines the base. Prompts MUST use the `{STATE_DIR}` variable convention (§2.3) to remain portable while resolving correctly.

---

## 5. Deduplication Protocol

Every agent that creates tickets MUST check for existing tickets before creating. Failure to do so wastes tokens on duplicate creation attempts that get swallowed by the dedup engine.

**Important correction**: `TicketStore.add()` raises `ValueError` on duplicate evidence hash, but `api_runner.py:611` catches it and returns `{"success": True, "output": "Duplicate: ..."}`. The `tickets_created` counter at `api_runner.py:1759` DOES increment because `success` is True. The real cost is not counter stall — it is wasted tokens. Every duplicate attempt consumes a full API round-trip (model generates tool call → api_runner executes → result returned → model reads response) for zero pipeline progress. On cheap models with tight token budgets, this can exhaust the session before any unique tickets are created.

### 5.1 How to Dedup

Use `grep` tool against tickets.json. Search for BOTH structured identifiers AND natural-language keywords:

```
grep pattern="{deliverable_id}" path="/absolute/path/to/tickets.json"
grep pattern="{key words from title}" path="/absolute/path/to/tickets.json"
```

Do NOT use Python code or TicketStore directly — agents don't have access to internal APIs.

### 5.2 Dedup Hits Are Not Noops

The prompt MUST explicitly state: "Checking dedup via grep and finding a match is legitimate work, NOT a noop. Add the ID to processed_ids and move on." Without this, agents count dedup skips toward their noop cap and die early.

---

## 6. Minimum Output Requirements

Discovery and planning agents MUST have a hard minimum ticket creation count per run. Recommended: ≥ 5.

State the minimum in at least THREE places:
1. Mission section: "You MUST successfully call `create_ticket` at least 5 times before exiting."
2. Process exit conditions: "DO NOT EXIT BEFORE 5 SUCCESSFUL TICKETS."
3. Anti-patterns: "NEVER exit after 1-2 tickets claiming 'done' — minimum is 5."

Repetition is necessary because cheap models forget constraints mid-session.

Clarify that only successful creations count. `api_runner.py:1759-1760` increments `tickets_created` only when `result.get("success")` is truthy. Failed calls (bad args, TypeError, store errors) return `success: False` and don't count.

Define legitimate completion: "If genuinely all candidates are deduped, write checkpoint with `reason: all_deduped` and exit cleanly." Without this, agents either loop forever or falsely claim completion.

---

## 7. Noop Definition and Limits

A noop is a run iteration where the agent neither produces output nor performs legitimate investigative work. The prompt MUST define both categories explicitly.

### What Counts as Noop
- Reading files unrelated to the task
- Writing text output without calling a tool
- Re-reading the same file twice
- Reading nonexistent boilerplate files (.drain, alignment_*, etc.)

### What Does NOT Count as Noop
- Checking dedup via grep and finding a match
- Reading checkpoint/index/tickets.json as part of the defined process
- Writing heartbeat or checkpoint files
- Grep searches that return zero results (legitimate negative finding)

### Noop Cap

Recommend: 20 consecutive noops before exit. Historical default was 10, which proved too aggressive — agents doing legitimate broad scans hit the cap before producing output.

---

## 7.5 Checkpoint Format Standard

Checkpoints persist agent progress across restarts. A malformed checkpoint can permanently kill an agent. The feature_decomposer wrote `{"reason": "completed"}` as its checkpoint, which caused it to read "completed" on every subsequent restart and immediately exit without doing work.

Every checkpoint JSON MUST contain these fields:

```json
{
  "processed_ids": ["2.A", "2.D"],
  "tickets_created": 5,
  "last_batch": "T0",
  "updated_at": 1789795066.0
}
```

| Field | Type | Purpose |
|-------|------|--------|
| `processed_ids` | array of strings | IDs already handled; skip on restart |
| `tickets_created` | integer | Running count for minimum-output enforcement |
| `last_batch` | string | Resume point (tier name, section ID, etc.) |
| `updated_at` | float | Unix timestamp for staleness detection |

The checkpoint MUST NOT contain a `"reason": "completed"` field. Completion is determined by the process logic (all candidates exhausted), not by a checkpoint flag. If an agent writes `"completed"` to its checkpoint, the orchestrator has no mechanism to clear it — the agent is permanently dead until manual intervention.

The prompt MUST instruct the agent to write checkpoints in this exact format and MUST forbid writing terminal status flags.

---

## 7.6 Heartbeat Format Standard

Heartbeats MUST be bare Unix timestamps only. No JSON wrapping, no extra fields, no human-readable dates.

**WRONG:** `{"timestamp": 1789795066.0, "bot": "feature_hunter"}`
**RIGHT:** `1789795066.6893487`

Evidence: bug_hunter received an evolution patch (`tighten_heartbeat_format`) because it wrote JSON heartbeats. The orchestrator's `read_heartbeat()` function (`orchestrator.py:868-891`) parses the file as a bare float. JSON-formatted heartbeats cause `float(txt)` to throw ValueError, falling through to ISO parsing, which also fails, returning 0.0 — making the agent appear permanently stuck.

The prompt MUST specify: "Write heartbeats as bare Unix timestamps only. Format: write the string `str(time.time())` directly to the heartbeat file."

Note: `api_runner.py:816-817` intercepts `.heartbeat` file writes server-side and injects the real timestamp, so the model's actual content is discarded. But the model still needs to call `write` with the correct path to trigger the interception.

---

## 7.7 Error Recovery Behavior

Tool calls can fail. The prompt MUST define what the agent should do when `api_runner.py:820-822` returns `{"success": False, "error": "..."}`.

| Error Type | Cause | Agent Should |
|-----------|-------|-------------|
| `"unknown tool: X"` | Tool name not in `_TOOL_MAP` | Stop using that tool name. Check allowed tools list. |
| `"bad args for X: ..."` | TypeError from `func(**args)` — wrong parameter names or types | Fix argument format. Re-read the tool's expected parameters. Do NOT retry with same args. |
| `"store failed: ..."` | OSError during TicketStore write | Retry once after brief pause. If second failure, write checkpoint and exit cleanly. |
| `"command denied"` | bash command not in allowlist | Stop trying that command. Use allowed alternatives (grep tool instead of bash grep). |
| File not found | Read/grep on nonexistent path | Skip the file. Do NOT retry. Do NOT count as noop if it was a speculative check. |

Critical rule: **NEVER retry a failed tool call with identical arguments.** The failure is deterministic — same input produces same output. Retrying wastes tokens. Fix the input or move on.

The prompt MUST include at least one anti-pattern entry about retry loops.

---

## 8. Field Value Constraints

All enum-typed fields MUST use lowercase string values matching the enum definitions in `ticket_engine.py`.

### Valid Values

| Field | Valid Values | Source |
|-------|-------------|--------|
| `ticket_class` | bug, feature, security, performance, documentation, test, refactor, dependency, architecture, infrastructure | `TicketClass` enum |
| `severity` | critical, high, medium, low | `Severity` enum |
| `risk` | critical, high, medium, low | `RiskLevel` enum |
| `source` | Exact role name (e.g., "feature_hunter", "bug_hunter", "security_auditor") | Free string, but MUST be role-specific |

### Source Field

The `source` field MUST be the exact role name. Never generic values like "agent", "roadmap", or "automated". This field is used for auditing which roles produce tickets and for routing to correct implementers via `TICKET_CLASS_TO_IMPLEMENTER` (`orchestrator.py:199-209`).

---

## 9. Anti-Pattern Catalog

Every role prompt MUST include an Anti-Patterns section covering known failure modes. Include at minimum the patterns relevant to the role's category:

| # | Anti-Pattern | Root Cause | Affected Roles |
|---|-------------|-----------|----------------|
| 1 | Reading .drain, .update_lock, alignment_scores.json on startup | Boilerplate copy-paste from other prompts; files don't exist | All discovery |
| 2 | YAML-format tool arguments instead of JSON | Model doesn't know api_runner uses json.loads() | All roles |
| 3 | Using relative paths that break under different CWD | CWD varies between orchestrator and subprocess | All roles |
| 4 | Writing text analysis instead of calling create_ticket | Model treats task as essay rather than tool-use | Discovery, planning |
| 5 | Exiting early after 1-2 tickets claiming "done" | No minimum output requirement stated | Discovery, planning |
| 6 | Using generic source values ("agent", "roadmap") | Prompt doesn't enforce role-specific source | Discovery |
| 7 | Leaving acceptance_criteria or evidence empty | Unknown fallback behavior in api_runner | Discovery |
| 8 | Reading full large files (ROADMAP.md at 3500 lines) | No index-driven fast path specified | Planning |
| 9 | Counting dedup skips as noops | Noop definition doesn't exclude legitimate dedup | Discovery |
| 10 | Wrong state directory (state/ vs .codebot/state/) | Historical path mismatch, pre-adapter defaults | All roles |
| 11 | Using `bash` to read state files instead of `read`/`grep` tools | Model prefers shell commands; bash allowlist denies them | All roles |
| 12 | Writing `"reason": "completed"` to checkpoint | Agent marks itself done permanently; orchestrator has no reset mechanism | Discovery, planning |
| 13 | JSON-wrapped heartbeats instead of bare timestamps | Orchestrator `read_heartbeat()` fails to parse, returns 0.0, agent appears stuck | All roles |
| 14 | Retrying failed tool calls with identical arguments | Failures are deterministic; retries waste tokens | All roles |
| 15 | Reading entire tickets.json (300KB+) instead of grepping for specific patterns | Model tries to load full file into context; exceeds token budget | Discovery, planning |
| 16 | Leaving `affected_modules` empty when index has `[]` | Tool receives empty string, produces ticket with no module routing | Discovery |

---

## 12. Model Tier Considerations

Prompt verbosity MUST scale inversely with model capability. A prompt that works for qwen-3.8-max-thinking will fail silently on xiaomi-mimo-2.5.

| Tier | Models | Prompt Requirements |
|------|--------|-------------------|
| Cheap | xiaomi-mimo-2.5 | Exact JSON examples, repeated constraints, explicit skip lists, absolute paths everywhere, anti-patterns enumerated by number |
| Standard | qwen-3.5-plus, qwen-3.7-plus | Moderate abstraction OK, still need JSON format examples and field constraints |
| Expensive | qwen-3.8-max, qwen-3.8-max-thinking | Can reason about decomposition and architecture, still need exact tool format specifications |

Rule: if a role uses a cheap model profile (`CostClass.CHEAP` in `role_registry.py`), its prompt MUST include at least one complete JSON tool call example and MUST state every constraint in at least two different sections.

---

## 13. Testing and Validation Checklist

Before shipping any new or modified role prompt, verify:

- [ ] Tool argument format matches `json.loads()` expectations (valid JSON objects, not YAML)
- [ ] All state/checkpoint/heartbeat paths use `.codebot/state/` prefix (absolute preferred)
- [ ] `source` field matches role name exactly as registered in `role_registry.py`
- [ ] Noop definition explicitly excludes legitimate dedup checks
- [ ] Minimum output requirement stated in ≥ 3 locations (mission, process, anti-patterns)
- [ ] Anti-patterns section covers applicable failure modes from §9
- [ ] Empty field fallback behavior documented for `evidence` and `acceptance_criteria`
- [ ] Role registered in `role_registry.py` with matching tool policy (including `write` if checkpoints needed)
- [ ] Role added to correct `*_ROLE_NAMES` frozenset in `orchestrator.py`
- [ ] Role added to `ALWAYS_RESPAWN` in `orchestrator.py` if it should auto-restart
- [ ] Startup sequence explicitly skips boilerplate files
- [ ] Title length constrained to < 200 characters
- [ ] Enum values (severity, risk, ticket_class) specified as lowercase strings
- [ ] Checkpoint format matches §7.5 standard (no `"reason": "completed"` field)
- [ ] Heartbeat format specified as bare Unix timestamp per §7.6
- [ ] Error recovery behavior defined per §7.7 (no retry loops)
- [ ] Prompt word count within limits for target model context size per §1.5
- [ ] Path variables used instead of hardcoded absolute paths per §2.3
- [ ] Role added to queue scaling logic in orchestrator if applicable (`_scale_queue_depth` checks `DISCOVERY_ROLE_NAMES | PLANNING_ROLE_NAMES`)
- [ ] Role added to no-ticket penalty tracking in orchestrator (exit handler checks at lines ~4158 and ~4269)
- [ ] `create_ticket` tool policy includes `write` if agent needs checkpoint saving
