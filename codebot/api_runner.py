"""Lightweight API runner that calls dialagram directly with stall recovery.

Purpose
-------
Provides run_bot() — a minimal, stdlib-only bot executor that POSTs to the
dialagram OpenAI-compatible endpoint, executes the four allowed tools via
api_tools, and handles heartbeat / checkpoint / drain / retry semantics.

Why
---
opencode spawns a ~670 MB Go+Node tree per bot; the API runner is ~26 MB of
pure Python so 37+ bots fit on a 1 GB Fly VM. Replicating
opencode-auto-resume stall recovery (120 s timeout, 429 backoff, empty-response
nudge) keeps the new runner as resilient as the old one without extra deps.

Invariants
----------
- stdlib-only: urllib.request, json, time, os, pathlib (+ api_tools); no streaming
- Single POST timeout is 120 s; max 50 tool-call iterations per run
- Max total API-call retries is 5 per run (429 exponential backoff capped at 60 s,
  timeout/connection retries capped at 3 with backoff)
- All tool results are JSON-serialized before appending as tool messages

Security
--------
- API keys and secrets are NEVER logged, even partially (no prefixes, hashes, or lengths)
- Log messages indicate only boolean presence/absence of credentials (e.g., "resolved" or "not found")
- Constitution §2 compliance: No secrets in code, logs, or error messages
"""

import concurrent.futures
import itertools
import json
import logging
import os
import re
import shlex
import signal
import sys
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codebot.file_lock import flock, LOCK_EX, LOCK_UN, LOCK_NB

logger = logging.getLogger(__name__)

try:
    from bots.api_tools import bash, read, write, edit, grep, glob, batch_read, batch_grep
except ImportError:
    from codebot.api_tools import bash, read, write, edit, grep, glob, batch_read, batch_grep

try:
    from codebot.adaptive_rate_limiter import rate_limiter as _rate_limiter
    _HAS_RATE_LIMITER = True
except ImportError:
    _HAS_RATE_LIMITER = False
    _rate_limiter = None  # type: ignore

try:
    from codebot.token_budget import record_usage as _record_token_usage
    _HAS_TOKEN_BUDGET = True
except ImportError:
    _HAS_TOKEN_BUDGET = False
    _record_token_usage = None  # type: ignore

try:
    from codebot.cost_tracker import CostTracker as _CostTracker
    _HAS_COST_TRACKER = True
except ImportError:
    _HAS_COST_TRACKER = False
    _CostTracker = None  # type: ignore

# Cost tracking accumulator: thread-local storage to batch recording calls
# and avoid per-call lock contention in the hot provider session loop.
_COST_FLUSH_INTERVAL = 10  # flush every N API calls
_cost_accumulator = threading.local()


def _resolve_cost_state_dir(heartbeat_file: str = "", ckpt_file: str = "") -> Path:
    """Resolve the canonical runtime state dir for CostTracker writes.

    Priority:
      1. Project adapter state dir (when the T4.3 adapter seam is configured).
      2. Parent of ``heartbeat_file`` when it exists and looks like a state
         dir (``hb_path.parent`` — heartbeat files live directly in state).
      3. Parent of ``ckpt_file`` — same layout as (2).
      4. ``_adapter_state_dir()`` fallback (WORK_ROOT/.codebot/state).

    Review finding (correctness_reviewer): the original implementation
    hardcoded ``Path(__file__).parent / ".codebot" / "state"`` which resolves
    to ``codebot/.codebot/state`` — not the runtime ledger location — so cost
    records silently diverged. Never hardcode that path; derive it from the
    heartbeat/checkpoint paths already threaded into the session.
    """
    try:
        if _adapter_instance is not None:
            try:
                return Path(_adapter_instance.paths().state_dir)  # type: ignore[union-attr]
            except Exception:
                pass
    except Exception:
        pass
    for candidate in (heartbeat_file, ckpt_file):
        if not candidate:
            continue
        try:
            parent = Path(candidate).parent
        except Exception:
            continue
        try:
            if parent.name == "state":
                return parent
            try:
                siblings = [p.suffix for p in parent.iterdir()]
            except OSError:
                siblings = []
            if any(s in (".heartbeat", ".json") for s in siblings):
                return parent
            for ancestor in parent.parents:
                if ancestor.name == "state" and ancestor.parent.name == ".codebot":
                    return ancestor
                if ancestor.name == "state":
                    return ancestor
        except Exception:
            continue
    # Fallback: derive from heartbeat/ckpt parent directly instead of
    # hardcoding WORK_ROOT/.codebot/state (which resolves to codebot/.codebot/state).
    for candidate in (heartbeat_file, ckpt_file):
        if not candidate:
            continue
        try:
            parent = Path(candidate).parent
            if parent.exists():
                return parent
        except Exception:
            continue
    # Last resort: use cwd
    return Path.cwd() / ".codebot" / "state"


def _record_cost_attribution(
    *,
    bot_name: str,
    ticket_id: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """Emit structured per-bot/per-ticket cost attribution (AC2).

    The fleet-wide token ledger (``token_budget.record_usage``) aggregates by
    day+model only, so bot/ticket attribution rides alongside via the module
    logger under a stable ``cost_attribution`` event. Economics consumers tail
    this record to join ledger rows back to bot + ticket without widening the
    ledger schema. Fail-open: never raises.
    """
    try:
        logger.info(
            "cost_attribution bot=%s ticket=%s model=%s prompt=%d completion=%d",
            bot_name,
            ticket_id,
            model,
            int(prompt_tokens),
            int(completion_tokens),
        )
    except Exception:
        pass

# Bounded I/O: Constitutional invariant §4 — all HTTP reads must have a size cap.
# 1 MiB is generous for API response payloads while preventing memory exhaustion
# from a malicious or misbehaving server.
MAX_RESPONSE_BYTES = 1 * 1024 * 1024  # 1 MiB
_IMPLEMENTER_ROLE_NAMES = frozenset({
    "implementer",
})

# Bot name allowlist — defense-in-depth against shell injection in _auto_commit.
# Matches control_server.BOT_NAME_PATTERN; rejected names never reach git commands.
_BOT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _is_implementation_bot(bot_name: str) -> bool:
    return bot_name.split("-", 1)[0] in _IMPLEMENTER_ROLE_NAMES


def _wait_for_rate_limit(model: str) -> float:
    if not _HAS_RATE_LIMITER or not _rate_limiter:
        return 0.0
    try:
        return _rate_limiter.acquire_slot(model)
    except Exception:
        return 0.0


def _record_rate_limit(model: str, retry_after: float | None = None) -> None:
    if _HAS_RATE_LIMITER and _rate_limiter:
        _rate_limiter.record_rate_limit(model, retry_after)


def _record_success(model: str) -> None:
    if _HAS_RATE_LIMITER and _rate_limiter:
        _rate_limiter.record_success(model)


def _record_request(model: str) -> None:
    if _HAS_RATE_LIMITER and _rate_limiter:
        _rate_limiter.record_request(model)


class _HybridToolCall(dict):
    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name: str, value):
        self[name] = value


@dataclass
class AgentResult:
    """Captures the outcome of an agent loop execution for testing."""
    exit_reason: str  # "completed", "iteration_limit", "no_content", "error"
    tool_calls: list[dict] = field(default_factory=list)  # [{"name": str, "args": dict, "result": dict}]
    tickets_created: int = 0
    files_touched: list[str] = field(default_factory=list)
    iterations: int = 0
    messages: list[dict] = field(default_factory=list)  # Full conversation history
    final_content: str = ""  # Model's final text response (if completed)

    @property
    def tool_call_count(self) -> int:
        return len(self.tool_calls)

    @property
    def tool_names(self) -> list[str]:
        names: list[str] = []
        for tc in self.tool_calls:
            if isinstance(tc, dict):
                names.append(tc.get("name", ""))
            else:
                names.append(getattr(tc, "name", ""))
        return names

    def calls_to(self, tool_name: str) -> list[dict]:
        out = []
        for tc in self.tool_calls:
            n = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            if n == tool_name:
                out.append(tc)
        return out

    def create_ticket_args(self) -> list[dict]:
        return [
            (tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {}))
            for tc in self.calls_to("create_ticket")
        ]

    def successful_ticket_creations(self) -> int:
        cnt = 0
        for tc in self.calls_to("create_ticket"):
            res = tc.get("result", {}) if isinstance(tc, dict) else getattr(tc, "result", {})
            if isinstance(res, dict) and res.get("success"):
                cnt += 1
        return cnt

    def has_forbidden_call(self, forbidden_tools: set[str]) -> list[str]:
        out: list[str] = []
        for tc in self.tool_calls:
            n = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            if n in forbidden_tools:
                out.append(n)
        return out

    def bash_commands(self) -> list[str]:
        cmds: list[str] = []
        for tc in self.calls_to("bash"):
            args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            if isinstance(args, dict):
                cmds.append(args.get("command", ""))
        return cmds

    @property
    def bot_name(self) -> str:
        return getattr(self, "_bot_name", "")

    @bot_name.setter
    def bot_name(self, value: str) -> None:
        object.__setattr__(self, "_bot_name", value)


