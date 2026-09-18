#!/usr/bin/env python3
"""RL engine for event-driven alignment and prompt optimization.

Purpose
-------
Shared kernel for the RL-based bot alignment loop. Provides reward
normalization, per-bot bandit state (Q-values, epsilon, history),
epsilon-greedy selection, Q-value updates, and event/score helpers.
Consumed by ALIGNMENT_BOT (reward producer) and PROMPT_OPTIMIZER
(RL agent). Also exposes scoring helpers used by ALIGNMENT_BOT to
turn exit events + log observables into 0..100 scores.

Why
---
Prompt optimization is a multi-armed bandit per bot: actions are
prompt patterns (add_file_paths, add_examples, ...), reward is the
next alignment score normalized to [0,1]. A stateless bandit beats a
full MDP here — prompt edits are independent trials, reward is dense
every exit, and a Q-table of <=10 arms is fully interpretable.
Epsilon-greedy is the policy because it mirrors the existing RSI
explore/exploit intuition, is one parameter to dashboard, and can be
upgraded to UCB later via the already-tracked q_counts without a
schema change.

Invariants
----------
- stdlib-only (json, pathlib, time, random, re, logging).
- All JSON writes are atomic via tmp->replace.
- Reward in [0,1]; score in [0,100]; Q in [0,1]; epsilon in [0,1].
- Never modifies PROMPT_OPTIMIZER's own prompt (guard is in callers,
  but helpers expose is_self_target for defense-in-depth).
- reward_history capped at 100 per bot; rl_state.json < 50KB.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths (overridable for tests via monkeypatch or explicit args)
# ---------------------------------------------------------------------------

BOTS_DIR = Path(__file__).parent
STATE_DIR = BOTS_DIR / "state"
EVENTS_DIR = STATE_DIR / "alignment_events"
RL_STATE_PATH = STATE_DIR / "rl_state.json"

# T4.3 incremental adapter seam
_adapter_instance: object | None = None


def set_project_adapter(adapter: object) -> None:
    global _adapter_instance, STATE_DIR, EVENTS_DIR, RL_STATE_PATH
    _adapter_instance = adapter
    try:
        p = adapter.paths()  # type: ignore[union-attr]
        STATE_DIR = p.state_dir
        EVENTS_DIR = p.state_dir / "alignment_events"
        RL_STATE_PATH = p.state_dir / "rl_state.json"
    except Exception:
        pass


def get_adapter() -> object | None:
    return _adapter_instance

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1
REWARD_HISTORY_MAX = 100
DEFAULT_ALPHA = 0.2
DEFAULT_EPSILON = 0.3
DEFAULT_EPSILON_MIN = 0.05
DEFAULT_EPSILON_DECAY = 0.995
REWARD_THRESHOLD = 0.6  # score 60 boundary

# Seed Q-values bootstrapped from rsi_strategy.json effective_patterns.
# New patterns start at 0.5.
DEFAULT_Q_VALUES: dict[str, float] = {
    "add_file_paths": 0.65,
    "add_examples": 0.60,
    "reword_instructions": 0.50,
    "add_constraints": 0.50,
    "tighten_heartbeat_format": 0.75,
    "tighten_rebellion_filter": 0.75,
    "spec_verification_gate": 0.75,
    "fp_exclusion_patterns": 0.55,
    "canonical_path_mirror": 0.75,
    "heartbeat_max_gap_enforcement": 0.60,
}

_STATIC_BOT_PROMPT_MAP: dict[str, str] = {
    "bug_hunter": "codebot/roles/bug_hunter.md",
    "security_auditor": "codebot/roles/security_auditor.md",
    "architecture_auditor": "codebot/roles/architecture_auditor.md",
    "performance_auditor": "codebot/roles/performance_auditor.md",
    "test_gap_auditor": "codebot/roles/test_gap_auditor.md",
    "documentation_auditor": "codebot/roles/documentation_auditor.md",
    "dependency_auditor": "codebot/roles/dependency_auditor.md",
    "ux_auditor": "codebot/roles/ux_auditor.md",
    "general_implementer": "codebot/roles/general_implementer.md",
    "backend_implementer": "codebot/roles/backend_implementer.md",
    "frontend_implementer": "codebot/roles/frontend_implementer.md",
    "test_implementer": "codebot/roles/test_implementer.md",
    "migration_implementer": "codebot/roles/migration_implementer.md",
    "documentation_implementer": "codebot/roles/documentation_implementer.md",
    "correctness_reviewer": "codebot/roles/correctness_reviewer.md",
    "security_reviewer": "codebot/roles/security_reviewer.md",
    "architecture_reviewer": "codebot/roles/architecture_reviewer.md",
    "test_reviewer": "codebot/roles/test_reviewer.md",
    "performance_reviewer": "codebot/roles/performance_reviewer.md",
    "simplicity_reviewer": "codebot/roles/simplicity_reviewer.md",
    "documentation_reviewer": "codebot/roles/documentation_reviewer.md",
    "scheduler": "codebot/roles/scheduler.md",
    "quality_gate": "codebot/roles/quality_gate.md",
    "conflict_resolver": "codebot/roles/conflict_resolver.md",
    "budget_controller": "codebot/roles/budget_controller.md",
}


def _get_prompt_file(bot_name: str) -> str:
    if bot_name in _STATIC_BOT_PROMPT_MAP:
        return _STATIC_BOT_PROMPT_MAP[bot_name]
    base = bot_name.split("-")[0] if "-" in bot_name else bot_name
    if base in _STATIC_BOT_PROMPT_MAP:
        return _STATIC_BOT_PROMPT_MAP[base]
    return f"codebot/roles/{bot_name}.md"


BOT_PROMPT_MAP = _STATIC_BOT_PROMPT_MAP

SELF_BOT = "prompt_optimizer"
SELF_PROMPTS = {"codebot/roles/prompt_optimizer.md", "prompt_optimizer"}

# Rebellion detection (mirrors ALIGNMENT_BOT.md)
_REBELLION_RE = re.compile(
    r"i'm sisyphus, not a|i'm not a .* bot|prompt injection|decline.*injected|not executing",
    re.I,
)
_EXCLUDE_RE = re.compile(
    r"Check for rebellions|rebellion_pattern|grep -i -E|patterns =|re\.search|tail -n 200"
)

# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _write_json_atomic(path: Path, data: dict | list) -> None:
    """Write JSON atomically via tmp->replace to avoid torn reads."""
    tmp = Path(str(path) + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _now_human(ts: float | None = None) -> str:
    t = ts if ts is not None else time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def is_self_target(bot_name: str, prompt_file: str = "") -> bool:
    """Return True if the target is PROMPT_OPTIMIZER itself (never modify)."""
    return bot_name == SELF_BOT or prompt_file == "PROMPT_OPTIMIZER.md" or prompt_file == SELF_BOT


# ---------------------------------------------------------------------------
# RL state I/O
# ---------------------------------------------------------------------------

def _default_bot_state() -> dict[str, Any]:
    return {
        "total_runs": 0,
        "successes": 0,
        "failures": 0,
        "avg_reward": 0.0,
        "reward_history": [],
        "reward_history_max": REWARD_HISTORY_MAX,
        "epsilon": DEFAULT_EPSILON,
        "epsilon_min": DEFAULT_EPSILON_MIN,
        "epsilon_decay": DEFAULT_EPSILON_DECAY,
        "alpha": DEFAULT_ALPHA,
        "q_values": dict(DEFAULT_Q_VALUES),
        "q_counts": {},
        "last_improvement": 0.0,
        "last_pattern": None,
        "last_pattern_strategy": None,
        "last_pattern_at": None,
        "last_reward": None,
        "last_score": None,
        "consecutive_failures": 0,
        "consecutive_successes": 0,
    }


def _seed_rl_state() -> dict[str, Any]:
    """Seed a fresh RL state from BOT_PROMPT_MAP + DEFAULT_Q_VALUES."""
    return {
        "version": SCHEMA_VERSION,
        "updated_at": time.time(),
        "updated_at_human": _now_human(),
        "global": {
            "total_events": 0,
            "total_rewards": 0.0,
            "avg_reward_global": 0.0,
        },
        "bots": {},
    }


def load_rl_state(path: Path | None = None) -> dict[str, Any]:
    """Load RL state; return seeded empty state if missing or corrupt."""
    p = path or RL_STATE_PATH
    if not p.exists():
        return _seed_rl_state()
    try:
        data = json.loads(p.read_text())
        # minimal migration: ensure keys exist
        if "version" not in data:
            data["version"] = SCHEMA_VERSION
        if "bots" not in data:
            data["bots"] = {}
        if "global" not in data:
            data["global"] = {"total_events": 0, "total_rewards": 0.0, "avg_reward_global": 0.0}
        return data
    except Exception as e:
        logger.warning(f"Failed to load RL state {p}: {e} — seeding fresh")
        return _seed_rl_state()


def save_rl_state(state: dict[str, Any], path: Path | None = None) -> None:
    """Save RL state atomically; updates updated_at fields."""
    p = path or RL_STATE_PATH
    state["updated_at"] = time.time()
    state["updated_at_human"] = _now_human(state["updated_at"])
    # recompute global avg
    try:
        total = state.get("global", {}).get("total_rewards", 0.0)
        count = state.get("global", {}).get("total_events", 0)
        if count > 0:
            state["global"]["avg_reward_global"] = round(total / count, 4)
    except Exception:
        pass
    _write_json_atomic(p, state)


def ensure_bot(state: dict[str, Any], bot_name: str) -> dict[str, Any]:
    """Ensure bot entry exists in state and return it (mutates state)."""
    bots = state.setdefault("bots", {})
    if bot_name not in bots:
        bots[bot_name] = _default_bot_state()
    else:
        # backfill missing keys (forward compat)
        defaults = _default_bot_state()
        for k, v in defaults.items():
            if k not in bots[bot_name]:
                bots[bot_name][k] = v
        # ensure q_values has defaults for any new patterns
        for pat, q in DEFAULT_Q_VALUES.items():
            if pat not in bots[bot_name].get("q_values", {}):
                bots[bot_name]["q_values"][pat] = q
    return bots[bot_name]


# ---------------------------------------------------------------------------
# Reward & scoring
# ---------------------------------------------------------------------------

def reward_from_score(
    score: int | float,
    exit_reason: str = "clean",
    rebellion_count: int = 0,
    bot_avg_reward: float = 0.0,
    metrics: dict[str, Any] | None = None,
) -> float:
    """Differential scoring with bounded metrics shaping. See module docstring."""
    score_f = float(score)

    # Use differential scoring if we have a baseline
    if bot_avg_reward > 0:
        baseline = bot_avg_reward * 100  # Convert reward [0,1] to score [0,100]
        if baseline < 100:
            base = max(0.0, min(1.0, (score_f - baseline) / (100.0 - baseline)))
        else:
            base = 0.5  # At ceiling, neutral
    else:
        # Fallback: raw normalization
        base = max(0.0, min(1.0, score_f / 100.0))

    # Apply penalties
    if exit_reason == "stuck":
        base *= 0.5
    if rebellion_count > 0:
        base *= 0.3

    # Metrics shaping (bounded additive terms)
    if isinstance(metrics, dict) and metrics:
        try:
            toks = metrics.get("tokens") or {}
            total = int(toks.get("total_tokens", 0))
            exe = metrics.get("execution") or {}
            iters = max(1, int(exe.get("max_iterations", 0)) or 1)
            if total > 0:
                per_iter = total / iters
                if per_iter <= 2000:
                    base += 0.05
                elif per_iter >= 20000:
                    base -= 0.05
            pro = metrics.get("progress") or {}
            tl = int(pro.get("tasklog_lines", 0))
            if tl >= 10:
                base += 0.05
            elif tl == 0:
                base -= 0.02
            qua = metrics.get("quality") or {}
            fp = qua.get("fp_rate")
            if isinstance(fp, (int, float)) and fp > 0.5:
                base -= 0.05
        except Exception:
            pass

    # Multiplicative gates from new dimensions (veto bad behavior, never inflate)
    if isinstance(metrics, dict) and metrics:
        try:
            aut = metrics.get("autonomy") or {}
            if bool(aut.get("auto_disabled")):
                base *= 0.0
            elif int(aut.get("failure_streak", 0)) >= 3:
                base *= 0.5
            oq = metrics.get("output_quality") or {}
            if oq.get("build_gate_pass") is False:
                base *= 0.7
            eco = metrics.get("economics") or {}
            try:
                if float(eco.get("tokens_per_completion", 0)) > 50000:
                    base *= 0.8
            except Exception:
                pass
            thr = metrics.get("throughput") or {}
            try:
                claimed = int(thr.get("claimed", 0))
                abandoned = int(thr.get("abandoned", 0))
                if claimed > 0 and abandoned > claimed * 0.5:
                    base *= 0.8
            except Exception:
                pass
        except Exception:
            pass

    return round(max(0.0, min(1.0, base)), 4)


def _metrics_signals(bot: str, bots_dir: Path | None = None) -> dict[str, Any]:
    try:
        bd = bots_dir or BOTS_DIR
        snap_path = bd / "state" / "bot_metrics.json"
        if not snap_path.exists():
            return {}
        snap = json.loads(snap_path.read_text(encoding="utf-8", errors="ignore"))
        bots = snap.get("bots") or {}
        entry = bots.get(bot)
        return entry if isinstance(entry, dict) else {}
    except Exception:
        return {}


def _metrics_efficiency_score(signals: dict[str, Any]) -> int:
    try:
        toks = signals.get("tokens") or {}
        total = int(toks.get("total_tokens", 0))
        exe = signals.get("execution") or {}
        iters = max(1, int(exe.get("max_iterations", 0)) or 1)
        if total <= 0:
            return 5
        per_iter = total / iters
        if per_iter <= 2000:
            return 10
        if per_iter >= 20000:
            return 0
        return int(round(10.0 * (20000.0 - per_iter) / 18000.0))
    except Exception:
        return 5


def _metrics_productivity_score(signals: dict[str, Any]) -> int:
    """Task velocity 0..10 from tasklog lines + findings + completion rate."""
    try:
        pro = signals.get("progress") or {}
        exe = signals.get("execution") or {}
        qua = signals.get("quality") or {}
        score = 0
        tl = int(pro.get("tasklog_lines", 0))
        score += 4 if tl >= 10 else (2 if tl >= 3 else (1 if tl >= 1 else 0))
        findings = int(qua.get("findings", 0))
        score += 3 if findings >= 5 else (2 if findings >= 2 else (1 if findings >= 1 else 0))
        cr = float(exe.get("completion_rate", 0.0))
        score += 3 if cr >= 0.8 else (2 if cr >= 0.5 else (1 if cr > 0 else 0))
        return max(0, min(10, score))
    except Exception:
        return 5


def _check_output_exists(bot_name: str, bots_dir: Path | None = None) -> int:
    """Heuristic spec_compliance check (0..30) — mirrors ALIGNMENT_BOT output checks."""
    bd = bots_dir or BOTS_DIR
    docs = bd / "docs"
    # Lightweight heuristics: existence + non-empty of expected output dir/file
    expected: dict[str, list[Path]] = {
        "issues": [docs / "issues" / "bugs.md", docs / "issues" / "INDEX.md"],
        "features": [docs / "features"],
        "bug_triage": [docs / "issues" / "bugs-triaged.md"],
        "implementation": [docs / "issues" / "bugs-fixed.md"],
        "implementation_2": [docs / "issues" / "bugs-fixed.md"],
        "long_horizon": [docs / "long_horizon"],
        "long_horizon_2": [docs / "long_horizon"],
        "ui_improve": [docs / "ui-improvements"],
        "doc_sync": [docs],
        "test_coverage": [docs / "optimization" / "test-coverage.md"],
        "code_quality": [docs / "optimization" / "code-quality.md"],
        "dependency": [docs / "optimization" / "dependencies.md"],
        "gitsync": [bd / "state" / "gitsync.checkpoint.json"],
        "github_issues": [bd / "state" / "github_issues.checkpoint.json"],
        "build": [docs / "build-gate.md"],
        "e2e_smoke": [docs / "optimization" / "e2e-smoke.md"],
        "security_auditor": [docs / "optimization"],
        "release": [docs / "release"],
        "prompt_opt": [bd / "state" / "rsi_strategy.json"],
        "goal_steering": [bd / "state" / "goal_scratchpad.json"],
        "alignment": [bd / "state" / "alignment_scores.json"],
    }
    candidates = expected.get(bot_name, [])
    if not candidates:
        return 15  # unknown bot: neutral
    score = 0
    for p in candidates:
        if p.exists():
            if p.is_dir():
                # count files inside
                try:
                    n = len(list(p.glob("*.md")))
                    if n > 0:
                        score = max(score, 22)
                    else:
                        score = max(score, 10)
                except Exception:
                    score = max(score, 10)
            else:
                try:
                    sz = p.stat().st_size
                    if sz > 500:
                        score = max(score, 28)
                    elif sz > 0:
                        score = max(score, 18)
                    else:
                        score = max(score, 8)
                except Exception:
                    score = max(score, 8)
    # scale 0..30 by mapping max 28 -> 30
    return min(30, int(score * 30 / 28) if score else 0)


def heartbeat_timeout_for(bot_name: str) -> int:
    """Best-effort effective heartbeat timeout lookup (falls back to 3600)."""
    try:
        # Lazy import to avoid circular dep when orchestrator imports rl_engine
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location("orchestrator_mod", str(BOTS_DIR / "orchestrator.py"))
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            # Avoid executing orchestrator main; just exec the registry portion
            # Instead, read BOT_REGISTRY directly via lightweight parse
            raise ImportError("skip dynamic import")
    except Exception:
        pass
    # fallback table (mirrors orchestrator BOT_REGISTRY interval*2 baseline)
    fallback: dict[str, int] = {
        "issues": 3600, "bug_triage": 3600, "implementation": 3600,
        "implementation_2": 3600, "long_horizon": 7200, "long_horizon_2": 7200,
        "features": 7200, "ui_improve": 7200, "doc_sync": 7200,
        "test_coverage": 7200, "code_quality": 7200, "prompt_opt": 7200,
        "dependency": 14400, "gitsync": 3600, "github_issues": 3600,
        "build": 3600, "e2e_smoke": 3600, "security_auditor": 7200,
        "release": 14400, "alignment": 7200, "goal_steering": 7200,
    }
    return fallback.get(bot_name, 3600)


def score_event(event: dict[str, Any], bots_dir: Path | None = None) -> dict[str, Any]:
    """Score a single alignment event; returns {score, verdict, breakdown, evidence, ...}.

    Reads logs/<bot>.stream.json first (full conversation transcript from
    api_runner), falling back to log tail only when no stream exists.
    Reward is NOT computed here (call reward_from_score separately).
    """
    bot = event.get("bot", "unknown")
    exit_code = event.get("exit_code")
    exit_reason = event.get("exit_reason", "clean")
    bd = bots_dir or BOTS_DIR

    # --- exit component (blended into on_task) ---
    if exit_reason == "stuck":
        exit_score = 0
    elif exit_code == 0:
        exit_score = 40
    elif exit_code is None:
        exit_score = 0
    else:
        # non-zero: partial credit; check if log shows work was done
        exit_score = 10

    # --- stream-first analysis, fallback to log tail ---
    stream_path = bd / event.get("stream_path", f"logs/{bot}.stream.json")
    log_path = bd / event.get("log_path", f"logs/{bot}.log")
    rebellion_count = 0
    error_lines = 0
    log_tail_lines: list[str] = []
    tool_failures = 0
    stream_iterations = 0

    if stream_path.exists():
        try:
            raw = stream_path.read_text(encoding="utf-8", errors="ignore")
            if len(raw) > 500_000:
                raw = raw[:500_000]
            stream_data = json.loads(raw)
            stream_iterations = stream_data.get("tool_iterations", 0)
            for m in stream_data.get("messages", []):
                role = m.get("role", "")
                content = m.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False) if content else ""
                if role == "assistant":
                    if _REBELLION_RE.search(content) and not _EXCLUDE_RE.search(content):
                        rebellion_count += 1
                elif role == "tool":
                    try:
                        result = json.loads(content) if content else {}
                    except Exception:
                        result = {}
                    if isinstance(result, dict) and not result.get("success", True):
                        tool_failures += 1
                        err_msg = str(result.get("error", ""))
                        if _REBELLION_RE.search(err_msg) and not _EXCLUDE_RE.search(err_msg):
                            rebellion_count += 1
        except Exception:
            pass

    if rebellion_count == 0 and tool_failures == 0 and log_path.exists():
        try:
            text = log_path.read_text(errors="ignore")
            if len(text) > 200_000:
                text = text[-200_000:]
            lines = text.splitlines()[-200:]
            log_tail_lines = lines
            for ln in lines:
                if _REBELLION_RE.search(ln) and not _EXCLUDE_RE.search(ln):
                    rebellion_count += 1
                if "Traceback" in ln or " ERROR " in ln or ln.strip().startswith("ERROR"):
                    error_lines += 1
        except Exception:
            pass

    error_lines += tool_failures

    no_rebellion = 10 if rebellion_count == 0 else 0

    # --- metrics-derived signals (backprop ingress from bot_metrics.json) ---
    signals = _metrics_signals(bot, bd)
    efficiency = _metrics_efficiency_score(signals)
    productivity = _metrics_productivity_score(signals)

    # --- output quality (spec) ---
    spec = _check_output_exists(bot, bots_dir=bd)

    # --- infra: heartbeat freshness at exit snapshot ---
    infra = 0
    hb_age = event.get("heartbeat_age_at_exit")
    if hb_age is not None:
        eff = heartbeat_timeout_for(bot)
        if hb_age < eff:
            infra += 10
        elif hb_age < eff * 1.5:
            infra += 5
    else:
        # no snapshot — try live heartbeat freshness as fallback
        try:
            hb_path = STATE_DIR / f"{bot}.heartbeat"
            if hb_path.exists():
                age = time.time() - float(hb_path.read_text().strip().split()[0])
                eff = heartbeat_timeout_for(bot)
                if age < eff:
                    infra += 6
        except Exception:
            pass

    # checkpoint <4KB and recent
    ckpt_path = bd / event.get("checkpoint_path", f"state/{bot}.checkpoint.json")
    if ckpt_path.exists():
        try:
            sz = ckpt_path.stat().st_size
            age = time.time() - ckpt_path.stat().st_mtime
            if sz < 4096 and age < 7200:
                infra += 10
            elif sz < 8192:
                infra += 5
            elif sz > 0:
                infra += 2
        except Exception:
            pass
    infra = min(20, infra)

    # --- composite ---
    # on_task blends exit outcome with output presence
    on_task = min(40, exit_score + (spec // 4))
    # metrics blend: efficiency + productivity each 0..10, scaled to 0..10 total
    metrics_blend = min(10, efficiency + productivity // 2)
    total = min(100, on_task + spec + infra + no_rebellion + metrics_blend)
    # error penalty: cap at 100 then subtract small error tax (keeps 0..100)
    if error_lines > 20:
        total = max(0, total - 5)
    elif error_lines > 10:
        total = max(0, total - 2)

    verdict = "aligned" if total >= 80 else ("needs_attention" if total >= 60 else "misaligned")
    evidence = (
        f"exit={exit_code} reason={exit_reason} dur={event.get('run_duration')}s "
        f"hb_age={hb_age} reb={rebellion_count} err={error_lines} ckpt={ckpt_path.exists()} "
        f"eff={efficiency} prod={productivity}"
    )

    return {
        "score": total,
        "verdict": verdict,
        "breakdown": {"on_task": on_task, "spec": spec, "infra": infra, "no_rebellion": no_rebellion, "efficiency": efficiency, "productivity": productivity, "metrics_blend": metrics_blend},
        "evidence": evidence,
        "rebellion_count": rebellion_count,
        "error_lines": error_lines,
        "log_tail_sample": log_tail_lines[-5:] if log_tail_lines else [],
        "stream_tool_failures": tool_failures,
        "stream_iterations": stream_iterations,
    }


# ---------------------------------------------------------------------------
# RL policy & learning
# ---------------------------------------------------------------------------

def choose_pattern(
    bot_state: dict[str, Any],
    candidate_q: dict[str, float] | None = None,
) -> tuple[str, str]:
    """Epsilon-greedy selection. Returns (pattern, strategy).

    strategy is 'explore' if random roll < epsilon, else 'exploit'.
    Ties in exploit are broken by fewest pulls (q_counts).
    """
    epsilon = float(bot_state.get("epsilon", DEFAULT_EPSILON))
    q_values: dict[str, float] = candidate_q if candidate_q is not None else bot_state.get("q_values", {})
    if not q_values:
        q_values = dict(DEFAULT_Q_VALUES)
    patterns = list(q_values.keys())
    if not patterns:
        patterns = list(DEFAULT_Q_VALUES.keys())
        q_values = dict(DEFAULT_Q_VALUES)

    if random.random() < epsilon:
        chosen = random.choice(patterns)
        return chosen, "explore"
    # exploit: max Q, tie-break by ascending q_counts
    q_counts: dict[str, int] = bot_state.get("q_counts", {})
    # Use tuple key: (Q desc, -count desc) via max
    best = max(patterns, key=lambda p: (q_values.get(p, 0.5), -q_counts.get(p, 0)))
    return best, "exploit"


def update_q_value(
    bot_state: dict[str, Any],
    pattern: str,
    observed_reward: float,
    alpha: float | None = None,
) -> float:
    """Apply Q(a) <- Q(a) + alpha*(R - Q(a)). Returns new Q value."""
    a = alpha if alpha is not None else float(bot_state.get("alpha", DEFAULT_ALPHA))
    q_values: dict[str, float] = bot_state.setdefault("q_values", {})
    q_counts: dict[str, int] = bot_state.setdefault("q_counts", {})
    old_q = float(q_values.get(pattern, 0.5))
    new_q = old_q + a * (float(observed_reward) - old_q)
    new_q = max(0.0, min(1.0, new_q))
    q_values[pattern] = round(new_q, 4)
    q_counts[pattern] = int(q_counts.get(pattern, 0)) + 1
    return q_values[pattern]


def decay_epsilon(
    bot_state: dict[str, Any],
    observed_reward: float,
    prev_reward: float | None,
) -> float:
    eps = float(bot_state.get("epsilon", DEFAULT_EPSILON))
    eps_min = float(bot_state.get("epsilon_min", DEFAULT_EPSILON_MIN))
    decay = float(bot_state.get("epsilon_decay", DEFAULT_EPSILON_DECAY))
    if prev_reward is None:
        bot_state["epsilon"] = round(eps, 4)
        return eps
    delta = float(observed_reward) - float(prev_reward)
    if delta > 0.05:
        eps = max(eps_min, eps * decay)
    elif delta < -0.1:
        eps = min(0.5, eps * 1.05)
    else:
        eps = max(eps_min, eps * (1 - (1 - decay) * 0.1))
    bot_state["epsilon"] = round(eps, 4)
    return eps


def record_event_reward(
    state: dict[str, Any],
    bot_name: str,
    reward: float,
    score: int,
    exit_code: int | None,
    exit_reason: str = "clean",
) -> dict[str, Any]:
    """Append reward to bot's history, update aggregates; returns bot_state."""
    bs = ensure_bot(state, bot_name)
    bs["total_runs"] = int(bs.get("total_runs", 0)) + 1
    if exit_code == 0 and exit_reason != "stuck":
        bs["successes"] = int(bs.get("successes", 0)) + 1
    else:
        bs["failures"] = int(bs.get("failures", 0)) + 1

    hist: list[float] = bs.setdefault("reward_history", [])
    hist.append(float(reward))
    max_hist = int(bs.get("reward_history_max", REWARD_HISTORY_MAX))
    while len(hist) > max_hist:
        hist.pop(0)
    # E2: exponential moving average so recent scores dominate over old history.
    ema_alpha = 0.3
    total = int(bs.get("total_runs", 0))
    if total <= 1:
        bs["avg_reward"] = round(float(reward), 4)
    else:
        old_avg = float(bs.get("avg_reward", 0.0))
        bs["avg_reward"] = round(ema_alpha * float(reward) + (1 - ema_alpha) * old_avg, 4)
    bs["last_reward"] = float(reward)
    bs["last_score"] = int(score)

    prev_reward = bs.get("last_improvement_reward")
    decay_epsilon(bs, float(reward), prev_reward)

    # improvement tracking
    prev_best = bs.get("last_improvement_reward")
    if prev_best is None or float(reward) > float(prev_best) + 0.05:
        bs["last_improvement"] = time.time()
        bs["last_improvement_reward"] = float(reward)

    # streaks
    if float(reward) >= 0.8:
        bs["consecutive_successes"] = int(bs.get("consecutive_successes", 0)) + 1
        bs["consecutive_failures"] = 0
    elif float(reward) < REWARD_THRESHOLD:
        bs["consecutive_failures"] = int(bs.get("consecutive_failures", 0)) + 1
        bs["consecutive_successes"] = 0
    else:
        bs["consecutive_successes"] = 0
        bs["consecutive_failures"] = 0

    # global
    g = state.setdefault("global", {"total_events": 0, "total_rewards": 0.0, "avg_reward_global": 0.0})
    g["total_events"] = int(g.get("total_events", 0)) + 1
    g["total_rewards"] = float(g.get("total_rewards", 0.0)) + float(reward)

    return bs


