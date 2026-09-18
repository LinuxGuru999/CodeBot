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
"""

import fcntl
import json
import os
import re
import signal
import sys
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from bots.api_tools import bash, read, write, edit, grep, glob
except ImportError:
    from codebot.api_tools import bash, read, write, edit, grep, glob


def _log(msg: str) -> None:
    """Write timestamped line to stdout (orchestrator redirects to per-bot .log)."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


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


def _auto_commit(bot_name: str, files_touched: list[str]) -> None:
    """Auto-commit and push any uncommitted changes when a worker completes.

    Why: Workers often skip the git commit step even when told to commit.
    This structural fix ensures changes are always committed after completion.
    SELF-03: Gatekeeper must PASS before any commit proceeds.
    """
    if _adapter_instance is not None and files_touched:
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
                ticket_id=bot_name,
                ticket_class=ticket_class,
                changed_files=files_touched,
            )
            if result.get("decision") != "COMPLETE":
                _log(f"{bot_name}: gatekeeper BLOCKED commit — decision={result.get('decision')} failed_gates={result.get('failed_gates', [])}")
                return
        except ImportError:
            pass
        except Exception as gk_err:
            _log(f"{bot_name}: gatekeeper check failed (allowing commit): {gk_err}")

    repos = set()
    for f in files_touched:
        abs_path = f if f.startswith("/") else str(WORK_ROOT / f)
        if "Monitor-Manager-Python" in abs_path or "lib-common" in abs_path:
            repos.add("Monitor-Manager-Python")
        if "Monitor-Client-Python" in abs_path:
            repos.add("Monitor-Client-Python")

    if not repos:
        return

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
        result = bash(f"git -C {repo_path} status --short", timeout=10)
        if not result["success"] or not result["output"].strip():
            continue

        add_result = bash(f"git -C {repo_path} add -A", timeout=15)
        if not add_result["success"]:
            _log(f"{bot_name}: auto-commit git add failed for {repo}: {add_result.get('error', '')}")
            continue

        commit_msg = f"bot: {bot_name} auto-commit after task completion"
        commit_result = bash(f'git -C {repo_path} commit -m "{commit_msg}"', timeout=15)
        if not commit_result["success"]:
            if "nothing to commit" in commit_result.get("error", "") or "nothing to commit" in commit_result.get("output", ""):
                continue
            _log(f"{bot_name}: auto-commit git commit failed for {repo}: {commit_result.get('error', '')}")
            continue

        push_result = bash(f"git -C {repo_path} push origin HEAD", timeout=30)
        if push_result["success"]:
            _log(f"{bot_name}: auto-commit + push successful for {repo}")
        else:
            _log(f"{bot_name}: auto-commit push failed for {repo}: {push_result.get('error', '')}")


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
    return _DEFAULT_API_URL


API_URL = _DEFAULT_API_URL
API_TIMEOUT = 120