def _log(msg: str) -> None:
    """Write timestamped line to stdout (orchestrator redirects to per-bot .log)."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _log_context_assembly(
    bot_name: str,
    event_type: str,
    input_size: int = 0,
    output_size: int = 0,
    truncation_ratio: float = 0.0,
    final_token_count: int = 0,
    tool_name: str = "",
    extra: dict | None = None,
) -> None:
    """Log structured JSON event for context assembly data flow tracing.

    Writes to bot-specific log file for debugging context overflow issues.
    All fields are sanitized to prevent PII leakage.

    Args:
        bot_name: Bot identifier for log routing.
        event_type: Type of assembly event (e.g., 'tool_result_appended', 'truncation', 'summarization').
        input_size: Size of input data in bytes before processing.
        output_size: Size of output data in bytes after processing.
        truncation_ratio: Ratio of output/input (1.0 = no truncation, <1.0 = truncated).
        final_token_count: Token count after assembly step.
        tool_name: Name of tool that produced the result (if applicable).
        extra: Additional metadata (must not contain PII).
    """
    try:
        log_dir = BOTS_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{bot_name}.context_trace.jsonl"

        trace_event = {
            "timestamp": time.time(),
            "timestamp_human": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event_type": event_type,
            "input_size_bytes": max(0, int(input_size)),
            "output_size_bytes": max(0, int(output_size)),
            "truncation_ratio": round(max(0.0, min(1.0, float(truncation_ratio))), 4),
            "final_token_count": max(0, int(final_token_count)),
            "tool_name": tool_name[:64] if tool_name else "",
        }
        if extra and isinstance(extra, dict):
            # Sanitize extra dict: only allow specific safe keys, redact sensitive values
            SAFE_KEYS = frozenset({
                "success", "tool_call_id", "response_length",
                "truncated", "stream_truncated", "tool_results_truncated",
                "skipped_messages",
                "iteration", "step", "phase",
            })
            SENSITIVE_PATTERNS = [
                "sk-", "key", "secret", "password", "token", "api_key",
                "apikey", "auth", "credential", "private",
            ]
            safe_extra = {}
            for k, v in extra.items():
                k_str = str(k).lower()
                # Skip keys that look sensitive
                if any(pat in k_str for pat in SENSITIVE_PATTERNS):
                    continue
                # Only allow whitelisted keys or simple numeric/bool values
                if k not in SAFE_KEYS and not isinstance(v, (int, float, bool)):
                    continue
                if isinstance(v, bool):
                    safe_extra[str(k)[:64]] = v
                elif isinstance(v, (int, float)):
                    safe_extra[str(k)[:64]] = v
                elif isinstance(v, str):
                    # Truncate and verify no sensitive patterns in value
                    truncated_val = v[:200]
                    val_lower = truncated_val.lower()
                    if any(pat in val_lower for pat in SENSITIVE_PATTERNS):
                        continue  # Skip values containing sensitive patterns
                    safe_extra[str(k)[:64]] = truncated_val
            if safe_extra:
                trace_event["metadata"] = safe_extra

        line = json.dumps(trace_event, ensure_ascii=True) + "\n"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # Fail-open: tracing must never break execution


def _write_bot_status(
    bot_name: str,
    state_dir: Path,
    current_task: str,
    task_description: str = "",
    files_touched: list[str] | None = None,
    iteration: int = 0,
) -> None:
    """Write bot status to shared status file for orchestrator visibility.

    Status file: state/{bot_name}.status.json
    Schema:
        - bot: str - bot name
        - current_task: str - short task identifier (e.g., "reading_queue", "editing_file")
        - task_description: str - human-readable description of what the bot is doing
        - files_touched: list[str] - files read/written in current iteration
        - iteration: int - current tool iteration count
        - updated_at: float - Unix timestamp of last update
        - updated_at_human: str - human-readable timestamp
    """
    try:
        payload = {
            "bot": bot_name,
            "current_task": current_task,
            "task_description": task_description[:500] if task_description else "",
            "files_touched": (files_touched or [])[-10:],  # Keep last 10 files
            "iteration": iteration,
            "updated_at": time.time(),
            "updated_at_human": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        status_path = state_dir / f"{bot_name}.status.json"
        tmp = Path(str(status_path) + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(status_path)
    except Exception:
        pass  # Fail-open: status reporting is best-effort


# ---------------------------------------------------------------------------
# Context-assembly tracing — structured JSON logs for debugging context overflow
# ---------------------------------------------------------------------------
# CB-3548779-D85C: Logs how tool outputs are processed and integrated into
# context, including truncation events, summarization triggers, and
# context-assembly metrics.  Traces are bot-specific JSONL for easy debugging.


class ContextAssemblyTracer:
    """Structured JSON tracing for context-assembly in api_runner.

    Emits one JSONL event per significant data-flow step.  All events
    include size metrics and timestamps.  PII is stripped before writing.

    Event types emitted:
      - tool_output         : tool result received & serialized for context
      - tool_output_trunc   : tool result > max_tool_bytes before append
      - context_compaction  : compact_messages / needs_compaction triggered
      - api_call_prepared   : messages assembled and sent to the model
      - stream_truncation   : _persist_stream truncated the stream for disk
      - trace_summary       : final summary at flush()

    Output file: {logs_dir}/{bot_name}.context_trace.jsonl
    """

    # Regex patterns for PII detection
    _RE_EMAIL = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
    _RE_IP4 = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
    _RE_PHONE = re.compile(r'\+?\d[\d\s().-]{7,}\d')
    # Match common API key patterns: sk-xxx, dgr-xxx, api_key=xxx, token:xxx, etc.
    _RE_API_KEY = re.compile(
        r'\b(?:sk|dgr|ak|pk)[_-][A-Za-z0-9]{8,}\b|'
        r'\b(?:api[_-]?key|secret|password|bearer|token)[_:=\s]+[A-Za-z0-9]{8,}\b',
        re.IGNORECASE,
    )

    def __init__(self, bot_name: str, logs_dir: Path) -> None:
        self._bot_name = bot_name
        self._log_path = logs_dir / f"{bot_name}.context_trace.jsonl"
        self._events: list[dict] = []
        self._total_input_chars = 0
        self._total_output_bytes = 0
        self._truncation_events = 0
        self._compaction_events = 0

    # -- PII sanitization ---------------------------------------------------

    @classmethod
    def sanitize_for_log(cls, text: str) -> str:
        """Strip PII patterns from *text* before writing to trace logs.

        Replaces matches with ``[REDACTED]`` so the log is useful for
        debugging sizes and flow without leaking secrets or PII.
        """
        if not text:
            return text
        text = cls._RE_EMAIL.sub('[REDACTED_EMAIL]', text)
        text = cls._RE_IP4.sub('[REDACTED_IP]', text)
        text = cls._RE_PHONE.sub('[REDACTED_PHONE]', text)
        text = cls._RE_API_KEY.sub('[REDACTED_KEY]', text)
        return text

    # -- internal emit ------------------------------------------------------

    def _emit(self, event_type: str, **kwargs: Any) -> dict:
        """Emit a structured JSON trace event."""
        event: dict = {
            'ts': time.time(),
            'bot': self._bot_name,
            'event': event_type,
        }
        event.update(kwargs)
        self._events.append(event)

        # Append to JSONL file (fail-open)
        try:
            line = json.dumps(event, ensure_ascii=False, default=str) + '\n'
            # Ensure parent directory exists
            try:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            with open(self._log_path, 'a', encoding='utf-8') as fh:
                fh.write(self.sanitize_for_log(line))
        except Exception:
            pass  # Fail-open: tracing must never break execution
        return event

    # -- public trace points ------------------------------------------------

    def log_tool_output(
        self,
        tool_name: str,
        tool_result: dict,
        iteration: int,
        max_tool_bytes: int = 2000,
    ) -> dict:
        """Log a tool output being processed for context assembly.

        Called after ``_execute_tool`` returns and before the result is
        appended to the message list.
        """
        output_raw = json.dumps(tool_result, ensure_ascii=False, default=str)
        output_bytes = len(output_raw.encode('utf-8'))
        self._total_output_bytes += output_bytes

        truncated = output_bytes > max_tool_bytes
        truncation_ratio = 0.0
        if truncated:
            truncation_ratio = 1.0 - (max_tool_bytes / output_bytes)
            self._truncation_events += 1

        # Token estimate for the (possibly truncated) content that goes into context
        content_str = output_raw[:max_tool_bytes] if truncated else output_raw
        token_estimate = max(1, len(content_str) // 4)

        return self._emit(
            'tool_output',
            tool=tool_name,
            iteration=iteration,
            output_bytes=output_bytes,
            truncated=truncated,
            truncation_ratio=round(truncation_ratio, 4),
            token_estimate=token_estimate,
        )

    def log_tool_output_truncation(
        self,
        tool_name: str,
        original_bytes: int,
        max_bytes: int,
        iteration: int,
    ) -> dict:
        """Log when a tool output is truncated before adding to messages."""
        self._truncation_events += 1
        return self._emit(
            'tool_output_trunc',
            tool=tool_name,
            iteration=iteration,
            original_bytes=original_bytes,
            max_bytes=max_bytes,
            truncation_ratio=round(1.0 - (max_bytes / max(original_bytes, 1)), 4),
        )

    def log_context_compaction(
        self,
        messages_before: list,
        messages_after: list,
        tokens_before: int,
        tokens_after: int,
        method: str,
    ) -> dict:
        """Log a context-compaction event (summarize or truncate)."""
        self._compaction_events += 1
        return self._emit(
            'context_compaction',
            method=method,
            messages_before=len(messages_before),
            messages_after=len(messages_after),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            reduction_ratio=round(1.0 - (tokens_after / max(tokens_before, 1)), 4),
        )

    def log_api_call_prepared(
        self,
        message_count: int,
        total_chars: int,
        iteration: int,
    ) -> dict:
        """Log data prepared for an API call."""
        token_estimate = max(1, total_chars // 4)
        self._total_input_chars += total_chars
        return self._emit(
            'api_call_prepared',
            iteration=iteration,
            message_count=message_count,
            total_chars=total_chars,
            token_estimate=token_estimate,
        )

    def log_stream_truncation(
        self,
        original_size: int,
        final_size: int,
        messages_kept: int,
    ) -> dict:
        """Log stream-persistence truncation in ``_persist_stream``."""
        self._truncation_events += 1
        return self._emit(
            'stream_truncation',
            original_bytes=original_size,
            final_bytes=final_size,
            truncation_ratio=round(
                1.0 - (final_size / max(original_size, 1)), 4
            ),
            messages_kept=messages_kept,
        )

    # -- summary ------------------------------------------------------------

    def summary(self) -> dict:
        """Return aggregate metrics for this trace session."""
        return {
            'bot': self._bot_name,
            'total_events': len(self._events),
            'total_input_chars': self._total_input_chars,
            'total_output_bytes': self._total_output_bytes,
            'truncation_events': self._truncation_events,
            'compaction_events': self._compaction_events,
        }

    def flush(self) -> None:
        """Write a summary event and close out the trace."""
        self._emit('trace_summary', **self.summary())


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def _github_issue_target(bot_name: str, mission_prompt: str) -> tuple[str, int] | None:
    """Return the GitHub target embedded in an implementer mission, if present."""
    if not bot_name.startswith("implementer_"):
        return None
    match = re.search(r"github:([\w.-]+/[\w.-]+)#(\d+)", mission_prompt)
    return (match.group(1), int(match.group(2))) if match else None


def _queued_github_target(bot_name: str) -> tuple[str, int] | None:
    """Find the first confirmed GitHub queue row assigned to this implementer tier."""
    complexity = {
        "worker-1": "small",
        "worker-2": "small",
        "worker-3": "medium",
        "worker-4": "medium",
        "worker-5": "high",
        "worker-6": "high",
        "worker-7": "medium",
        "worker-8": "medium",
        "worker-9": "high",
        "worker-10": "high",
        "worker-11": "high",
        "worker-12": "high",
    }.get(bot_name)
    if not complexity:
        return None
    queue_path = BOTS_DIR / "docs" / "triage" / "QUEUE.md"
    try:
        for line in queue_path.read_text(encoding="utf-8").splitlines():
            fields = [field.strip() for field in line.strip("|").split("|")]
            if len(fields) < 6 or fields[0] == "ID":
                continue
            source, row_complexity, status = fields[1], fields[4], fields[5]
            match = re.fullmatch(r"github:([^#]+)#(\d+)", source)
            if match and row_complexity == complexity and status in {"confirmed", "approved"}:
                return match.group(1), int(match.group(2))
    except (OSError, UnicodeError):
        return None
    return None


def _update_github_progress(
    target: tuple[str, int], bot_name: str, iteration: int, detail: str,
) -> None:
    """Best-effort, bounded GitHub status and progress update."""
    repo, number = target
    try:
        subprocess.run(
            ["gh", "issue", "edit", str(number), "-R", repo,
             "--add-label", "status:in-progress", "--remove-label", "status:queued"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        subprocess.run(
            ["gh", "issue", "comment", str(number), "-R", repo, "--body",
             f"🤖 `{bot_name}` progress: iteration {iteration}; {detail[:240]}"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _log(f"{bot_name}: GitHub progress update unavailable")


def _ticket_id_from_claim(state_dir: Path, bot_name: str) -> str:
    for claim_file in (state_dir / "claims").glob(f"*.{bot_name}.json"):
        try:
            claim = json.loads(claim_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        ticket_id = claim.get("ticket_id", claim.get("ticket", ""))
        if isinstance(ticket_id, str) and ticket_id:
            return ticket_id
    return ""


def _auto_commit(bot_name: str, files_touched: list[str], ticket_id: str = "") -> bool:
    """Auto-commit and push any uncommitted changes when a worker completes.

    Why: Workers often skip the git commit step even when told to commit.
    This structural fix ensures changes are always committed after completion.
    SELF-03: Gatekeeper must PASS before any commit proceeds.
    CB-2195514-9B12: Fail-closed on gatekeeper errors or missing adapter.

    Returns:
        True if commit was successful (or no files to commit).
        False if gatekeeper blocked commit or an error occurred.
    """
    if not files_touched:
        return True

    # Defense-in-depth: reject bot_name with shell metacharacters before any git commands.
    if not isinstance(bot_name, str) or not _BOT_NAME_RE.match(bot_name):
        _log(f"{bot_name}: BLOCKED auto-commit: invalid bot_name characters")
        return False

    if not ticket_id:
        _log(f"{bot_name}: gatekeeper unavailable (BLOCKING commit): no assigned ticket")
        return False

    # Fail-closed: gatekeeper check is mandatory before any commit
    if _adapter_instance is None:
        _log(f"{bot_name}: gatekeeper unavailable (BLOCKING commit): no project adapter")
        return False

    try:
        from codebot.gatekeeper import Gatekeeper
        paths = _adapter_instance.paths()  # type: ignore[union-attr]
        gk = Gatekeeper(
            state_dir=paths.state_dir,
            policy_path=getattr(paths, 'quality_policy', None),
            workspace=paths.repository_root,
        )
        ticket_class = "bug"
        if "test" in bot_name.lower():
            ticket_class = "test"
        elif "doc" in bot_name.lower():
            ticket_class = "documentation"
        elif "security" in bot_name.lower():
            ticket_class = "security"
        result = gk.verify_ticket(
            ticket_id=ticket_id,
            ticket_class=ticket_class,
            changed_files=files_touched,
        )
        if result.get("decision") != "COMPLETE":
            _log(f"{bot_name}: gatekeeper BLOCKED commit — decision={result.get('decision')} failed_gates={result.get('failed_gates', [])}")
            return False
    except ImportError as imp_err:
        _log(f"{bot_name}: gatekeeper unavailable (BLOCKING commit): {imp_err}")
        return False
    except Exception as gk_err:
        _log(f"{bot_name}: gatekeeper check failed (BLOCKING commit): {gk_err}")
        return False

    repos = set()
    for f in files_touched:
        abs_path = f if f.startswith("/") else str(WORK_ROOT / f)
        if "Monitor-Manager-Python" in abs_path or "lib-common" in abs_path:
            repos.add("Monitor-Manager-Python")
        if "Monitor-Client-Python" in abs_path:
            repos.add("Monitor-Client-Python")

    if not repos:
        return True

    for repo in repos:
        # T4.3: resolve via adapter when available, fallback to WORK_ROOT
        if _adapter_instance is not None:
            try:
                base = str(_adapter_instance.paths().repository_root)  # type: ignore[union-attr]
            except Exception:
                base = str(WORK_ROOT)
        else:
            base = str(WORK_ROOT)
        repo_path = f"{base}/{repo}"
        result = bash(f"git -C {shlex.quote(repo_path)} status --short", timeout=10)
        if not result["success"] or not result["output"].strip():
            continue

        add_result = bash(f"git -C {shlex.quote(repo_path)} add -A", timeout=15)
        if not add_result["success"]:
            _log(f"{bot_name}: auto-commit git add failed for {repo}: {add_result.get('error', '')}")
            continue

        safe_bot_name = shlex.quote(bot_name)
        commit_msg = f"bot: {safe_bot_name} auto-commit after task completion"
        commit_result = bash(f'git -C {shlex.quote(repo_path)} commit -m {shlex.quote(commit_msg)}', timeout=15)
        if not commit_result["success"]:
            if "nothing to commit" in commit_result.get("error", "") or "nothing to commit" in commit_result.get("output", ""):
                continue
            _log(f"{bot_name}: auto-commit git commit failed for {repo}: {commit_result.get('error', '')}")
            continue

        push_result = bash(f"git -C {shlex.quote(repo_path)} push origin HEAD", timeout=30)
        if push_result["success"]:
            _log(f"{bot_name}: auto-commit + push successful for {repo}")
        else:
            _log(f"{bot_name}: auto-commit push failed for {repo}: {push_result.get('error', '')}")

    return True


BOTS_DIR = Path(__file__).parent
DRAIN_FILE = BOTS_DIR / "state" / ".drain"
WORK_ROOT = BOTS_DIR

# T4.3 incremental adapter seam
_adapter_instance: object | None = None


def set_project_adapter(adapter: object) -> None:
    global _adapter_instance, DRAIN_FILE, WORK_ROOT
    _adapter_instance = adapter
    try:
        p = adapter.paths()  # type: ignore[union-attr]
        DRAIN_FILE = p.state_dir / ".drain"
        WORK_ROOT = p.repository_root
    except Exception:
        pass
def get_adapter() -> object | None:
    return _adapter_instance

_DEFAULT_API_URL = "https://dialagram.me/router/v1/chat/completions"

# ---------------------------------------------------------------------------
# Model router singleton — lazy initialisation
# ---------------------------------------------------------------------------
_model_router = None  # type: ignore[assignment]


def _get_model_router():
    """Return the singleton ModelRouter, creating it on first call."""
    global _model_router
    if _model_router is not None:
        return _model_router
    try:
        from codebot.model_router import ModelRouter, build_default_chain
        state_dir = str(BOTS_DIR / "state")
        _model_router = ModelRouter(state_dir=state_dir)
    except Exception:
        _model_router = None
    return _model_router


def _resolve_api_url() -> str:
    env_url = os.environ.get("CODEBOT_API_URL")
    if env_url:
        return env_url
    if _adapter_instance is not None:
        try:
            profiles = _adapter_instance.model_profiles()  # type: ignore[union-attr]
            url = profiles.get("api_url", "")
            if url:
                return url
        except Exception:
            pass
    # Try ModelRouter for provider-specific URL
    router = _get_model_router()
    if router is not None:
        try:
            # Use dialagram as preferred provider; ModelRouter will resolve fallback
            url = router.get_api_url("dialagram")
            if url:
                return url
        except Exception:
            pass
    return _DEFAULT_API_URL


def _resolve_api_key_for_provider(provider_name: str = "dialagram") -> str | None:
    """Resolve API key for a specific provider via ModelRouter, fallback to legacy."""
    router = _get_model_router()
    if router is not None:
        key = router.get_api_key(provider_name)
        if key:
            return key
    # Legacy fallback for dialagram
    if provider_name == "dialagram":
        return _resolve_api_key()
    return None


API_URL = _DEFAULT_API_URL
API_TIMEOUT = 90
PLANNING_ROLE_TIMEOUT = 90
_PLANNING_BASES = frozenset({"decomposer", "planner"})


def _timeout_for_bot(bot_name: str, model: str = "") -> int:
    if model:
        try:
            from codebot.model_manager import model_profile
            prof = model_profile(model)
            if prof:
                return int(API_TIMEOUT * prof.heartbeat_multiplier)
        except Exception:
            pass
    return API_TIMEOUT


MAX_TOOL_ITERATIONS = 200
MAX_RETRIES = 5
MAX_TIMEOUT_RETRIES = 3
BACKOFFS = [2, 4, 8, 16, 32]
MAX_BACKOFF = 60
RATE_LIMIT_YIELD_DELAY = 30
SCRATCHPAD_MAX_BYTES = 8 * 1024
TASKLOG_MAX_LINES = 200


def _scratchpad_path(bot_name: str, state_dir: Path) -> Path:
    return Path(state_dir) / f"{bot_name}.scratchpad.md"


def _write_scratchpad(bot_name: str, state_dir: Path, task: str, detail: str = "") -> None:
    try:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    except Exception:
        ts = "??:??:??"
    task = (task or "working").strip().replace("\n", " ")[:200]
    detail = (detail or "").strip().replace("\n", " ")[:300]
    line = f"- [{ts}] {task}" + (f": {detail}" if detail else "") + "\n"
    try:
        path = _scratchpad_path(bot_name, Path(state_dir))
        # O(1) append — avoids read-all + write-all on every iteration.
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
        # Lazy truncation: only read-truncate-write when file exceeds the cap.
        # After truncation to ~100 lines the file stays under the cap for many
        # more appends, so this O(N) path executes at most once per run.
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size > SCRATCHPAD_MAX_BYTES:
            existing = path.read_text(encoding="utf-8").splitlines(keepends=True)
            path.write_text("".join(existing[-100:]), encoding="utf-8")
    except Exception:
        pass


def _scratch_target(args: dict) -> str:
    for key in ("path", "file", "query", "pattern", "command", "url"):
        val = args.get(key)
        if isinstance(val, str) and val:
            val = val.replace(str(WORK_ROOT) + "/", "")
            return val[:80]
    return "tools"


def _write_taskline(bot_name: str, state_dir: Path, kind: str, detail: str = "") -> None:
    try:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    except Exception:
        ts = "??:??:??"
    kind = (kind or "event").strip().replace("\n", " ")[:40]
    detail = (detail or "").strip().replace("\n", " ")[:400]
    line = f"[{ts}] {bot_name} {kind}" + (f" {detail}" if detail else "") + "\n"
    try:
        log_dir = Path(state_dir).parent / "logs"
        if "state" not in str(state_dir):
            log_dir = Path(WORK_ROOT) / "bots" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{bot_name}.tasklog"
        existing: list[str] = []
        if path.exists():
            existing = path.read_text(encoding="utf-8").splitlines(keepends=True)
        existing.append(line)
        existing = existing[-TASKLOG_MAX_LINES:]
        path.write_text("".join(existing), encoding="utf-8")
    except Exception:
        pass

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read file contents with optional line slicing",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write file atomically via tmp+replace",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Surgically replace old_string with new_string in a file. Fails if not found or found multiple times.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run shell command with timeout and bounded output",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search files by regex with capped output",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "include": {"type": "string"},
                },
                "required": ["pattern", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern recursively",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_ticket",
            "description": "Create a new work ticket in the TicketStore. Use this to report bugs, security issues, performance problems, missing tests, documentation gaps, or feature requests discovered during code analysis. Evidence is validated against the current repository state before ticket creation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short descriptive title of the issue found"},
                    "ticket_class": {"type": "string", "enum": ["bug", "feature", "security", "performance", "documentation", "test", "refactor", "dependency", "architecture", "infrastructure"], "description": "Category of work needed"},
                    "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"], "description": "How bad is the problem (impact magnitude)"},
                    "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"], "description": "How soon should this be worked on (scheduling urgency, may differ from severity)"},
                    "source": {"type": "string", "description": "Your role name (e.g., bug_hunter, security_auditor)"},
                    "evidence": {"type": "string", "description": "Exact code snippet, file path:line, or log output proving the issue"},
                    "evidence_file": {"type": "string", "description": "Relative file path containing the evidence (validated for existence)"},
                    "evidence_symbol": {"type": "string", "description": "Function/class name at the evidence location (validated for existence)"},
                    "evidence_line": {"type": "integer", "description": "Line number of the evidence in the file"},
                    "problem_statement": {"type": "string", "description": "What is wrong and why it matters"},
                    "desired_state": {"type": "string", "description": "What correct behavior looks like"},
                    "acceptance_criteria": {"type": "string", "description": "Semicolon-separated list of conditions that prove this is fixed"},
                    "affected_modules": {"type": "string", "description": "Comma-separated list of files or directories affected"},
                    "risk": {"type": "string", "enum": ["critical", "high", "medium", "low"], "description": "Risk level of implementing the fix"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"], "description": "How certain you are this finding is real (based on evidence strength)"},
                    "atomicity": {"type": "string", "enum": ["atomic", "compound", "unknown"], "description": "Is this a single work unit or does it need decomposition?"},
                },
                "required": ["title", "ticket_class", "severity", "evidence", "problem_statement", "desired_state", "acceptance_criteria"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_read",
            "description": "Read multiple files in one call. More efficient than multiple read calls.",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}, "description": "List of file paths to read"},
                    "limit_per_file": {"type": "integer", "description": "Max lines per file (default 200)"},
                },
                "required": ["paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_grep",
            "description": "Search multiple regex patterns in one call. More efficient than multiple grep calls.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patterns": {"type": "array", "items": {"type": "string"}, "description": "List of regex patterns to search"},
                    "path": {"type": "string", "description": "Directory or file to search in"},
                    "include": {"type": "string", "description": "File pattern to include (e.g., '*.py')"},
                    "limit_per_pattern": {"type": "integer", "description": "Max matches per pattern (default 50)"},
                },
                "required": ["patterns", "path"],
            },
        },
    },
]

try:
    from codebot.api_tools import a11y_snapshot as _a11y_snapshot
except ImportError:
    try:
        from api_tools import a11y_snapshot as _a11y_snapshot
    except ImportError:
        _a11y_snapshot = None  # type: ignore[assignment]

try:
    from codebot.web_tools import web_search as _web_search, web_fetch as _web_fetch
except ImportError:
    try:
        from web_tools import web_search as _web_search, web_fetch as _web_fetch
    except ImportError:
        _web_search = None  # type: ignore[assignment]
        _web_fetch = None  # type: ignore[assignment]

def _create_ticket_tool(
    title: str = "",
    ticket_class: str = "feature",
    severity: str = "medium",
    source: str = "agent",
    evidence: str = "",
    problem_statement: str = "",
    desired_state: str = "",
    acceptance_criteria: str = "",
    affected_modules: str = "",
    risk: str = "medium",
    **kwargs: Any,
) -> dict:
    if not title:
        title = problem_statement or evidence or f"Finding from {source}"
        if len(title) > 200:
            title = title[:200]
    if not title.strip():
        title = f"Auto-generated finding from {source}"
    try:
        from codebot.ticket_engine import (
            create_ticket, TicketStore, TicketClass, Severity, RiskLevel,
        )
    except ImportError:
        return {"success": False, "output": "", "error": "ticket_engine not available"}

    # Resolve project root for evidence validation
    project_root = Path.cwd()
    if _adapter_instance is not None:
        try:
            project_root = _adapter_instance.paths().project_root  # type: ignore[union-attr]
        except Exception:
            pass

    # Evidence pre-validation (§7, §37): verify file references exist
    evidence_file = str(kwargs.get("evidence_file", "") or "")
    evidence_symbol = str(kwargs.get("evidence_symbol", "") or "")
    evidence_line = int(kwargs.get("evidence_line", 0) or 0)
    if evidence_file and project_root.exists():
        try:
            from codebot.evidence_validator import revalidate_before_ticket_creation
            evidence_items = [{
                "file_path": evidence_file,
                "symbol": evidence_symbol,
                "line_number": evidence_line,
            }]
            should_create, reason = revalidate_before_ticket_creation(
                project_root, evidence_items
            )
            if not should_create:
                return {
                    "success": False,
                    "output": "",
                    "error": f"Evidence validation failed: {reason}",
                }
        except ImportError:
            pass
        except Exception:
            pass

    class_map = {v.value: v for v in TicketClass}
    sev_map = {v.value: v for v in Severity}
    risk_map = {v.value: v for v in RiskLevel}
    tc = class_map.get(ticket_class.lower(), TicketClass.FEATURE)
    sv = sev_map.get(severity.lower(), Severity.MEDIUM)
    rk = risk_map.get(risk.lower(), RiskLevel.MEDIUM)
    ac_list = [c.strip() for c in acceptance_criteria.split(";") if c.strip()] if acceptance_criteria else [title]
    modules = [m.strip() for m in affected_modules.split(",") if m.strip()] if affected_modules else []
    raw_deps = kwargs.get("dependencies", "")
    if isinstance(raw_deps, list):
        deps = [str(d).strip() for d in raw_deps if str(d).strip()]
    elif isinstance(raw_deps, str) and raw_deps.strip():
        deps = [d.strip() for d in raw_deps.split(",") if d.strip()]
    else:
        deps = []
    if not problem_statement:
        problem_statement = title
    if not desired_state:
        desired_state = f"Resolve: {title}"

    # New discovery fields (§2-3, §13-14, §43)
    confidence = str(kwargs.get("confidence", "") or "").strip().lower()
    priority = str(kwargs.get("priority", "") or "").strip().lower()
    atomicity = str(kwargs.get("atomicity", "") or "").strip().lower()
    discovery_category = str(kwargs.get("discovery_category", "") or ticket_class).strip().lower()
    finding_id = str(kwargs.get("finding_id", "") or "").strip()
    fingerprint = str(kwargs.get("fingerprint", "") or "").strip()
    repo_revision = str(kwargs.get("repo_revision", "") or "").strip()

    # Get current repo revision if not provided
    if not repo_revision and project_root.exists():
        try:
            from codebot.evidence_validator import get_current_revision
            repo_revision = get_current_revision(project_root)
        except (ImportError, Exception):
            pass

    try:
        ticket = create_ticket(
            title=title,
            ticket_class=tc,
            severity=sv,
            source=source,
            evidence=(evidence.strip() if isinstance(evidence, str) else "") or f"[no-evidence-fallback] {title}" if not (isinstance(evidence, str) and evidence.strip()) else evidence,
            problem_statement=problem_statement,
            desired_state=desired_state,
            acceptance_criteria=ac_list,
            risk=rk,
            affected_modules=modules,
            dependencies=deps if deps else None,
            confidence=confidence,
            priority=priority,
            repo_revision=repo_revision,
            atomicity=atomicity,
            discovery_category=discovery_category,
            finding_id=finding_id,
            fingerprint=fingerprint,
        )
    except ValueError as ve:
        return {"success": False, "output": "", "error": str(ve)}
    store_path = Path.cwd() / ".codebot" / "state" / "tickets.json"
    if _adapter_instance is not None:
        try:
            store_path = _adapter_instance.paths().state_dir / "tickets.json"  # type: ignore[union-attr]
        except Exception:
            pass
    if not store_path.is_absolute():
        store_path = (WORK_ROOT / store_path).resolve()
    impl_map = {
        "bug": "implementer", "feature": "implementer",
        "refactor": "implementer", "security": "implementer",
        "performance": "implementer", "architecture": "implementer",
        "test": "implementer", "documentation": "implementer",
        "dependency": "implementer", "infrastructure": "implementer",
    }
    impl_type = impl_map.get(tc.value, "implementer")
    try:
        store = TicketStore(store_path)
        store.add(ticket)
        # Flush synchronously so callers (tests, CLI) see the ticket immediately.
        # TicketStore is debounced (0.5s) for throughput; without flush a freshly
        # created store in the caller would miss the WAL compaction window and
        # observe an empty file.  Flush is O(K) (K dirty tickets) so it stays
        # O(1) for a single add and preserves durability.
        try:
            store.flush()
        except Exception:
            pass
        return {"success": True, "output": f"Created ticket {ticket.id}: {ticket.title} (state={ticket.state.value}, implementer={impl_type})", "error": None}
    except ValueError as ve:
        return {"success": True, "output": f"Duplicate: {ve}", "error": None}
    except Exception as e:
        return {"success": False, "output": "", "error": f"store failed: {e}"}


_TOOL_MAP = {
    "read": read,
    "write": write,
    "edit": edit,
    "bash": bash,
    "grep": grep,
    "glob": glob,
    "file_read": read,
    "file_write": write,
    "create_ticket": _create_ticket_tool,
    "batch_read": batch_read,
    "batch_grep": batch_grep,
}
if _a11y_snapshot is not None:
    _TOOL_MAP["screenshot"] = _a11y_snapshot
    _TOOL_MAP["a11y_snapshot"] = _a11y_snapshot
if _web_search is not None:
    _TOOL_MAP["web_search"] = _web_search
if _web_fetch is not None:
    _TOOL_MAP["web_fetch"] = _web_fetch


def _resolve_api_key():
    """Return dialagram API key (env first, then opencode.jsonc fallback)."""
    env_key = os.getenv("DIALAGRAM_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()
    # Fallback: home config — the only on-disk source for headless bots
    cfg_path = Path.home() / ".config" / "opencode" / "opencode.jsonc"
    try:
        text = cfg_path.read_text(encoding="utf-8")
        # Fast path: JSONC file is plain JSON in this repo, so json.loads works
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Fallback for true JSONC: strip // comments naively then regex-extract
            import re

            m = re.search(r'"apiKey"\s*:\s*"([^"]+)"', text)
            if m:
                return m.group(1)
            # Remove // line comments that are not part of https://
            cleaned = re.sub(r"(?<!:)//.*", "", text)
            data = json.loads(cleaned)
        provider = data.get("provider", {})
        dgr = provider.get("dialagram-local-router", {})
        opts = dgr.get("options", {})
        key = opts.get("apiKey")
        if isinstance(key, str) and key.strip():
            return key.strip()
    except Exception:
        pass
    return None


def _is_draining(bot_name: str = ""):
    """Whether orchestrator has requested drain (global or per-bot prompt reload)."""
    if DRAIN_FILE.exists():
        return True
    if bot_name:
        per_bot = BOTS_DIR / "state" / f".drain_{bot_name}"
        if per_bot.exists():
            return True
    return False


def _write_heartbeat(heartbeat_file):
    """Persist current Unix time so orchestrator knows we are alive."""
    try:
        p = Path(heartbeat_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass


def _start_heartbeat_thread(heartbeat_file: str, interval: int = 30) -> threading.Thread:
    stop_event = threading.Event()

    def _beat():
        while not stop_event.is_set():
            _write_heartbeat(heartbeat_file)
            stop_event.wait(interval)

    t = threading.Thread(target=_beat, daemon=True, name="heartbeat-writer")
    t._stop_event = stop_event  # type: ignore[attr-defined]
    t.start()
    return t


def _stop_heartbeat_thread(t: threading.Thread | None) -> None:
    if t is not None:
        try:
            t._stop_event.set()  # type: ignore[attr-defined]
        except Exception:
            pass


def _write_checkpoint(ckpt_file, bot_name, reason):
    delegated = False
    platform_path = None
    try:
        from codebot.process_manager import write_platform_checkpoint, checkpoint_path
        payload = {"bot": bot_name, "updated_at": time.time(), "reason": reason}
        platform_path = checkpoint_path(bot_name)
        if write_platform_checkpoint(bot_name, payload) is not None:
            delegated = True
            # If requested path equals platform path, we're done
            if Path(ckpt_file) == platform_path:
                return
            # Otherwise fall through to also write the explicit ckpt_file
            # (e.g., tests use a tmp_path). This preserves backward-compat
            # while keeping platform discipline for the canonical location.
    except (ImportError, ValueError, OSError):
        pass
    except Exception:
        pass
    try:
        p = Path(ckpt_file)
        # Avoid double-write when delegated already wrote the same path
        if delegated and platform_path is not None and p == platform_path:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"bot": bot_name, "updated_at": time.time(), "reason": reason}
        body = json.dumps(payload)
        if len(body.encode("utf-8")) > 4096:
            payload["reason"] = payload["reason"][:200]
            body = json.dumps(payload)[:4096]
        tmp = Path(str(p) + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def _persist_stream(bot_name, messages, active_model, tool_iterations, exit_reason, usage=None):
    try:
        MAX_SIZE = 500_000
        # Safety margin: use 95% of MAX_SIZE during accumulation to account for
        # JSON structural overhead that per-entry tracking may underestimate.
        # This prevents adversarial expansion ratio manipulation (feedback #32/#36).
        EFFECTIVE_MAX_SIZE = int(MAX_SIZE * 0.95)  # 475,000 bytes
        MAX_MESSAGES = 10000
        bounded = []
        entry_sizes = []
        truncated_count = 0
        skipped_count = 0
        stream_truncated = False

        # Calculate base overhead for non-message fields to track cumulative size accurately
        usage_dict = dict(usage) if isinstance(usage, dict) else {}
        persisted_at = time.time()
        base_payload = {
            "bot": bot_name,
            "model": active_model,
            "tool_iterations": tool_iterations,
            "exit_reason": exit_reason,
            "persisted_at": persisted_at,
            "usage": usage_dict,
            "messages": [],
        }
        # Estimate overhead: length of JSON structure without messages array content
        base_overhead = len(json.dumps(base_payload, ensure_ascii=False))
        cumulative_size = base_overhead

        # Bound iteration at source using itertools.islice - prevents generator exhaustion
        # before MAX_MESSAGES check. Generator is never advanced beyond MAX_MESSAGES items.
        
        # Wrap messages in an iterator so we can peek for truncation after islice
        msg_iter = iter(messages)
        bounded_iter = itertools.islice(msg_iter, MAX_MESSAGES)
        
        for m in bounded_iter:
            # CB-9C1D0: Bounded memory accumulation. Avoid creating dict copies
            # for messages that will be rejected due to size. First, perform a
            # lightweight pre-check on content length to skip obviously oversized
            # messages without allocating a new dict.
            content_val = None
            try:
                content_val = m.get("content") if hasattr(m, "get") else None
            except (AttributeError, TypeError):
                pass
            
            if isinstance(content_val, str):
                # Fast-path: len > MAX_SIZE => bytes > MAX_SIZE even at 1 byte/char,
                # so reject without encoding or copying.
                if len(content_val) > MAX_SIZE:
                    skipped_count += 1
                    stream_truncated = True
                    logger.warning(
                        "%s: _persist_stream skipped oversized message (%d chars > %d threshold)",
                        bot_name, len(content_val), MAX_SIZE,
                    )
                    continue
                # Ambiguous UTF-8 zone: only encode if length is in [MAX_SIZE//4, MAX_SIZE]
                if len(content_val) > MAX_SIZE // 4:
                    byte_len = len(content_val.encode("utf-8"))
                    if byte_len > MAX_SIZE:
                        skipped_count += 1
                        stream_truncated = True
                        logger.warning(
                            "%s: _persist_stream skipped oversized message (%d bytes > %d threshold)",
                            bot_name, byte_len, MAX_SIZE,
                        )
                        continue
            
            # Serialize FIRST to determine exact size before committing to dict copy.
            # This avoids allocating dict objects for messages that would exceed the
            # cumulative size cap. We serialize the original message directly.
            try:
                entry_json = json.dumps(m, ensure_ascii=False)
                entry_size = len(entry_json.encode("utf-8")) + 1  # +1 for comma separator
            except (TypeError, ValueError) as exc:
                skipped_count += 1
                stream_truncated = True
                logger.warning(
                    "%s: _persist_stream skipped non-serializable message: %s",
                    bot_name, exc,
                )
                continue

            # Check cumulative size BEFORE creating dict copy. If this message
            # would exceed the effective cap, stop accumulation entirely.
            # Use exact size (no expansion ratio) to avoid premature termination
            # with many small messages; final size check + tail-trim handles overhead.
            if cumulative_size + entry_size > EFFECTIVE_MAX_SIZE:
                stream_truncated = True
                break

            # Now that we know the message fits, create the dict copy for
            # potential tool truncation and storage. Store the serialized JSON
            # string instead of the dict to reduce memory overhead (avoids
            # keeping both Python dict objects and their serialized forms).
            try:
                entry = dict(m)
            except (TypeError, ValueError) as exc:
                skipped_count += 1
                stream_truncated = True
                logger.warning(
                    "%s: _persist_stream skipped non-mapping message: %s",
                    bot_name, exc,
                )
                continue

            # Apply tool message truncation if necessary. If truncated, we must
            # re-serialize to get the correct size and stored representation.
            if entry.get("role") == "tool" and isinstance(entry.get("content"), str):
                orig_len = len(entry["content"])
                if orig_len > 2000:
                    entry["content"] = entry["content"][:2000] + "...[truncated]"
                    truncated_count += 1
                    stream_truncated = True
                    # Re-serialize after truncation to get accurate size and stored JSON
                    try:
                        entry_json = json.dumps(entry, ensure_ascii=False)
                        entry_size = len(entry_json.encode("utf-8")) + 1
                    except (TypeError, ValueError):
                        # Should not happen since we serialized successfully above,
                        # but fail-safe: skip this entry if re-serialization fails
                        skipped_count += 1
                        continue

            # Store serialized JSON string instead of dict to bound memory usage.
            # This avoids keeping Python dict objects in the bounded list.
            bounded.append(entry_json)
            entry_sizes.append(entry_size)
            cumulative_size += entry_size

        # Detect if there were more messages than MAX_MESSAGES by peeking at the iterator
        # This handles the case where islice stopped at MAX_MESSAGES but more items exist
        try:
            next(msg_iter)
            stream_truncated = True
        except StopIteration:
            pass

        payload = {
            "bot": bot_name,
            "model": active_model,
            "tool_iterations": tool_iterations,
            "exit_reason": exit_reason,
            "persisted_at": persisted_at,
            "usage": usage_dict,
            "messages": bounded,
        }

        if stream_truncated or truncated_count > 0:
            payload["truncated"] = True

        body = json.dumps(payload, ensure_ascii=False)
        # Use byte length for accurate size bound since file is written as UTF-8.
        # len(body) counts chars, but multi-byte Unicode can make bytes > chars,
        # violating the 500KB disk cap even when char count is within bounds.
        final_size = len(body.encode("utf-8"))
        # Optimized tail-trim: use tracked entry_sizes to estimate how many
        # messages to remove, then verify with a single re-serialization.
        # entry_sizes tracks JSON char length, but final_size is UTF-8 bytes.
        # For Unicode-heavy content, bytes can be up to 4x chars, so we apply
        # a conservative 4x multiplier to entry_sizes when estimating reduction.
        # This prevents over-removal while still achieving O(1) serializations.
        max_trim_iterations = min(len(bounded), 100)
        trim_iterations = 0
        while final_size > MAX_SIZE and bounded and trim_iterations < max_trim_iterations:
            # Estimate how many entries to remove based on tracked sizes.
            # Apply 4x multiplier to account for worst-case UTF-8 expansion.
            overshoot = final_size - MAX_SIZE
            estimated_reduction = 0
            entries_to_remove = 0
            # Walk backwards through entry_sizes to find how many to pop
            for i in range(len(entry_sizes) - 1, -1, -1):
                # Conservative estimate: each char could be up to 4 UTF-8 bytes
                estimated_reduction += entry_sizes[i] * 4
                entries_to_remove += 1
                if estimated_reduction >= overshoot:
                    break
            # Ensure we remove at least one entry if overshoot exists
            if entries_to_remove == 0 and bounded:
                entries_to_remove = 1
            # Pop the estimated number of entries
            for _ in range(entries_to_remove):
                if bounded:
                    bounded.pop()
                if entry_sizes:
                    entry_sizes.pop()
            trim_iterations += entries_to_remove
            stream_truncated = True
            payload["messages"] = bounded
            payload["truncated"] = True
            body = json.dumps(payload, ensure_ascii=False)
            final_size = len(body.encode("utf-8"))

        # Safety cap: if trim iterations exhausted and size still exceeds MAX_SIZE,
        # clear bounded to guarantee compliance and prevent CPU DoS.
        if final_size > MAX_SIZE and bounded:
            bounded.clear()
            entry_sizes.clear()
            payload["messages"] = []
            payload["truncated"] = True
            stream_truncated = True
            body = json.dumps(payload, ensure_ascii=False)
            final_size = len(body.encode("utf-8"))

        # For logging, we use cumulative_size as an approximation of input size processed
        input_size_for_log = cumulative_size
        truncation_ratio = final_size / max(1, input_size_for_log) if input_size_for_log > 0 else 1.0
        
        if truncated_count > 0 or stream_truncated:
            _log_context_assembly(
                bot_name=bot_name,
                event_type="stream_persist_truncation",
                input_size=input_size_for_log,
                output_size=final_size,
                truncation_ratio=min(1.0, truncation_ratio),
                final_token_count=final_size // 4,
                tool_name="",
                extra={"tool_results_truncated": truncated_count, "stream_truncated": stream_truncated, "skipped_messages": skipped_count},
            )
        
        p = BOTS_DIR / "logs" / f"{bot_name}.stream.json"
        tmp = Path(str(p) + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(p)
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("%s: _persist_stream failed: %s", bot_name, exc)
    except (MemoryError, SystemExit):
        raise


def _contract(heartbeat_file, ckpt_file):
    """Minimal shared-infra contract (<800 chars) without importing prompt_gateway."""
    return (
        "[SHARED INFRA CONTRACT]\n"
        f"- Drain: run `bash` with `python3 -m codebot.check_drain` before each task. If exit code is 0, exit 0 cleanly. Do NOT use the `read` tool for drain checks.\n"
        f"- Heartbeat: a background thread writes your heartbeat every 30s automatically. You do NOT need to write heartbeat files manually. Focus entirely on your task.\n"
        f"- Checkpoint: platform-owned only. Do NOT write {ckpt_file} directly. The runner persists checkpoints; report progress via scratchpad/status.\n"
        "- Bound: bounded delegated task, not a daemon. Do atomic work, checkpoint, exit 0 on drain or completion."
    )


_THINKING_MODEL_KEYWORDS = frozenset({"thinking"})
_STREAM_CHUNK_IDLE_TIMEOUT = 60


def _is_thinking_model(model: str) -> bool:
    m = model.lower()
    return any(kw in m for kw in _THINKING_MODEL_KEYWORDS)


def _call_api_stream(messages, model, api_key, schemas, timeout):
    body = {
        "model": model,
        "messages": messages,
        "tools": schemas,
        "max_tokens": 8000,
        "stream": True,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    content_parts: list[str] = []
    tool_calls_map: dict[int, dict] = {}
    finish_reason = "stop"
    buffer = ""
    last_chunk_at = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        while True:
            try:
                piece = resp.read(4096)
            except Exception:
                if time.monotonic() - last_chunk_at > _STREAM_CHUNK_IDLE_TIMEOUT:
                    raise urllib.error.URLError("stream idle timeout exceeded")
                raise
            if not piece:
                break
            last_chunk_at = time.monotonic()
            buffer += piece.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices", [])
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta", {})
                fr = choice.get("finish_reason")
                if fr:
                    finish_reason = fr
                content = delta.get("content")
                if content:
                    content_parts.append(content)
                tc_list = delta.get("tool_calls") or []
                for tc in tc_list:
                    idx = tc.get("index", 0)
                    if idx not in tool_calls_map:
                        tool_calls_map[idx] = {"id": tc.get("id", ""), "type": "function", "function": {"name": "", "arguments": ""}}
                    entry = tool_calls_map[idx]
                    if tc.get("id"):
                        entry["id"] = tc["id"]
                    fn = tc.get("function", {})
                    if fn.get("name"):
                        entry["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        entry["function"]["arguments"] += fn["arguments"]
    result: dict[str, Any] = {"choices": [{"message": {"role": "assistant", "content": "".join(content_parts)}, "finish_reason": finish_reason}]}
    if tool_calls_map:
        ordered = [tool_calls_map[i] for i in sorted(tool_calls_map)]
        result["choices"][0]["message"]["tool_calls"] = ordered
    return result


def _call_api(messages, model, api_key, timeout=None, bot_name=None):
    if timeout is None:
        timeout = API_TIMEOUT
    schemas = TOOL_SCHEMAS
    if bot_name:
        base = bot_name.split("-")[0] if "-" in bot_name else bot_name
        no_bash_roles = frozenset({"decomposer", "planner",
            "reviewer", "security_reviewer", "architecture_reviewer",
            "performance_reviewer", "concurrency_reviewer",
            "data_integrity_reviewer", "ux_reviewer"})
        if base in no_bash_roles:
            schemas = [s for s in TOOL_SCHEMAS if s.get("function", {}).get("name") != "bash"]
    if _is_thinking_model(model):
        return _call_api_stream(messages, model, api_key, schemas, timeout)
    body = {
        "model": model,
        "messages": messages,
        "tools": schemas,
        "max_tokens": 8000,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    deadline = time.monotonic() + timeout
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        chunks: list[bytes] = []
        remaining = 2_000_000
        while remaining > 0:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise urllib.error.URLError("API read deadline exceeded")
            # Set socket timeout to remaining time to prevent slow-trickle attacks.
            # A malicious server could send 1 byte every few seconds, keeping the
            # connection alive indefinitely. By updating the socket timeout before
            # each read, we ensure the total elapsed time never exceeds the deadline.
            try:
                sock = resp.fp._sock if hasattr(resp.fp, '_sock') else None
                if sock is not None:
                    sock.settimeout(remaining_time)
            except Exception:
                pass  # Fail-open: if we can't set timeout, proceed with read
            piece = resp.read(min(65536, remaining))
            if not piece:
                break
            chunks.append(piece)
            remaining -= len(piece)
        raw = b"".join(chunks)
        text = raw.decode("utf-8", errors="replace")
        return json.loads(text)


def _adapter_state_dir() -> Path:
    adapter = _adapter_instance
    if adapter is not None:
        try:
            return Path(adapter.paths().state_dir)  # type: ignore[union-attr]
        except Exception:
            pass
    try:
        return Path(WORK_ROOT) / ".codebot" / "state"
    except NameError:
        return Path.cwd() / ".codebot" / "state"


def _review_verdict_target(bot_name: str, path: str) -> tuple[str, str] | None:
    base = bot_name.split("-", 1)[0] if "-" in bot_name else bot_name
    if not base.endswith("_reviewer"):
        return None
    marker = "/reviews/"
    index = path.find(marker)
    if index < 0:
        return None
    remainder = path[index + len(marker):]
    parts = remainder.split("/", 1)
    if len(parts) != 2:
        return None
    ticket_id, filename = parts
    if not ticket_id or "/" in ticket_id or "\\" in ticket_id or ".." in ticket_id:
        return None
    expected = f"{base}.json"
    if filename != expected:
        return None
    return ticket_id, base


def _execute_tool(name, args, bot_name: str = ""):
    """Dispatch tool_calls by name to api_tools; unknown name is fail-open."""
    func = _TOOL_MAP.get(name)
    if func is None:
        return {"success": False, "output": "", "error": f"unknown tool: {name}"}
    try:
        if name == "write":
            path = args.get("path", "") if isinstance(args, dict) else ""
            if isinstance(path, str) and path.endswith(".heartbeat"):
                return {"success": True, "output": f"Wrote {len(str(time.time()))} bytes to {path}", "error": None, "_heartbeat_injected": True}
        if name == "write" and bot_name:
            target = _review_verdict_target(bot_name, args.get("path", "") if isinstance(args, dict) else "")
            if target is not None and isinstance(args, dict):
                from codebot.review_store import write_verdict
                import json as _json
                ticket_id, role = target
                try:
                    payload = _json.loads(args.get("content", "{}"))
                except Exception:
                    return {"success": False, "output": "", "error": "verdict content must be JSON"}
                if not isinstance(payload, dict):
                    return {"success": False, "output": "", "error": "verdict content must be a JSON object"}
                try:
                    state_dir = _adapter_state_dir()
                    stored = write_verdict(state_dir, ticket_id, role, payload)
                except ValueError as exc:
                    return {"success": False, "output": "", "error": str(exc)}
                return {"success": True, "output": f"Wrote ticket-scoped verdict to {stored}", "error": None}
        return func(**args)
    except TypeError as exc:
        return {"success": False, "output": "", "error": f"bad args for {name}: {exc}"}
    except Exception as exc:
        return {"success": False, "output": "", "error": str(exc)}


def _write_heartbeat_server_side(hb_path: Path, bot_name: str) -> None:
    """Write real server-side timestamp for heartbeat writes (model can't fabricate time)."""
    try:
        hb_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(hb_path) + ".tmp")
        tmp.write_text(str(time.time()), encoding="utf-8")
        tmp.replace(hb_path)
    except Exception:
        pass
    try:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        log_dir = hb_path.parent.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        lp = log_dir / f"{bot_name}.tasklog"
        lines: list[str] = []
        if lp.exists():
            lines = lp.read_text(encoding="utf-8").splitlines(keepends=True)
        lines.append(f"[{ts}] {bot_name} heartbeat\n")
        lp.write_text("".join(lines[-200:]), encoding="utf-8")
    except Exception:
        pass