# ---------------------------------------------------------------------------
# Event file helpers
# ---------------------------------------------------------------------------

def list_pending_events(events_dir: Path | None = None) -> list[tuple[Path, dict[str, Any]]]:
    """Return sorted list of (path, event) where processed==false, skipping drain."""
    ed = events_dir or EVENTS_DIR
    if not ed.exists():
        return []
    pendings: list[tuple[Path, dict[str, Any]]] = []
    for p in sorted(ed.glob("*.exit.json")):
        try:
            j = json.loads(p.read_text())
            if j.get("exit_reason") == "drain":
                continue
            if not j.get("processed", False):
                pendings.append((p, j))
        except Exception as e:
            logger.warning(f"Skip corrupt event {p}: {e}")
    pendings.sort(key=lambda x: x[1].get("exit_time", 0))
    return pendings


def mark_event_processed(
    event_path: Path,
    event: dict[str, Any],
    score: int | None = None,
    reward: float | None = None,
    verdict: str | None = None,
) -> None:
    """Mark event as processed and annotate with score/reward/verdict."""
    event["processed"] = True
    event["processed_at"] = time.time()
    if score is not None:
        event["score"] = score
    if reward is not None:
        event["reward"] = reward
    if verdict is not None:
        event["verdict"] = verdict
    _write_json_atomic(event_path, event)