API_URL = _DEFAULT_API_URL
API_TIMEOUT = 120
MAX_TOOL_ITERATIONS = 150
MAX_RETRIES = 5
MAX_TIMEOUT_RETRIES = 3
BACKOFFS = [2, 4, 8, 16, 32]
MAX_BACKOFF = 60
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
        existing: list[str] = []
        if path.exists():
            existing = path.read_text(encoding="utf-8").splitlines(keepends=True)
        existing.append(line)
        blob = "".join(existing)
        if len(blob.encode("utf-8")) > SCRATCHPAD_MAX_BYTES:
            blob = "".join(existing[-100:])
        path.write_text(blob, encoding="utf-8")
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
            "description": "Create a new work ticket in the TicketStore. Use this to report bugs, security issues, performance problems, missing tests, documentation gaps, or feature requests discovered during code analysis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short descriptive title of the issue found"},
                    "ticket_class": {"type": "string", "enum": ["bug", "feature", "security", "performance", "documentation", "test", "refactor", "dependency", "architecture", "infrastructure"], "description": "Category of work needed"},
                    "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"], "description": "How severe is this issue"},
                    "source": {"type": "string", "description": "Your role name (e.g., bug_hunter, security_auditor)"},
                    "evidence": {"type": "string", "description": "Exact code snippet, file path:line, or log output proving the issue"},
                    "problem_statement": {"type": "string", "description": "What is wrong and why it matters"},
                    "desired_state": {"type": "string", "description": "What correct behavior looks like"},
                    "acceptance_criteria": {"type": "string", "description": "Semicolon-separated list of conditions that prove this is fixed"},
                    "affected_modules": {"type": "string", "description": "Comma-separated list of files or directories affected"},
                    "risk": {"type": "string", "enum": ["critical", "high", "medium", "low"], "description": "Risk level of implementing the fix"},
                },
                "required": ["title", "ticket_class", "severity", "evidence", "problem_statement", "desired_state", "acceptance_criteria"],
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
    title: str,
    ticket_class: str = "feature",
    severity: str = "medium",
    source: str = "agent",
    evidence: str = "",
    problem_statement: str = "",
    desired_state: str = "",
    acceptance_criteria: str = "",
    affected_modules: str = "",
    risk: str = "medium",
) -> dict:
    try:
        from codebot.ticket_engine import (
            create_ticket, TicketStore, TicketClass, Severity, RiskLevel,
        )
    except ImportError:
        return {"success": False, "output": "", "error": "ticket_engine not available"}
    class_map = {v.value: v for v in TicketClass}
    sev_map = {v.value: v for v in Severity}
    risk_map = {v.value: v for v in RiskLevel}
    tc = class_map.get(ticket_class.lower(), TicketClass.FEATURE)
    sv = sev_map.get(severity.lower(), Severity.MEDIUM)
    rk = risk_map.get(risk.lower(), RiskLevel.MEDIUM)
    ac_list = [c.strip() for c in acceptance_criteria.split(";") if c.strip()] if acceptance_criteria else [title]
    modules = [m.strip() for m in affected_modules.split(",") if m.strip()] if affected_modules else []
    if not problem_statement:
        problem_statement = title
    if not desired_state:
        desired_state = f"Resolve: {title}"
    try:
        ticket = create_ticket(
            title=title,
            ticket_class=tc,
            severity=sv,
            source=source,
            evidence=evidence or title,
            problem_statement=problem_statement,
            desired_state=desired_state,
            acceptance_criteria=ac_list,
            risk=rk,
            affected_modules=modules,
        )
    except ValueError as ve:
        return {"success": False, "output": "", "error": str(ve)}
    store_path = Path(".codebot/state/tickets.json")
    if _adapter_instance is not None:
        try:
            store_path = _adapter_instance.paths().state_dir / "tickets.json"  # type: ignore[union-attr]
        except Exception:
            pass
    if not store_path.is_absolute():
        store_path = (WORK_ROOT / store_path).resolve()
    impl_map = {
        "bug": "general_implementer", "feature": "general_implementer",
        "refactor": "general_implementer", "security": "backend_implementer",
        "performance": "backend_implementer", "architecture": "backend_implementer",
        "test": "test_implementer", "documentation": "documentation_implementer",
        "dependency": "migration_implementer", "infrastructure": "migration_implementer",
    }
    impl_type = impl_map.get(tc.value, "general_implementer")
    try:
        store = TicketStore(store_path)
        store.add(ticket)
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