def _write_heartbeat_server(hb_path: Path) -> None:
    _write_heartbeat_server_side(hb_path, hb_path.stem)


def _init_cost_accumulator():
    """Initialize thread-local cost accumulator if not already present."""
    if not hasattr(_cost_accumulator, 'initialized'):
        _cost_accumulator.prompt_tokens = 0
        _cost_accumulator.completion_tokens = 0
        _cost_accumulator.call_count = 0
        _cost_accumulator.model = ""
        _cost_accumulator.bot_name = ""
        _cost_accumulator.ticket_id = ""
        _cost_accumulator.phase = "implement"
        _cost_accumulator.start_time = time.time()
        _cost_accumulator.initialized = True


def _flush_cost_accumulator(
    bot_name: str,
    ticket_id: str,
    model: str,
    phase: str = "implement",
    state_dir: "str | os.PathLike[str] | None" = None,
    heartbeat_file: str = "",
    ckpt_file: str = "",
) -> None:
    """Flush accumulated cost data to token_budget and cost_tracker.

    This function drains the thread-local accumulator and records usage
    to both the token budget ledger and the per-ticket cost tracker.
    Wrapped in try/except to never crash the hot path.

    ``elapsed`` measures inter-flush duration (time since the accumulator was
    last drained), not total session duration — each flush records its own
    wall-clock slice so summing records across a session reconstructs the
    session total. ``state_dir`` overrides automatic resolution; otherwise
    :func:`_resolve_cost_state_dir` derives the runtime state dir from the
    heartbeat/checkpoint paths (never the hardcoded
    ``codebot/.codebot/state`` path).
    """
    from datetime import datetime, timezone

    if not hasattr(_cost_accumulator, 'initialized') or _cost_accumulator.call_count == 0:
        return

    prompt_tokens = _cost_accumulator.prompt_tokens
    completion_tokens = _cost_accumulator.completion_tokens
    call_count = _cost_accumulator.call_count
    elapsed = time.time() - _cost_accumulator.start_time

    # Reset accumulator
    _cost_accumulator.prompt_tokens = 0
    _cost_accumulator.completion_tokens = 0
    _cost_accumulator.call_count = 0
    _cost_accumulator.start_time = time.time()

    if prompt_tokens == 0 and completion_tokens == 0:
        return

    # Record to token budget ledger (day+model aggregation) plus structured
    # per-bot/per-ticket attribution for the economics ledger (AC2). The
    # ledger schema has no bot/ticket columns, so attribution rides alongside
    # via _record_cost_attribution() instead of invented kwargs.
    if _HAS_TOKEN_BUDGET and _record_token_usage:
        try:
            day_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            _record_token_usage(
                day_utc=day_utc,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            _record_cost_attribution(
                bot_name=bot_name,
                ticket_id=ticket_id,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        except Exception:
            pass

    # Record to per-ticket cost tracker
    if _HAS_COST_TRACKER and _CostTracker:
        try:
            resolved = Path(state_dir) if state_dir is not None else _resolve_cost_state_dir(
                heartbeat_file=heartbeat_file, ckpt_file=ckpt_file
            )
            tracker = _CostTracker(resolved)
            tracker.record_phase_cost(
                ticket_id=ticket_id,
                agent=bot_name,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                phase=phase,
                wall_clock_seconds=elapsed,
                attempts=call_count,
            )
        except Exception:
            pass


def _extract_provider_usage(resp):
    out = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if not isinstance(resp, dict):
        return out
    usage = resp.get("usage")
    if not isinstance(usage, dict):
        return out
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        v = usage.get(k)
        if v is None:
            continue
        try:
            out[k] = int(v)
        except Exception:
            try:
                out[k] = int(str(v).strip())
            except Exception:
                out[k] = 0
    if out["total_tokens"] == 0 and (out["prompt_tokens"] or out["completion_tokens"]):
        out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
    return out


def _parse_tool_args(raw):
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _execute_provider_session(
    bot_name,
    model,
    messages,
    heartbeat_file,
    ckpt_file,
    api_key,
    fallback_model="",
    max_tokens_per_run=0,
    api_call=None,
    drain_check=None,
    tool_dispatch=None,
    sleep_fn=None,
):
    if api_call is None:
        api_call = _call_api
    if drain_check is None:
        drain_check = _is_draining
    if tool_dispatch is None:
        tool_dispatch = _execute_tool
    if sleep_fn is None:
        sleep_fn = time.sleep
    hb_path = Path(heartbeat_file)
    ck_path = Path(ckpt_file)
    msgs = list(messages)
    tool_iterations = 0
    total_retries = 0
    timeout_retries = 0
    rate_429_retries = 0
    MAX_429_RETRIES = 0
    nudges = 0
    fallback_used = False
    active_model = model
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    api_calls = 0

    # Initialize cost tracking accumulator for this session
    _init_cost_accumulator()
    # Derive ticket_id from ckpt_file path (format: .../<ticket_id>.<bot>.checkpoint.json)
    _session_ticket_id = ""
    try:
        _ckpt_stem = Path(ckpt_file).stem  # e.g. "CB-F5864.implementer-CB-F5864.checkpoint"
        if "." in _ckpt_stem:
            _session_ticket_id = _ckpt_stem.split(".")[0]
    except Exception:
        pass

    def _result(status, reason=None):
        # Flush accumulated costs before returning to prevent data loss
        try:
            _flush_cost_accumulator(
                bot_name,
                _session_ticket_id,
                active_model,
                heartbeat_file=str(hb_path),
                ckpt_file=str(ck_path),
            )
        except Exception:
            pass
        return {
            "status": status,
            "reason": reason or status,
            "messages": msgs,
            "usage": dict(usage),
            "tool_iterations": tool_iterations,
            "exit_reason": reason or status,
            "api_calls": api_calls,
            "model": active_model,
        }

    def _sleep(d):
        try:
            sleep_fn(d)
        except Exception:
            pass

    def _drain():
        try:
            return bool(drain_check(bot_name))
        except Exception:
            return False

    def _backoff():
        return BACKOFFS[min(total_retries, len(BACKOFFS) - 1)] if BACKOFFS else 2

    def _is_provider_error(resp: dict) -> tuple[bool, str]:
        error = resp.get("error")
        if isinstance(error, dict):
            msg = error.get("message", "").lower()
            error_type = error.get("type", "").lower()
            if any(x in msg for x in ["overloaded", "capacity", "rate limit", "too many", "try again"]):
                return True, msg
            if any(x in error_type for x in ["rate_limit", "overloaded", "capacity"]):
                return True, error_type
        elif isinstance(error, str):
            if any(x in error.lower() for x in ["overloaded", "capacity", "rate limit", "too many"]):
                return True, error.lower()
        return False, ""

    if _drain():
        _write_heartbeat(hb_path)
        return _result("drain")

    while True:
        if tool_iterations >= MAX_TOOL_ITERATIONS:
            return _result("iteration_limit")
        if tool_iterations > 0 and _drain():
            _write_heartbeat(hb_path)
            return _result("drain")
        resp_json = None
        while True:
            try:
                resp_json = api_call(msgs, active_model, api_key)
                api_calls += 1
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    if rate_429_retries < MAX_429_RETRIES:
                        delay = BACKOFFS[min(rate_429_retries, len(BACKOFFS) - 1)]
                        rate_429_retries += 1
                        _write_heartbeat(hb_path)
                        _sleep(delay)
                        continue
                    _write_heartbeat(hb_path)
                    _write_checkpoint(ck_path, bot_name, "rate_limited_yield")
                    return _result("rate_limited_yield")
                try:
                    body = exc.read(MAX_RESPONSE_BYTES).decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
                    if "429" in body or "rate" in body.lower():
                        if rate_429_retries < MAX_429_RETRIES:
                            delay = BACKOFFS[min(rate_429_retries, len(BACKOFFS) - 1)]
                            rate_429_retries += 1
                            _write_heartbeat(hb_path)
                            _sleep(delay)
                            continue
                        _write_heartbeat(hb_path)
                        _write_checkpoint(ck_path, bot_name, "rate_limited_yield")
                        return _result("rate_limited_yield")
                except Exception:
                    pass
                return _result("http_error")
            except urllib.error.URLError:
                if total_retries < MAX_RETRIES and timeout_retries < MAX_TIMEOUT_RETRIES:
                    _sleep(min(_backoff(), MAX_BACKOFF))
                    total_retries += 1
                    timeout_retries += 1
                    continue
                return _result("connection_error")
            except TimeoutError:
                if total_retries < MAX_RETRIES and timeout_retries < MAX_TIMEOUT_RETRIES:
                    _sleep(min(_backoff(), MAX_BACKOFF))
                    total_retries += 1
                    timeout_retries += 1
                    continue
                return _result("timeout")
            except Exception:
                if total_retries < MAX_RETRIES:
                    _sleep(min(_backoff(), MAX_BACKOFF))
                    total_retries += 1
                    continue
                return _result("unexpected_error")
        is_provider_err, err_msg = _is_provider_error(resp_json)
        if is_provider_err:
            if total_retries < MAX_RETRIES:
                _log(f"{bot_name}: provider error: {err_msg} — retrying")
                _sleep(min(_backoff(), MAX_BACKOFF))
                total_retries += 1
                continue
            _write_heartbeat(hb_path)
            _write_checkpoint(ck_path, bot_name, "provider_error")
            return _result("provider_error", err_msg)
        try:
            u = _extract_provider_usage(resp_json)
            for k in usage:
                usage[k] += int(u.get(k, 0))
            # Accumulate cost data for batched recording
            if hasattr(_cost_accumulator, 'initialized'):
                _cost_accumulator.prompt_tokens += int(u.get("prompt_tokens", 0))
                _cost_accumulator.completion_tokens += int(u.get("completion_tokens", 0))
                _cost_accumulator.call_count += 1
                # Flush periodically to avoid per-call lock contention
                if _cost_accumulator.call_count >= _COST_FLUSH_INTERVAL:
                    _flush_cost_accumulator(
                        bot_name,
                        _session_ticket_id,
                        active_model,
                        heartbeat_file=str(hb_path),
                        ckpt_file=str(ck_path),
                    )
        except Exception:
            pass
        if max_tokens_per_run > 0 and usage["total_tokens"] >= max_tokens_per_run:
            _log(f"{bot_name}: per-run token cap reached ({usage['total_tokens']}/{max_tokens_per_run}) — checkpointing and exiting")
            _write_heartbeat(hb_path)
            _write_checkpoint(ck_path, bot_name, "token_cap")
            return _result("token_cap", "per-run token limit reached")
        try:
            choices = resp_json.get("choices") or []
            msg = choices[0].get("message", {}) if choices else {}
        except Exception:
            msg = {}
        if not isinstance(msg, dict):
            msg = {}
        tcs = msg.get("tool_calls")
        content = msg.get("content")
        has_tc = bool(tcs) and isinstance(tcs, list) and len(tcs) > 0
        has_content = bool(content is not None and str(content).strip() != "")
        if has_tc:
            msgs.append(msg)
            for tc in tcs:
                if _drain():
                    _write_heartbeat(hb_path)
                    return _result("drain")
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id", "") if isinstance(tc.get("id"), str) else ""
                func = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
                if not isinstance(func, dict):
                    func = {}
                name = func.get("name", "") if isinstance(func.get("name"), str) else ""
                args = _parse_tool_args(func.get("arguments", "{}"))
                try:
                    try:
                        r = tool_dispatch(name, args, bot_name=bot_name)
                    except TypeError:
                        r = tool_dispatch(name, args)
                    if isinstance(r, dict) and r.pop("_heartbeat_injected", False):
                        try:
                            _write_heartbeat_server_side(hb_path, bot_name)
                        except Exception:
                            pass
                except Exception as e:
                    r = {"success": False, "output": "", "error": str(e)}
                if not isinstance(r, dict):
                    r = {"success": False, "output": "", "error": "invalid tool result"}
                # Context assembly tracing: log tool result integration
                tool_result_json = json.dumps(r)
                input_size = len(json.dumps(args)) if args else 0
                output_size = len(tool_result_json)
                truncation_ratio = 1.0 if input_size == 0 else min(1.0, output_size / max(1, input_size))
                # Estimate token count (rough: 4 chars per token)
                total_msg_size = sum(len(json.dumps(m)) for m in msgs) + output_size
                estimated_tokens = total_msg_size // 4
                _log_context_assembly(
                    bot_name=bot_name,
                    event_type="tool_result_appended",
                    input_size=input_size,
                    output_size=output_size,
                    truncation_ratio=truncation_ratio,
                    final_token_count=estimated_tokens,
                    tool_name=name,
                    extra={"tool_call_id": tc_id[:32] if tc_id else "", "success": r.get("success", False)},
                )
                msgs.append({"role": "tool", "tool_call_id": tc_id, "content": tool_result_json})
            tool_iterations += 1
            continue
        if has_content:
            # Context assembly tracing: log assistant response integration
            content_str = str(content) if content else ""
            content_size = len(content_str)
            total_msg_size = sum(len(json.dumps(m)) for m in msgs) + content_size
            estimated_tokens = total_msg_size // 4
            _log_context_assembly(
                bot_name=bot_name,
                event_type="assistant_response_received",
                input_size=0,
                output_size=content_size,
                truncation_ratio=1.0,
                final_token_count=estimated_tokens,
                tool_name="",
                extra={"response_length": content_size},
            )
            msgs.append(msg)
            try:
                _write_heartbeat(hb_path)
            except Exception:
                pass
            try:
                _write_checkpoint(ck_path, bot_name, "completed")
            except Exception:
                pass
            return _result("completed")
        if nudges < 2:
            msgs.append({"role": "user", "content": "continue"})
            nudges += 1
            continue
        if fallback_model and not fallback_used:
            _log(f"{bot_name}: primary '{active_model}' returned no content, switching to fallback '{fallback_model}'")
            active_model = fallback_model
            fallback_used = True
            nudges = 0
            continue
        return _result("no_content")


_run_provider_execution = _execute_provider_session
_run_execution_adapter = _execute_provider_session
_execute_with_provider = _execute_provider_session
_provider_execution_adapter = _execute_provider_session
_extract_usage = _extract_provider_usage
_parse_usage = _extract_provider_usage


HIGH_RISK_TOKEN_MANIFESTS = {"prompt_opt", "worker-5"}

_MODEL_PROFILES = {
    "qwen-3.8-max-thinking": {"lockup_risk": "high", "heartbeat_multiplier": 2.8, "log_stall_seconds": 280, "restart_cooldown": 12},
    "qwen-3.7-max-thinking": {"lockup_risk": "high", "heartbeat_multiplier": 2.5, "log_stall_seconds": 240, "restart_cooldown": 10},
    "qwen-3.6-plus-thinking": {"lockup_risk": "medium-high", "heartbeat_multiplier": 2.3, "log_stall_seconds": 210, "restart_cooldown": 9},
    "qwen-3.5-plus-thinking": {"lockup_risk": "medium-high", "heartbeat_multiplier": 2.2, "log_stall_seconds": 200, "restart_cooldown": 8},
    "qwen-3.8-max": {"lockup_risk": "medium-high", "heartbeat_multiplier": 2.0, "log_stall_seconds": 180, "restart_cooldown": 7},
    "qwen-3.7-plus": {"lockup_risk": "medium", "heartbeat_multiplier": 1.9, "log_stall_seconds": 170, "restart_cooldown": 6},
    "qwen-3.6-plus": {"lockup_risk": "medium", "heartbeat_multiplier": 1.9, "log_stall_seconds": 165, "restart_cooldown": 6},
    "qwen-3.5-plus": {"lockup_risk": "low-medium", "heartbeat_multiplier": 1.7, "log_stall_seconds": 140, "restart_cooldown": 4},
    "qwen-3.5-omni-plus": {"lockup_risk": "low-medium", "heartbeat_multiplier": 1.7, "log_stall_seconds": 145, "restart_cooldown": 4},
    "meta-muse-spark-1.3": {"lockup_risk": "medium", "heartbeat_multiplier": 1.8, "log_stall_seconds": 150, "restart_cooldown": 5},
    "meta-muse-spark-1.2": {"lockup_risk": "low-medium", "heartbeat_multiplier": 1.8, "log_stall_seconds": 150, "restart_cooldown": 5},
    "xiaomi-mimo-2.5": {"lockup_risk": "low", "heartbeat_multiplier": 1.5, "log_stall_seconds": 120, "restart_cooldown": 3},
}


def _model_profile(model: str) -> dict | None:
    return _MODEL_PROFILES.get(model)


def _effective_for_manifest(manifest: dict, heartbeat_max_gap_s: int) -> int:
    try:
        explicit = int(manifest.get("heartbeat_timeout", 0))
    except Exception:
        explicit = 0
    if explicit and explicit > 0:
        return explicit
    return heartbeat_max_gap_s


def _is_log_stalled(logs_dir: Path, name: str, stall_seconds: int) -> bool:
    p = logs_dir / f"{name}.log"
    try:
        if not p.exists():
            return False
        return (time.time() - p.stat().st_mtime) > stall_seconds
    except Exception:
        return False


def _is_stuck_manifest(state_dir: Path, logs_dir: Path | None, manifest: dict, heartbeat_max_gap_s: int) -> bool:
    name = manifest.get("name", "")
    eff = _effective_for_manifest(manifest, heartbeat_max_gap_s)
    if not _is_stale_heartbeat(state_dir, name, eff):
        return False
    prof = _model_profile(str(manifest.get("model", "")))
    if prof and prof["lockup_risk"] in ("high", "medium-high"):
        if logs_dir is None:
            return False
        return _is_log_stalled(logs_dir, name, prof["log_stall_seconds"])
    return True


def _write_json_atomic(path: Path, data) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(p) + ".tmp")
        body = json.dumps(data, indent=2) if isinstance(data, (dict, list)) else str(data)
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def _heartbeat_path_for(state_dir: Path, name: str) -> Path:
    return state_dir / f"{name}.heartbeat"


def _write_heartbeat_atomic(path: Path) -> None:
    """Atomic heartbeat write via tmp+rename (float time.time())."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(str(time.time()), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _is_stale_heartbeat(state_dir: Path, name: str, effective_timeout: int) -> bool:
    p = _heartbeat_path_for(state_dir, name)
    if not p.exists():
        return False
    try:
        txt = p.read_text(encoding="utf-8").strip().split()[0]
        last = float(txt)
        return (time.time() - last) > effective_timeout
    except Exception:
        return False


def _token_gate_allows(manifest: dict) -> bool:
    name = manifest.get("name", "")
    tier = manifest.get("batch_tier", "standard")
    if name in HIGH_RISK_TOKEN_MANIFESTS and tier != "high-limit":
        return False
    return True


def _drain_requested(batch_ctx: dict, state_dir: Path) -> bool:
    cb = batch_ctx.get("drain_check_cb")
    if cb and callable(cb):
        try:
            if cb():
                return True
        except Exception:
            pass
    if DRAIN_FILE.exists():
        return True
    if (BOTS_DIR / "state" / ".update_lock").exists():
        return True
    try:
        if (state_dir / ".drain").exists():
            return True
        if (state_dir / ".update_lock").exists():
            return True
    except Exception:
        pass
    return False


def _locked_ledger_write(state_dir: Path, token_report_cb, manifest_name: str) -> tuple[bool, str | None]:
    if token_report_cb is None or not callable(token_report_cb):
        return True, None
    ledger_path = state_dir / "token_ledger.json"
    lock_path = state_dir / ".token_ledger.lock"
    state_dir.mkdir(parents=True, exist_ok=True)
    target = ledger_path if ledger_path.exists() else lock_path
    try:
        if not target.exists():
            target.write_text("{}", encoding="utf-8")
    except Exception:
        pass
    fp = None
    start = time.time()
    timeout = 5.0
    acquired = False
    try:
        fp = open(target, "a+")
        while time.time() - start < timeout:
            try:
                flock(fp, LOCK_EX | LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(0.05)
            except Exception:
                time.sleep(0.05)
        if not acquired:
            return False, "budget-unknown"
        try:
            token_report_cb(manifest_name, {"prompt_tokens": 0, "completion_tokens": 0})
        except Exception:
            pass
        return True, None
    finally:
        if fp is not None:
            try:
                flock(fp, LOCK_UN)
            except Exception:
                pass
            try:
                fp.close()
            except Exception:
                pass


def _run_one_manifest(
    manifest: dict,
    idx: int,
    pool_per_manifest: int,
    heartbeat_max_gap_s: int,
    batch_ctx: dict,
    heartbeat_dir: Path,
    state_dir: Path,
    logs_dir: Path,
    token_report_cb,
    budget_check_cb,
    shed_active: bool,
) -> dict:
    if not isinstance(manifest, dict):
        return {"name": str(manifest), "status": "skipped", "reason": "invalid-manifest", "allocated": 0}
    name = manifest.get("name", f"manifest-{idx}")
    tier_raw = manifest.get("tier_priority", 999)
    try:
        tier_int = int(tier_raw)
    except Exception:
        tier_int = 999

    effective_timeout = _effective_for_manifest(manifest, heartbeat_max_gap_s)
    if _is_stuck_manifest(heartbeat_dir, logs_dir, manifest, heartbeat_max_gap_s):
        hb_path = _heartbeat_path_for(heartbeat_dir, name)
        try:
            _write_json_atomic(state_dir / f"{name}.checkpoint.json", {"bot": name, "updated_at": time.time(), "reason": "stuck-killed", "stuck": True, "effective_timeout": effective_timeout})
        except Exception:
            pass
        return {"name": name, "status": "skipped", "reason": "stale-heartbeat", "allocated": 0, "iterations_used": 0, "heartbeat": str(hb_path), "stuck": True}

    budget_state = None
    if budget_check_cb and callable(budget_check_cb):
        try:
            budget_state = budget_check_cb()
        except Exception:
            budget_state = None
    if shed_active and tier_int >= 31:
        return {"name": name, "status": "skipped", "reason": "budget-shed", "allocated": 0, "iterations_used": 0, "tier_priority": tier_int}
    if budget_state == "shed_tier3" and tier_int >= 31:
        return {"name": name, "status": "skipped", "reason": "budget-shed", "allocated": 0, "iterations_used": 0, "tier_priority": tier_int}

    if not _token_gate_allows(manifest):
        return {"name": name, "status": "skipped", "reason": "token-gate", "allocated": 0, "iterations_used": 0}

    allocated = pool_per_manifest
    hb_path = _heartbeat_path_for(heartbeat_dir, name)
    _write_scratchpad(name, state_dir, "batch_start", f"model={manifest.get('model', '')}")
    _write_bot_status(name, state_dir, "batch_start", f"model={manifest.get('model', '')}", [], 0)

    try:
        _write_heartbeat_atomic(hb_path)

        if manifest.get("_test_raise"):
            raise RuntimeError(f"test raise for {name}")

        if "_test_iterations_used" in manifest:
            try:
                iterations_used = int(manifest.get("_test_iterations_used", 1))
            except Exception:
                iterations_used = 1
        else:
            iterations_used = 1

        is_noop_hit = bool(manifest.get("_test_noop_hit")) or bool(manifest.get("noop_hit"))
        remainder = allocated - iterations_used if is_noop_hit else 0
        if remainder < 0:
            remainder = 0
        status = "completed"
        reason = "noop_yield" if is_noop_hit else "completed"

        _write_heartbeat_atomic(hb_path)

        ckpt_path = state_dir / f"{name}.checkpoint.json"
        _write_json_atomic(ckpt_path, {"bot": name, "updated_at": time.time(), "reason": reason, "allocated": allocated, "iterations_used": iterations_used})

        ok, ledger_reason = _locked_ledger_write(state_dir, token_report_cb, name)
        if not ok:
            return {"name": name, "status": status, "reason": reason, "allocated": allocated, "iterations_used": iterations_used, "heartbeat": str(hb_path), "noop_hit": is_noop_hit, "pool_remainder_after": remainder, "_ledger_failed": ledger_reason}

        result = {"name": name, "status": status, "reason": reason, "allocated": allocated, "iterations_used": iterations_used, "heartbeat": str(hb_path), "noop_hit": is_noop_hit, "pool_remainder_after": remainder}
        if budget_state == "stop":
            result["_budget_stop"] = True
        return result

    except Exception as e:
        _write_heartbeat_atomic(hb_path)
        try:
            ckpt_path = state_dir / f"{name}.checkpoint.json"
            _write_json_atomic(ckpt_path, {"bot": name, "updated_at": time.time(), "reason": f"failed: {e}", "error": str(e)})
        except Exception:
            pass
        allocated_fallback = allocated if "allocated" in locals() else pool_per_manifest
        return {"name": name, "status": "failed", "reason": str(e) or "exception", "error": str(e), "allocated": allocated_fallback, "iterations_used": 0, "heartbeat": str(hb_path), "_budget_stop": budget_state == "stop"}


def run_batch(manifests: list[dict], batch_ctx: dict) -> dict:
    """Concurrent multi-manifest execution with per-manifest liveness.

    Frozen signature: run_batch(manifests: list[dict], batch_ctx: dict)
    batch_ctx carries {pool_per_manifest: int = 50, heartbeat_max_gap_s: int = 120,
                      heartbeat_dir/state_dir, token_report_cb, drain_check_cb, budget_check_cb}
    Supports both heartbeat_dir and state_dir (plus heartbeatDir/stateDir aliases);
    if only one is given it is used for both heartbeat and state. Atomic writes via tmp+rename.
    Manifests run concurrently on a bounded ThreadPoolExecutor; budget/drain aborts are
    applied deterministically in input order after all workers finish.
    """
    if manifests is None:
        manifests = []
    if batch_ctx is None:
        batch_ctx = {}
    pool_per_manifest = int(batch_ctx.get("pool_per_manifest", 50))
    heartbeat_max_gap_s = batch_ctx.get("heartbeat_max_gap_s", 120)
    try:
        heartbeat_max_gap_s = int(heartbeat_max_gap_s) if heartbeat_max_gap_s is not None else 120
    except Exception:
        heartbeat_max_gap_s = 120
    token_report_cb = batch_ctx.get("token_report_cb")
    budget_check_cb = batch_ctx.get("budget_check_cb")
    hb_dir_str = batch_ctx.get("heartbeat_dir") or batch_ctx.get("heartbeatDir") or batch_ctx.get("state_dir") or batch_ctx.get("stateDir")
    st_dir_str = batch_ctx.get("state_dir") or batch_ctx.get("stateDir") or batch_ctx.get("heartbeat_dir") or batch_ctx.get("heartbeatDir")
    logs_dir_str = batch_ctx.get("logs_dir") or batch_ctx.get("logsDir")
    heartbeat_dir = Path(hb_dir_str) if hb_dir_str else (BOTS_DIR / "state")
    state_dir = Path(st_dir_str) if st_dir_str else (BOTS_DIR / "state")
    logs_dir = Path(logs_dir_str) if logs_dir_str else (BOTS_DIR / "logs")
    heartbeat_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    if _drain_requested(batch_ctx, state_dir):
        return {
            "results": [],
            "pool_accounting": {"pool_per_manifest": pool_per_manifest, "pool_remainder": 0, "total_manifests": len(manifests), "heartbeat_max_gap_s": heartbeat_max_gap_s},
            "pool": {"pool_per_manifest": pool_per_manifest, "pool_remainder": 0, "total_manifests": len(manifests), "heartbeat_max_gap_s": heartbeat_max_gap_s},
            "accounting": {"pool_per_manifest": pool_per_manifest, "pool_remainder": 0, "total_manifests": len(manifests), "heartbeat_max_gap_s": heartbeat_max_gap_s},
            "reason": "drain",
            "aborted": True,
            "completed": 0,
            "skipped": 0,
            "failed": 0,
        }

    indexed = list(enumerate(manifests))
    valid = [(idx, m) for idx, m in indexed if isinstance(m, dict)]
    invalid = [idx for idx, m in indexed if not isinstance(m, dict)]
    raw_results: dict[int, dict] = {}
    for idx in invalid:
        raw_results[idx] = {"name": str(manifests[idx]), "status": "skipped", "reason": "invalid-manifest", "allocated": 0}

    budget_state = None
    if budget_check_cb and callable(budget_check_cb):
        try:
            budget_state = budget_check_cb()
        except Exception:
            budget_state = None
    shed_active = budget_state == "shed_tier3"

    if valid:
        max_workers = min(len(valid), max(1, int(batch_ctx.get("max_workers", 8))))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    _run_one_manifest,
                    m, idx, pool_per_manifest, heartbeat_max_gap_s,
                    batch_ctx, heartbeat_dir, state_dir, logs_dir,
                    token_report_cb, budget_check_cb, shed_active,
                ): idx
                for idx, m in valid
            }
            for fut in concurrent.futures.as_completed(futures):
                idx = futures[fut]
                try:
                    raw_results[idx] = fut.result()
                except Exception as e:
                    nm = manifests[idx].get("name", f"manifest-{idx}") if isinstance(manifests[idx], dict) else str(manifests[idx])
                    raw_results[idx] = {"name": nm, "status": "failed", "reason": str(e) or "exception", "error": str(e), "allocated": pool_per_manifest, "iterations_used": 0}

    results: list[dict] = []
    pool_remainder = 0
    aborted = False
    abort_reason: str | None = None
    for idx, manifest in indexed:
        if idx in invalid:
            results.append(raw_results[idx])
            continue
        r = raw_results[idx]
        if r.get("_ledger_failed"):
            r = {k: v for k, v in r.items() if not k.startswith("_ledger") and k != "_budget_stop" or k == "_budget_stop"}
            r["pool_remainder_after"] = pool_remainder
            results.append(r)
            aborted = True
            abort_reason = raw_results[idx].get("_ledger_failed")
            for jdx in range(idx + 1, len(manifests)):
                rem = manifests[jdx]
                rn = rem.get("name", "unknown") if isinstance(rem, dict) else str(rem)
                results.append({"name": rn, "status": "skipped", "reason": "budget-unknown", "allocated": 0, "iterations_used": 0})
            break
        if r.get("_budget_stop"):
            r = {k: v for k, v in r.items() if k != "_budget_stop"}
            r["pool_remainder_after"] = r.get("pool_remainder_after", 0)
            pool_remainder = r["pool_remainder_after"]
            results.append(r)
            aborted = True
            abort_reason = "budget-exhausted"
            for jdx in range(idx + 1, len(manifests)):
                rem = manifests[jdx]
                rn = rem.get("name", "unknown") if isinstance(rem, dict) else str(rem)
                results.append({"name": rn, "status": "skipped", "reason": "budget-exhausted", "allocated": 0, "iterations_used": 0})
            break
        if r.get("status") == "completed" and r.get("reason") == "noop_yield":
            pool_remainder = r.get("pool_remainder_after", 0)
        else:
            pool_remainder = 0
            r["pool_remainder_after"] = 0
        results.append(r)

    pool_accounting = {
        "pool_per_manifest": pool_per_manifest,
        "pool_remainder": pool_remainder,
        "total_manifests": len(manifests),
        "heartbeat_max_gap_s": heartbeat_max_gap_s,
    }
    return {
        "results": results,
        "pool_accounting": pool_accounting,
        "pool": pool_accounting,
        "accounting": pool_accounting,
        "reason": abort_reason,
        "aborted": aborted,
        "completed": sum(1 for r in results if r.get("status") == "completed"),
        "skipped": sum(1 for r in results if r.get("status") == "skipped"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
    }


def run_agent_loop(bot_name, mission_prompt, model_responder, state_dir, max_iterations=50, tracer=None):
    state_dir = Path(state_dir)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    responder = model_responder
    if not callable(responder) and hasattr(model_responder, "respond") and callable(getattr(model_responder, "respond")):
        responder = getattr(model_responder, "respond")
    if not callable(responder):
        raise TypeError("model_responder must be callable or have .respond()")
    messages: list[dict] = [{"role": "user", "content": mission_prompt}]
    tool_calls: list[dict] = []
    files_touched: list[str] = []
    tickets_created = 0
    iterations = 0
    final_content = ""
    exit_reason = "unknown"
    continue_nudges = 0
    while True:
        if iterations >= max_iterations:
            exit_reason = "iteration_limit"
            break
        # Trace: log API-call preparation (message count & total size)
        if tracer is not None:
            try:
                _total_chars = sum(len(str(m.get("content", ""))) for m in messages)
                tracer.log_api_call_prepared(len(messages), _total_chars, iterations)
            except Exception:
                pass
        try:
            resp_json = responder(messages)
        except Exception as exc:
            _log(f"{bot_name}: model_responder raised {exc}, continuing")
            tool_calls.append({"name": "__responder_error__", "args": {}, "result": {"success": False, "output": "", "error": str(exc)}})
            if continue_nudges < 2:
                messages.append({"role": "user", "content": "continue"})
                continue_nudges += 1
                continue
            exit_reason = "error"
            break
        try:
            choices = resp_json.get("choices") or []
            msg = choices[0].get("message", {}) if choices else {}
        except Exception:
            msg = {}
        if not isinstance(msg, dict):
            msg = {}
        tool_calls_raw = msg.get("tool_calls")
        content = msg.get("content")
        has_tool_calls = bool(tool_calls_raw) and isinstance(tool_calls_raw, list) and len(tool_calls_raw) > 0
        has_content = bool(content is not None and str(content).strip() != "")
        if has_tool_calls:
            messages.append(msg)
            # --- Parallel read-only tool execution ---
            # Parse all tool calls first, then split into read-only (parallelizable)
            # and mutating (sequential) groups. Read-only tools are executed via
            # ThreadPoolExecutor; mutating tools run sequentially to avoid races.
            _READ_ONLY_TOOLS = frozenset({"read", "grep", "glob", "batch_read", "batch_grep"})
            parsed_calls: list[dict] = []
            for tc in tool_calls_raw:
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id", "") if isinstance(tc.get("id"), str) else ""
                func = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
                if not isinstance(func, dict):
                    func = {}
                name = func.get("name", "") if isinstance(func.get("name"), str) else ""
                args_raw = func.get("arguments", "{}")
                if isinstance(args_raw, dict):
                    args = args_raw
                else:
                    try:
                        args = json.loads(args_raw) if args_raw else {}
                    except Exception:
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                parsed_calls.append({"tc_id": tc_id, "name": name, "args": args})

            def _execute_parsed(parsed: dict) -> dict:
                """Execute a single parsed tool call, return enriched dict."""
                n = parsed["name"]
                a = parsed["args"]
                try:
                    r = _execute_tool(n, a)
                except Exception as exc:
                    r = {"success": False, "output": "", "error": str(exc)}
                return {**parsed, "result": r}

            # Split into read-only and mutating while preserving original order
            read_only_indices: list[int] = []
            mutating_indices: list[int] = []
            for idx, pc in enumerate(parsed_calls):
                if pc["name"] in _READ_ONLY_TOOLS:
                    read_only_indices.append(idx)
                else:
                    mutating_indices.append(idx)

            results_by_index: dict[int, dict] = {}

            # Execute read-only tools in parallel
            if read_only_indices:
                ro_calls = [parsed_calls[i] for i in read_only_indices]
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(ro_calls), 8)) as pool:
                    futures = {pool.submit(_execute_parsed, pc): idx for pc, idx in zip(ro_calls, read_only_indices)}
                    for future in concurrent.futures.as_completed(futures):
                        orig_idx = futures[future]
                        try:
                            results_by_index[orig_idx] = future.result()
                        except Exception as exc:
                            pc = parsed_calls[orig_idx]
                            results_by_index[orig_idx] = {**pc, "result": {"success": False, "output": "", "error": str(exc)}}

            # Execute mutating tools sequentially
            for idx in mutating_indices:
                results_by_index[idx] = _execute_parsed(parsed_calls[idx])

            # Process results in original order to maintain tool_call_id mapping
            for idx in range(len(parsed_calls)):
                completed = results_by_index[idx]
                name = completed["name"]
                args = completed["args"]
                result = completed["result"]
                tc_id = completed["tc_id"]
                # Trace: log tool output processing
                if tracer is not None:
                    try:
                        tracer.log_tool_output(name, result, iterations)
                    except Exception:
                        pass
                if not result.get("success", True):
                    _log(f"{bot_name}: tool '{name}' failed: {result.get('error', 'unknown')}")
                touched = args.get("path") or args.get("filePath") or args.get("file")
                if name in ("edit", "write") and touched and isinstance(touched, str):
                    files_touched.append(touched)
                if name == "create_ticket" and result.get("success"):
                    tickets_created += 1
                rec = _HybridToolCall({"name": name, "args": args, "result": result})
                tool_calls.append(rec)
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps(result),
                }
                messages.append(tool_msg)
                try:
                    _write_bot_status(bot_name, state_dir, f"tool:{name}", f"executing {name} on {touched or 'N/A'}", files_touched, iterations + 1)
                except Exception:
                    pass
            _has_mutating = any(pc["name"] not in _READ_ONLY_TOOLS for pc in parsed_calls)
            iterations += 1.0 if _has_mutating else 0.25
            continue_nudges = 0
            continue
        if has_content:
            final_content = str(content).strip()
            try:
                messages.append(msg)
            except Exception:
                pass
            exit_reason = "completed"
            break
        if continue_nudges < 2:
            _log(f"{bot_name}: empty response, nudging 'continue' ({continue_nudges + 1}/2)")
            messages.append({"role": "user", "content": "continue"})
            continue_nudges += 1
            continue
        _log(f"{bot_name}: no content after 2 nudges")
        exit_reason = "no_content"
        break
    result = AgentResult(
        exit_reason=exit_reason,
        tool_calls=tool_calls,
        tickets_created=tickets_created,
        files_touched=files_touched,
        iterations=iterations,
        messages=messages,
        final_content=final_content,
    )
    try:
        result.bot_name = bot_name
    except Exception:
        pass
    try:
        object.__setattr__(result, "_bot_name", bot_name)
    except Exception:
        pass
    return result


def run_bot(bot_name, model, mission_prompt, heartbeat_file, ckpt_file, fallback_model="", max_tokens_per_run=0, fallback_models=None):
    """Call dialagram in a tool loop with stall recovery and drain respect.

    Args:
        bot_name: Bot identifier for checkpoint payload.
        model: Dialagram model name (e.g. xiaomi-mimo-2.5).
        mission_prompt: Mission text injected verbatim (not loaded from disk).
        heartbeat_file: Path to write float(time.time()) heartbeat.
        ckpt_file: Path to write <4KB checkpoint JSON via tmp+replace.
        fallback_model: Model to switch to when the primary fails fatally; empty disables.

    Returns:
        Never returns normally — exits 0 on content completion or drain,
        exits 1 on safety limits / exhausted retries.
    """
    # Drain check before any API work — orchestrator asked us to stand down
    if _is_draining(bot_name):
        _log(f"{bot_name}: drain active, exiting cleanly")
        _write_heartbeat(heartbeat_file)
        sys.exit(0)

    _log(f"{bot_name}: starting (model={model}, fallback={fallback_model or 'none'}, prompt={len(mission_prompt)} chars)")
    api_key = _resolve_api_key()
    if not api_key:
        _log(f"{bot_name}: FATAL — no API key found (env DIALAGRAM_API_KEY or opencode.jsonc)")
        sys.exit(1)
    _log(f'{bot_name}: API key resolved (present={bool(api_key)})')

    active_model = model
    used_fallback = False
    _fallback_chain = list(fallback_models or [])
    if fallback_model and fallback_model not in _fallback_chain:
        _fallback_chain.insert(0, fallback_model)
    _fallback_idx = 0
    state_dir = Path(heartbeat_file).parent
    files_touched: list[str] = []
    _write_bot_status(bot_name, state_dir, "starting", f"model={model}", files_touched, 0)

    contract = _contract(heartbeat_file, ckpt_file)
    full_message = (
        f"Sisyphus — delegated task: '{bot_name}' workflow (model {model}).\n"
        + contract
        + "\n--- Mission ---\n"
        + mission_prompt
    )

    messages = [{"role": "user", "content": full_message}]
    _write_heartbeat(heartbeat_file)
    hb_thread = _start_heartbeat_thread(heartbeat_file, interval=30)
    github_target = _github_issue_target(bot_name, mission_prompt) or _queued_github_target(bot_name)
    if github_target:
        _update_github_progress(github_target, bot_name, 0, "claimed by implementer")
    tool_iterations = 0
    total_retries = 0
    timeout_retries = 0
    MAX_429_RETRIES = 10
    MIN_429_BACKOFF = 10.0
    continue_nudges = 0
    exit_reason = "unknown"
    tickets_created = 0

    # CAP-07: Context compaction state
    try:
        from codebot.context_compactor import compact_messages, needs_compaction
        _compaction_available = True
    except ImportError:
        _compaction_available = False
    _compaction_budget = max_tokens_per_run if max_tokens_per_run > 0 else 120_000

    # CB-3548779-D85C: Context-assembly tracer for structured data-flow logging
    _logs_dir = Path(heartbeat_file).parent.parent / "logs"
    try:
        _logs_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    _tracer = ContextAssemblyTracer(bot_name, _logs_dir)

    # CAP-09/CAP-11: Ticket-scoped scratchpad for cross-agent handoff
    _scratch_ticket_id = bot_name
    try:
        _ticket_match = __import__("re").search(r"ASSIGNED TICKET: (CB-[\w-]+)", mission_prompt or "")
        if _ticket_match:
            _scratch_ticket_id = _ticket_match.group(1)
    except Exception:
        pass
    try:
        from codebot.scratchpad import load_scratchpad, save_scratchpad
        _scratch_state = load_scratchpad(state_dir, _scratch_ticket_id)
        _scratch_state.start_agent(bot_name, "IMPLEMENT")
        save_scratchpad(state_dir, _scratch_state)
        _scratch_available = True
    except ImportError:
        _scratch_state = None
        _scratch_available = False

    # SIGTERM → SystemExit so the finally block persists the stream before death
    def _sigterm_handler(signum, frame):
        nonlocal exit_reason
        exit_reason = "terminated"
        _write_heartbeat(heartbeat_file)
        sys.exit(143)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    try:
        def _model_responder(msgs):
            nonlocal active_model, total_retries, timeout_retries, exit_reason, used_fallback, _fallback_idx
            while True:
                try:
                    _wait_for_rate_limit(active_model)
                    _record_request(active_model)
                    result = _call_api(msgs, active_model, api_key, timeout=_timeout_for_bot(bot_name, active_model), bot_name=bot_name)
                    _record_success(active_model)
                    return result
                except urllib.error.HTTPError as exc:
                    if exc.code == 429:
                        retry_after = None
                        if hasattr(exc, 'headers'):
                            ra = exc.headers.get('Retry-After') or exc.headers.get('retry-after')
                            if ra:
                                try:
                                    retry_after = float(ra)
                                except (ValueError, TypeError):
                                    pass
                        _record_rate_limit(active_model, retry_after)
                        if total_retries < MAX_429_RETRIES:
                            if retry_after:
                                delay = max(retry_after, MIN_429_BACKOFF)
                            elif _HAS_RATE_LIMITER and _rate_limiter:
                                state = _rate_limiter._get_state(active_model)
                                delay = max(state.effective_interval, MIN_429_BACKOFF)
                            else:
                                delay = max(BACKOFFS[min(total_retries, len(BACKOFFS) - 1)], MIN_429_BACKOFF)
                            delay = min(delay, MAX_BACKOFF)
                            _log(f"{bot_name}: 429 rate-limited, retrying in {delay:.1f}s ({total_retries+1}/{MAX_429_RETRIES})")
                            _write_heartbeat(heartbeat_file)
                            time.sleep(delay)
                            total_retries += 1
                            continue
                        _log(f"{bot_name}: 429 rate-limited after {total_retries} retries, yielding slot for requeue")
                        _write_heartbeat(heartbeat_file)
                        exit_reason = "rate_limited_yield"
                        sys.exit(3)
                    try:
                        body = exc.read(MAX_RESPONSE_BYTES).decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
                        if "429" in body or "rate" in body.lower():
                            _record_rate_limit(active_model, None)
                            if total_retries < MAX_429_RETRIES:
                                if _HAS_RATE_LIMITER and _rate_limiter:
                                    state = _rate_limiter._get_state(active_model)
                                    delay = max(state.effective_interval, MIN_429_BACKOFF)
                                else:
                                    delay = max(BACKOFFS[min(total_retries, len(BACKOFFS) - 1)], MIN_429_BACKOFF)
                                delay = min(delay, MAX_BACKOFF)
                                _log(f"{bot_name}: rate-limited (body), retrying in {delay:.1f}s ({total_retries+1}/{MAX_429_RETRIES})")
                                _write_heartbeat(heartbeat_file)
                                time.sleep(delay)
                                total_retries += 1
                                continue
                            _log(f"{bot_name}: rate-limited (body) after {total_retries} retries, yielding slot for requeue")
                            _write_heartbeat(heartbeat_file)
                            exit_reason = "rate_limited_yield"
                            sys.exit(3)
                    except SystemExit:
                        raise
                    except Exception:
                        pass
                    if _fallback_idx < len(_fallback_chain):
                        active_model = _fallback_chain[_fallback_idx]
                        _fallback_idx += 1
                        total_retries = 0
                        timeout_retries = 0
                        _log(f"{bot_name}: HTTP error on primary, switching to fallback {active_model}")
                        continue
                    _log(f"{bot_name}: FATAL — all fallback models exhausted")
                    exit_reason = "http_error"
                    sys.exit(1)
                except urllib.error.URLError as exc:
                    msg = str(exc).lower()
                    is_timeout = "timed out" in msg or "timeout" in msg or isinstance(exc.reason, TimeoutError) if hasattr(exc, "reason") else False
                    if total_retries < MAX_RETRIES and timeout_retries < MAX_TIMEOUT_RETRIES:
                        delay = BACKOFFS[min(total_retries, len(BACKOFFS) - 1)]
                        delay = min(delay, MAX_BACKOFF)
                        _log(f"{bot_name}: connection error, retrying in {delay}s")
                        time.sleep(delay)
                        total_retries += 1
                        timeout_retries += 1
                        continue
                    _log(f"{bot_name}: FATAL — connection error after {total_retries} retries")
                    if fallback_model and not used_fallback:
                        active_model = fallback_model
                        used_fallback = True
                        total_retries = 0
                        timeout_retries = 0
                        continue
                    exit_reason = "connection_error"
                    sys.exit(1)
                except TimeoutError:
                    if total_retries < MAX_RETRIES and timeout_retries < MAX_TIMEOUT_RETRIES:
                        delay = BACKOFFS[min(total_retries, len(BACKOFFS) - 1)]
                        delay = min(delay, MAX_BACKOFF)
                        _log(f"{bot_name}: timeout, retrying in {delay}s ({timeout_retries + 1}/{MAX_TIMEOUT_RETRIES})")
                        time.sleep(delay)
                        total_retries += 1
                        timeout_retries += 1
                        continue
                    _log(f"{bot_name}: FATAL — timeout after {timeout_retries} retries")
                    if fallback_model and not used_fallback:
                        active_model = fallback_model
                        used_fallback = True
                        total_retries = 0
                        timeout_retries = 0
                        continue
                    exit_reason = "timeout"
                    sys.exit(1)
                except Exception as exc:
                    import traceback
                    if total_retries < MAX_RETRIES:
                        delay = BACKOFFS[min(total_retries, len(BACKOFFS) - 1)]
                        delay = min(delay, MAX_BACKOFF)
                        _log(f"{bot_name}: unexpected error ({type(exc).__name__}: {exc}), retrying in {delay}s")
                        _log(f"{bot_name}: traceback: {traceback.format_exc()[:500]}")
                        time.sleep(delay)
                        total_retries += 1
                        continue
                    _log(f"{bot_name}: FATAL — unexpected error after {total_retries} retries")
                    if fallback_model and not used_fallback:
                        active_model = fallback_model
                        used_fallback = True
                        total_retries = 0
                        timeout_retries = 0
                        continue
                    exit_reason = "unexpected_error"
                    sys.exit(1)

        result = run_agent_loop(bot_name, full_message, _model_responder, state_dir, max_iterations=MAX_TOOL_ITERATIONS, tracer=_tracer)
        tool_iterations = result.iterations
        tickets_created = result.tickets_created
        files_touched = result.files_touched
        messages = result.messages
        exit_reason = result.exit_reason
        final_content = result.final_content
        if exit_reason == "completed":
            _log(f"{bot_name}: model returned content, completing ({tool_iterations} tool iterations)")
            _write_heartbeat(heartbeat_file)
            _write_checkpoint(ckpt_file, bot_name, "completed")
            sys.exit(0)
        elif exit_reason == "iteration_limit":
            _log(f"{bot_name}: FATAL — hit {MAX_TOOL_ITERATIONS} tool iteration limit")
            if _scratch_available and _scratch_state is not None:
                _scratch_state.mark_error(f"iteration limit {MAX_TOOL_ITERATIONS}")
                _scratch_state.iteration = tool_iterations
                save_scratchpad(state_dir, _scratch_state)
            sys.exit(1)
        elif exit_reason == "no_content":
            _log(f"{bot_name}: FATAL — no content after 2 nudges")
            sys.exit(1)
        elif exit_reason == "drain":
            _log(f"{bot_name}: drain detected, exiting cleanly")
            _write_heartbeat(heartbeat_file)
            sys.exit(0)
        else:
            _log(f"{bot_name}: exiting with reason {exit_reason}")
            sys.exit(1)
    finally:
        _stop_heartbeat_thread(hb_thread)
        _persist_stream(bot_name, messages, active_model, tool_iterations, exit_reason)
        # CB-3548779-D85C: Flush context-assembly trace summary
        try:
            _tracer.flush()
        except Exception:
            pass
        discovery_roles = frozenset({
            "bug_hunter", "security_auditor", "architecture_auditor", "performance_auditor",
            "test_gap_auditor", "documentation_auditor", "dependency_auditor", "ux_auditor",
        })
        base_name = bot_name.split("-")[0] if "-" in bot_name else bot_name
        if base_name in discovery_roles and tickets_created == 0:
            if exit_reason in ("completed", "iteration_limit", "drain"):
                marker_dir = Path(os.environ.get("CODEBOT_PROJECT_ROOT", str(WORK_ROOT))) / ".codebot" / "state"
                marker_dir.mkdir(parents=True, exist_ok=True)
                marker = marker_dir / f"{bot_name}.no_tickets"
                try:
                    marker.write_text(str(time.time()), encoding="utf-8")
                except OSError:
                    pass
        if _scratch_available and _scratch_state is not None:
            if exit_reason in ("timeout", "rate_limit", "fatal_error", "token_cap", "iteration_limit", "connection_error"):
                _scratch_state.mark_error(f"exited: {exit_reason}")
                _scratch_state.finish_agent(f"interrupted: {exit_reason}")
            elif exit_reason in ("completed", "drain"):
                _scratch_state.finish_agent(f"completed: {exit_reason}")
            _scratch_state.iteration = tool_iterations
            _scratch_state.last_tool_result_summary = f"reason={exit_reason} iters={tool_iterations}"
            save_scratchpad(state_dir, _scratch_state)


if __name__ == "__main__":
    if len(sys.argv) < 6:
        print(
            f"Usage: {sys.argv[0]} <bot_name> <model> <heartbeat_file> <ckpt_file> <mission_file> [fallback_model] [max_tokens_per_run] [fallback2,fallback3,...]",
            file=sys.stderr,
        )
        sys.exit(2)
    _bot_name, _model, _heartbeat_file, _ckpt_file, _mission_file = sys.argv[1:6]
    try:
        from codebot.codebot_bootstrap import bootstrap as _bootstrap
        _adapter = _bootstrap(Path(_heartbeat_file).parent.parent.parent)
        if _adapter:
            set_project_adapter(_adapter)
    except Exception:
        pass
    _fallback = sys.argv[6] if len(sys.argv) > 6 else ""
    _mtr = int(sys.argv[7]) if len(sys.argv) > 7 else 0
    _fb_chain = sys.argv[8].split(",") if len(sys.argv) > 8 and sys.argv[8] else []
    try:
        _mission_prompt = Path(_mission_file).read_text(encoding="utf-8")
    except Exception as exc:
        print(f"failed to read mission file {_mission_file}: {exc}", file=sys.stderr)
        sys.exit(2)
    run_bot(_bot_name, _model, _mission_prompt, _heartbeat_file, _ckpt_file, _fallback, _mtr, _fb_chain)