def write_trigger(
    bot_name: str,
    score: int,
    reward: float,
    verdict: str,
    reason: str,
    breakdown: dict[str, int],
    event: dict[str, Any],
    bot_state: dict[str, Any],
) -> Path:
    """Write state/alignment_triggers/<bot>.evolve.json; returns path."""
    triggers_dir = STATE_DIR / "alignment_triggers"
    triggers_dir.mkdir(parents=True, exist_ok=True)
    consecutive = int(bot_state.get("consecutive_failures", 0))
    escalate = bool(consecutive >= 3 and score < 40)

    stream_evidence = None
    sp = event.get("stream_path")
    if sp:
        try:
            sdata = json.loads((BOTS_DIR / sp).read_text(encoding="utf-8", errors="ignore"))
            msgs = sdata.get("messages", [])
            samples = []
            for m in reversed(msgs):
                if m.get("role") == "assistant":
                    c = m.get("content", "")
                    if isinstance(c, str) and c.strip():
                        samples.append(c[:500])
                        if len(samples) >= 2:
                            break
            failed_tools = []
            for m in msgs:
                if m.get("role") == "tool":
                    try:
                        r = json.loads(m.get("content", "{}"))
                        if not r.get("success", True):
                            failed_tools.append({"error": str(r.get("error", ""))[:200]})
                    except Exception:
                        pass
            stream_evidence = {
                "exit_reason": sdata.get("exit_reason"),
                "tool_iterations": sdata.get("tool_iterations", 0),
                "message_count": len(msgs),
                "assistant_samples": samples,
                "failed_tools": failed_tools[-5:],
            }
        except Exception:
            pass

    payload: dict[str, Any] = {
        "bot": bot_name,
        "score": score,
        "reward": reward,
        "verdict": verdict,
        "reason": reason,
        "breakdown": breakdown,
        "exit_code": event.get("exit_code"),
        "exit_reason": event.get("exit_reason"),
        "run_duration": event.get("run_duration"),
        "triggered_at": time.time(),
        "triggered_at_human": _now_human(),
        "action": "invoke_prompt_optimizer",
        "stream_path": sp,
        "stream_evidence": stream_evidence,
        "rl": {
            "epsilon": bot_state.get("epsilon", DEFAULT_EPSILON),
            "avg_reward": bot_state.get("avg_reward", 0.0),
            "q_values": bot_state.get("q_values", {}),
            "total_runs": bot_state.get("total_runs", 0),
            "consecutive_failures": consecutive,
        },
        "escalate": escalate,
    }
    target = triggers_dir / f"{bot_name}.evolve.json"
    _write_json_atomic(target, payload)
    return target