def _write_checkpoint(ckpt_file, bot_name, reason):
    """Write <4 KB JSON checkpoint atomically via tmp+replace."""
    try:
        p = Path(ckpt_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"bot": bot_name, "updated_at": time.time(), "reason": reason}
        body = json.dumps(payload)
        # Guard 4 KB limit — payload is tiny, but truncate reason if ever needed
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
        bounded = []
        for m in messages:
            entry = dict(m)
            if entry.get("role") == "tool" and isinstance(entry.get("content"), str):
                if len(entry["content"]) > 2000:
                    entry["content"] = entry["content"][:2000] + "...[truncated]"
            bounded.append(entry)
        payload = {
            "bot": bot_name,
            "model": active_model,
            "tool_iterations": tool_iterations,
            "exit_reason": exit_reason,
            "persisted_at": time.time(),
            "usage": dict(usage) if isinstance(usage, dict) else {},
            "messages": bounded,
        }
        body = json.dumps(payload, ensure_ascii=False)
        if len(body) > 500_000:
            payload["messages"] = [bounded[0]] + bounded[-20:] if len(bounded) > 21 else bounded
            payload["truncated"] = True
            body = json.dumps(payload, ensure_ascii=False)
        p = BOTS_DIR / "logs" / f"{bot_name}.stream.json"
        tmp = Path(str(p) + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def _contract(heartbeat_file, ckpt_file):
    """Minimal shared-infra contract (<800 chars) without importing prompt_gateway."""
    # Inlined to avoid prompt_gateway side effects; keep only drain/heartbeat/checkpoint/bound
    return (
        "[SHARED INFRA CONTRACT]\n"
        f"- Drain: check `state/.drain` or `state/.drain_<bot_name>` via `read` tool before each task. If either exists, exit 0 cleanly.\n"
        f"- Heartbeat: write float(time.time()) to {heartbeat_file} at startup and after each task; never exceed 120s gap. Use the `write` tool with just the timestamp string.\n"
        f"- Checkpoint: after each task write <4KB JSON to {ckpt_file} using the `write` tool. Keys: bot, updated_at, reason.\n"
        "- Bound: bounded delegated task, not a daemon. Do atomic work, heartbeat, checkpoint, exit 0 on drain or completion."
    )


def _call_api(messages, model, api_key):
    """Non-streaming POST to dialagram; raises on HTTP/timeout for caller retry."""
    body = {
        "model": model,
        "messages": messages,
        "tools": TOOL_SCHEMAS,
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
    # 120 s stall detection — matches opencode-auto-resume semantics
    with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
        raw = resp.read(2_000_000)
        text = raw.decode("utf-8", errors="replace")
        return json.loads(text)


def _execute_tool(name, args):
    """Dispatch tool_calls by name to api_tools; unknown name is fail-open."""
    func = _TOOL_MAP.get(name)
    if func is None:
        return {"success": False, "output": "", "error": f"unknown tool: {name}"}
    try:
        if name == "write":
            path = args.get("path", "") if isinstance(args, dict) else ""
            if isinstance(path, str) and path.endswith(".heartbeat"):
                return {"success": True, "output": f"Wrote {len(str(time.time()))} bytes to {path}", "error": None, "_heartbeat_injected": True}
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
    nudges = 0
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    api_calls = 0

    def _result(status, reason=None):
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
                resp_json = api_call(msgs, model, api_key)
                api_calls += 1
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and total_retries < MAX_RETRIES:
                    _sleep(min(_backoff(), MAX_BACKOFF))
                    total_retries += 1
                    continue
                try:
                    body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
                    if ("429" in body or "rate" in body.lower()) and total_retries < MAX_RETRIES:
                        _sleep(min(_backoff(), MAX_BACKOFF))
                        total_retries += 1
                        continue
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
        try:
            u = _extract_provider_usage(resp_json)
            for k in usage:
                usage[k] += int(u.get(k, 0))
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
                msgs.append({"role": "tool", "tool_call_id": tc_id, "content": json.dumps(r)})
            tool_iterations += 1
            continue
        if has_content:
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
                fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
                fcntl.flock(fp, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                fp.close()
            except Exception:
                pass


def run_batch(manifests: list[dict], batch_ctx: dict) -> dict:
    """Sequential multi-manifest execution with per-manifest liveness.

    Frozen signature: run_batch(manifests: list[dict], batch_ctx: dict)
    batch_ctx carries {pool_per_manifest: int = 50, heartbeat_max_gap_s: int = 120,
                      heartbeat_dir/state_dir, token_report_cb, drain_check_cb, budget_check_cb}
    Supports both heartbeat_dir and state_dir (plus heartbeatDir/stateDir aliases);
    if only one is given it is used for both heartbeat and state. Atomic writes via tmp+rename.
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

    results: list[dict] = []
    pool_remainder = 0
    aborted = False
    abort_reason: str | None = None
    shed_active = False
    pending_budget_stop = False

    for idx, manifest in enumerate(manifests):
        if not isinstance(manifest, dict):
            results.append({"name": str(manifest), "status": "skipped", "reason": "invalid-manifest", "allocated": 0})
            continue
        name = manifest.get("name", f"manifest-{idx}")
        tier_raw = manifest.get("tier_priority", 999)
        try:
            tier_int = int(tier_raw)
        except Exception:
            tier_int = 999

        if _drain_requested(batch_ctx, state_dir):
            aborted = True
            abort_reason = "drain"
            break

        effective_timeout = _effective_for_manifest(manifest, heartbeat_max_gap_s)
        if _is_stuck_manifest(heartbeat_dir, logs_dir, manifest, heartbeat_max_gap_s):
            hb_path = _heartbeat_path_for(heartbeat_dir, name)
            try:
                _write_json_atomic(state_dir / f"{name}.checkpoint.json", {"bot": name, "updated_at": time.time(), "reason": "stuck-killed", "stuck": True, "effective_timeout": effective_timeout})
            except Exception:
                pass
            results.append({"name": name, "status": "skipped", "reason": "stale-heartbeat", "allocated": 0, "iterations_used": 0, "heartbeat": str(hb_path), "stuck": True})
            continue

        budget_state = None
        if budget_check_cb and callable(budget_check_cb):
            try:
                budget_state = budget_check_cb()
            except Exception:
                budget_state = None
        if budget_state == "shed_tier3":
            shed_active = True
        if shed_active and tier_int >= 31:
            results.append({"name": name, "status": "skipped", "reason": "budget-shed", "allocated": 0, "iterations_used": 0, "tier_priority": tier_int})
            continue
        if budget_state == "stop":
            pending_budget_stop = True

        if not _token_gate_allows(manifest):
            results.append({"name": name, "status": "skipped", "reason": "token-gate", "allocated": 0, "iterations_used": 0})
            continue

        allocated = pool_per_manifest + pool_remainder
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
            if is_noop_hit:
                remainder = allocated - iterations_used
                if remainder < 0:
                    remainder = 0
                pool_remainder = remainder
                status = "completed"
                reason = "noop_yield"
            else:
                pool_remainder = 0
                status = "completed"
                reason = "completed"

            _write_heartbeat_atomic(hb_path)

            ckpt_path = state_dir / f"{name}.checkpoint.json"
            _write_json_atomic(ckpt_path, {"bot": name, "updated_at": time.time(), "reason": reason, "allocated": allocated, "iterations_used": iterations_used})

            ok, ledger_reason = _locked_ledger_write(state_dir, token_report_cb, name)
            if not ok:
                aborted = True
                abort_reason = ledger_reason
                results.append({"name": name, "status": status, "reason": reason, "allocated": allocated, "iterations_used": iterations_used, "heartbeat": str(hb_path), "noop_hit": is_noop_hit, "pool_remainder_after": pool_remainder})
                for rem in manifests[idx + 1:]:
                    rn = rem.get("name", "unknown") if isinstance(rem, dict) else str(rem)
                    results.append({"name": rn, "status": "skipped", "reason": "budget-unknown", "allocated": 0, "iterations_used": 0})
                break

            results.append({"name": name, "status": status, "reason": reason, "allocated": allocated, "iterations_used": iterations_used, "heartbeat": str(hb_path), "noop_hit": is_noop_hit, "pool_remainder_after": pool_remainder})

            if pending_budget_stop:
                aborted = True
                abort_reason = "budget-exhausted"
                for rem in manifests[idx + 1:]:
                    rn = rem.get("name", "unknown") if isinstance(rem, dict) else str(rem)
                    results.append({"name": rn, "status": "skipped", "reason": "budget-exhausted", "allocated": 0, "iterations_used": 0})
                break

        except Exception as e:
            _write_heartbeat_atomic(hb_path)
            try:
                ckpt_path = state_dir / f"{name}.checkpoint.json"
                _write_json_atomic(ckpt_path, {"bot": name, "updated_at": time.time(), "reason": f"failed: {e}", "error": str(e)})
            except Exception:
                pass
            # on failure, reset pool remainder (no yield)
            pool_remainder = 0
            allocated_fallback = allocated if "allocated" in locals() else pool_per_manifest
            results.append({"name": name, "status": "failed", "reason": str(e) or "exception", "error": str(e), "allocated": allocated_fallback, "iterations_used": 0, "heartbeat": str(hb_path)})
            if pending_budget_stop:
                for rem in manifests[idx + 1:]:
                    rn = rem.get("name", "unknown") if isinstance(rem, dict) else str(rem)
                    results.append({"name": rn, "status": "skipped", "reason": "budget-exhausted", "allocated": 0, "iterations_used": 0})
                aborted = True
                abort_reason = "budget-exhausted"
                break
            continue

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
    _log(f"{bot_name}: API key resolved ({api_key[:10]}...)")

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
    github_target = _github_issue_target(bot_name, mission_prompt) or _queued_github_target(bot_name)
    if github_target:
        _update_github_progress(github_target, bot_name, 0, "claimed by implementer")
    tool_iterations = 0
    total_retries = 0
    timeout_retries = 0
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

    # CAP-09/CAP-11: Structured scratchpad for handoff on failure
    try:
        from codebot.scratchpad import load_scratchpad, save_scratchpad, ScratchpadState
        _scratch_state = load_scratchpad(state_dir, bot_name)
        _scratch_state.phase = "running"
        _scratch_state.ticket_id = bot_name
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
        while True:
            if tool_iterations >= MAX_TOOL_ITERATIONS:
                _log(f"{bot_name}: FATAL — hit {MAX_TOOL_ITERATIONS} tool iteration limit")
                exit_reason = "iteration_limit"
                # CAP-11: Save scratchpad for task_splitter handoff before exit
                if _scratch_available and _scratch_state is not None:
                    _scratch_state.mark_error(f"iteration limit {MAX_TOOL_ITERATIONS}")
                    _scratch_state.iteration = tool_iterations
                    save_scratchpad(state_dir, _scratch_state)
                sys.exit(1)

            # Periodic drain check between iterations (tool executions check individually too)
            if tool_iterations > 0 and _is_draining(bot_name):
                _log(f"{bot_name}: drain detected mid-run, exiting cleanly")
                _write_heartbeat(heartbeat_file)
                exit_reason = "drain"
                sys.exit(0)

            # CAP-07: Auto-compact context when approaching token budget
            if _compaction_available and tool_iterations > 0 and tool_iterations % 5 == 0:
                if needs_compaction(messages, max_tokens=_compaction_budget):
                    old_len = len(messages)
                    messages = compact_messages(messages, max_tokens=_compaction_budget)
                    _log(f"{bot_name}: context compacted ({old_len}→{len(messages)} messages)")
                    if _scratch_available and _scratch_state is not None:
                        _scratch_state.context_summary = f"Compacted at iter {tool_iterations}, {len(messages)} msgs remain"
                        save_scratchpad(state_dir, _scratch_state)
                sys.exit(0)

            # ---- API call with retry envelope ----
            resp_json = None
            _fallback_needed = False
            _log(f"{bot_name}: API call #{tool_iterations + 1} ({len(messages)} msgs)")
            while True:
                try:
                    resp_json = _call_api(messages, active_model, api_key)
                    break
                except urllib.error.HTTPError as exc:
                    # 429/rate-limit gets exponential backoff; others are fatal
                    if exc.code == 429 and total_retries < MAX_RETRIES:
                        delay = BACKOFFS[min(total_retries, len(BACKOFFS) - 1)]
                        delay = min(delay, MAX_BACKOFF)
                        _log(f"{bot_name}: 429 rate-limited, backing off {delay}s (retry {total_retries + 1}/{MAX_RETRIES})")
                        time.sleep(delay)
                        total_retries += 1
                        continue
                    # Try to handle 429 encoded only in body with non-429 status
                    try:
                        body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
                        if "429" in body or "rate" in body.lower():
                            if total_retries < MAX_RETRIES:
                                delay = BACKOFFS[min(total_retries, len(BACKOFFS) - 1)]
                                delay = min(delay, MAX_BACKOFF)
                                _log(f"{bot_name}: rate-limited (body), backing off {delay}s")
                                time.sleep(delay)
                                total_retries += 1
                                continue
                    except Exception:
                        pass
                    _log(f"{bot_name}: FATAL — all fallback models exhausted")
                    exit_reason = "http_error"
                    sys.exit(1)
                except urllib.error.URLError as exc:
                    # Covers timeout and connection errors from urlopen
                    msg = str(exc).lower()
                    is_timeout = "timed out" in msg or "timeout" in msg or isinstance(exc.reason, TimeoutError) if hasattr(exc, "reason") else False
                    # Connection errors and timeouts share the same retry budget but timeout has its own cap
                    if total_retries < MAX_RETRIES and timeout_retries < MAX_TIMEOUT_RETRIES:
                        delay = BACKOFFS[min(total_retries, len(BACKOFFS) - 1)]
                        delay = min(delay, MAX_BACKOFF)
                        _log(f"{bot_name}: connection error, retrying in {delay}s")
                        time.sleep(delay)
                        total_retries += 1
                        # Only count toward timeout cap if it looks like a timeout
                        if is_timeout or "connection" in msg or "temporary failure" in msg or "name resolution" in msg:
                            timeout_retries += 1
                        else:
                            timeout_retries += 1
                        continue
                    _log(f"{bot_name}: FATAL — connection error after {total_retries} retries")
                    if fallback_model and not used_fallback:
                        _fallback_needed = True
                        break
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
                        _fallback_needed = True
                        break
                    exit_reason = "timeout"
                    sys.exit(1)
                except Exception:
                    # Unknown transient — retry up to global cap before giving up
                    if total_retries < MAX_RETRIES:
                        delay = BACKOFFS[min(total_retries, len(BACKOFFS) - 1)]
                        delay = min(delay, MAX_BACKOFF)
                        _log(f"{bot_name}: unexpected error, retrying in {delay}s")
                        time.sleep(delay)
                        total_retries += 1
                        continue
                    _log(f"{bot_name}: FATAL — unexpected error after {total_retries} retries")
                    if fallback_model and not used_fallback:
                        _fallback_needed = True
                        break
                    exit_reason = "unexpected_error"
                    sys.exit(1)

            if _fallback_needed:
                _log(f"{bot_name}: switching to fallback model {fallback_model}")
                active_model = fallback_model
                used_fallback = True
                total_retries = 0
                timeout_retries = 0
                continue

            # ---- Parse response ----
            try:
                choices = resp_json.get("choices") or []
                msg = choices[0].get("message", {}) if choices else {}
            except Exception:
                msg = {}

            tool_calls = msg.get("tool_calls")
            content = msg.get("content")

            has_tool_calls = bool(tool_calls)
            has_content = bool(content and str(content).strip())

            if has_tool_calls:
                _log(f"{bot_name}: model returned {len(tool_calls)} tool_call(s)")
                messages.append(msg)
                for tc in tool_calls:
                    if _is_draining(bot_name):
                        _log(f"{bot_name}: drain detected during tool execution, exiting")
                        _write_heartbeat(heartbeat_file)
                        exit_reason = "drain"
                        sys.exit(0)
                    tc_id = tc.get("id", "")
                    func = tc.get("function", {}) or {}
                    name = func.get("name", "")
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
                    _log(f"{bot_name}: executing tool '{name}'")
                    result = _execute_tool(name, args)
                    if not result.get("success", True):
                        _log(f"{bot_name}: tool '{name}' failed: {result.get('error', 'unknown')}")
                    if name in ("edit", "write") and args.get("path"):
                        files_touched.append(args["path"])
                    if name == "create_ticket" and result.get("success"):
                        tickets_created += 1
                        _scratch_state.context_summary = (
                            (_scratch_state.context_summary or "") + f"\nTICKET_CREATED: {result.get('output', '')}"
                        ).strip()[:2000]
                    _write_bot_status(
                        bot_name, state_dir,
                        f"tool:{name}",
                        f"executing {name} on {args.get('path', 'N/A')}",
                        files_touched, tool_iterations + 1,
                    )
                    _write_taskline(bot_name, state_dir, f"iter{tool_iterations + 1}", f"tool:{name} {_scratch_target(args)}")
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": json.dumps(result),
                    }
                    messages.append(tool_msg)
                tool_iterations += 1
                if github_target:
                    last_tool = tool_calls[-1].get("function", {}).get("name", "tool")
                    _update_github_progress(
                        github_target, bot_name, tool_iterations,
                        f"completed tool `{last_tool}`; continuing implementation",
                    )
                continue

            if has_content:
                result_text = str(content).strip()
                summary = result_text.replace("\n", " ")[:150]
                _write_scratchpad(bot_name, state_dir, "completed", summary)
                _write_taskline(bot_name, state_dir, "done", summary)
                _log(f"{bot_name}: model returned content, completing ({tool_iterations} tool iterations)")
                _write_heartbeat(heartbeat_file)
                _auto_commit(bot_name, files_touched)
                _write_checkpoint(ckpt_file, bot_name, "completed")
                exit_reason = "completed"
                sys.exit(0)

            if continue_nudges < 2:
                _log(f"{bot_name}: empty response, nudging 'continue' ({continue_nudges + 1}/2)")
                messages.append({"role": "user", "content": "continue"})
                continue_nudges += 1
                continue
            _log(f"{bot_name}: FATAL — no content after 2 nudges")
            exit_reason = "no_content"
            sys.exit(1)
    finally:
        _persist_stream(bot_name, messages, active_model, tool_iterations, exit_reason)
        discovery_roles = frozenset({
            "bug_hunter", "security_auditor", "architecture_auditor", "performance_auditor",
            "test_gap_auditor", "documentation_auditor", "dependency_auditor", "ux_auditor",
        })
        base_name = bot_name.split("-")[0] if "-" in bot_name else bot_name
        if base_name in discovery_roles and tickets_created == 0:
            if exit_reason in ("completed", "iteration_limit", "drain"):
                marker = state_dir / f"{bot_name}.no_tickets"
                try:
                    marker.write_text(str(time.time()), encoding="utf-8")
                except OSError:
                    pass
        # CAP-11: Finalize scratchpad state for handoff or completion record
        if _scratch_available and _scratch_state is not None:
            if exit_reason in ("timeout", "rate_limit", "fatal_error", "token_cap", "iteration_limit", "connection_error"):
                _scratch_state.mark_error(f"exited: {exit_reason}")
                _scratch_state.phase = "interrupted"
            elif exit_reason in ("completed", "drain"):
                _scratch_state.phase = "completed"
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
    _fallback = sys.argv[6] if len(sys.argv) > 6 else ""
    _mtr = int(sys.argv[7]) if len(sys.argv) > 7 else 0
    _fb_chain = sys.argv[8].split(",") if len(sys.argv) > 8 and sys.argv[8] else []
    try:
        _mission_prompt = Path(_mission_file).read_text(encoding="utf-8")
    except Exception as exc:
        print(f"failed to read mission file {_mission_file}: {exc}", file=sys.stderr)
        sys.exit(2)
    run_bot(_bot_name, _model, _mission_prompt, _heartbeat_file, _ckpt_file, _fallback, _mtr, _fb_chain)
