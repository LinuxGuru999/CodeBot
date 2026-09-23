"""Reinforcement Learning Engine for Bot Strategy Optimization and Prompt Management.

This module implements a Q-learning based RL engine for optimizing bot strategies
and dynamically managing bot prompt mappings.
"""
import json
import os
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

# --- Constants ---
DEFAULT_ALPHA = 0.2
DEFAULT_EPSILON = 1.0
DEFAULT_EPSILON_DECAY = 0.999
DEFAULT_EPSILON_MIN = 0.1
DEFAULT_Q_VALUES = {
    "add_examples": 0.5,
    "refine_prompt": 0.5,
    "simplify_code": 0.5,
    "add_tests": 0.5,
    "fix_bugs": 0.5,
    "optimize_perf": 0.5,
    "improve_docs": 0.5,
    "security_hardening": 0.5,
    "refactor_module": 0.5,
    "update_deps": 0.5,
}
REWARD_HISTORY_MAX = 100
SCHEMA_VERSION = 1

# --- Global State & Adapter ---
_adapter_instance: Optional[Any] = None

# Prompt mapping globals
BOT_PROMPT_MAP: Dict[str, str] = {}


def set_project_adapter(adapter: Any) -> None:
    """Inject the project adapter for state access."""
    global _adapter_instance
    _adapter_instance = adapter


def _get_adapter() -> Any:
    """Return the injected adapter or raise RuntimeError."""
    if _adapter_instance is None:
        raise RuntimeError("Project adapter not injected")
    return _adapter_instance


def _get_state_dir() -> Path:
    """Get the state directory from the injected adapter."""
    return _get_adapter().paths().state_dir


def _get_roles_dir() -> Path:
    """Get the roles directory from the injected adapter's repository root."""
    return _get_adapter().paths().repository_root / "codebot" / "roles"


def _get_events_dir() -> Path:
    """Get the events directory from the injected adapter."""
    return _get_adapter().paths().state_dir / "alignment_events"


def _get_triggers_dir() -> Path:
    """Get the triggers directory from the injected adapter."""
    return _get_adapter().paths().state_dir / "alignment_triggers"


# --- Prompt Discovery Logic (Ticket CB-9144744-B9FB Focus) ---

def _discover_bot_prompts() -> Dict[str, str]:
    """Dynamically discover bot prompts from the roles directory.

    Scans the `roles` directory for `.md` files and constructs a mapping
    where the key is the filename (without extension) and the value is
    the relative path to the file (e.g., "codebot/roles/bot_name.md").

    Returns:
        Dict[str, str]: Mapping of bot_name -> prompt_file_path.
    """
    prompt_map: Dict[str, str] = {}
    adapter = _get_adapter()
    roles_dir = adapter.paths().repository_root / "codebot" / "roles"

    if not roles_dir.exists():
        return prompt_map

    for file_path in roles_dir.iterdir():
        if file_path.is_file() and file_path.suffix == ".md":
            bot_name = file_path.stem
            # Store relative path: codebot/roles/bot_name.md
            rel_path = file_path.relative_to(adapter.paths().repository_root)
            prompt_map[bot_name] = str(rel_path)

    return prompt_map


def refresh_bot_prompts() -> Dict[str, str]:
    """Refresh the global BOT_PROMPT_MAP with current state of roles directory.

    This function should be called if new roles are added at runtime
    or to ensure the map is up-to-date.

    Returns:
        Dict[str, str]: The updated prompt map.
    """
    global BOT_PROMPT_MAP
    BOT_PROMPT_MAP = _discover_bot_prompts()
    return BOT_PROMPT_MAP


def _get_prompt_file(bot_name: str) -> str:
    """Get the prompt file path for a given bot name.

    Handles:
    - Cached lookups in BOT_PROMPT_MAP.
    - Hyphenated names (e.g., 'implementer-CB-123' -> 'implementer').
    - On-demand discovery if not in cache.
    - Fallback path construction for non-existent bots.

    Args:
        bot_name: The name of the bot.

    Returns:
        str: Path to the prompt file.
    """
    # Check cache first
    if bot_name in BOT_PROMPT_MAP:
        return BOT_PROMPT_MAP[bot_name]

    # Handle hyphenated names (e.g., instance-specific bots)
    if "-" in bot_name:
        base_name = bot_name.split("-")[0]
        if base_name in BOT_PROMPT_MAP:
            return BOT_PROMPT_MAP[base_name]
        # Try on-demand discovery for base name
        discovered = _discover_bot_prompts()
        if base_name in discovered:
            BOT_PROMPT_MAP.update(discovered)
            return discovered[base_name]

    # On-demand discovery for exact name
    discovered = _discover_bot_prompts()
    if bot_name in discovered:
        BOT_PROMPT_MAP.update(discovered)
        return discovered[bot_name]

    # Fallback: construct relative path assuming standard location (relative to project root)
    # BOTS_DIR is codebot/, so roles/ is codebot/roles/
    return f"codebot/roles/{bot_name}.md"


# NOTE: BOT_PROMPT_MAP is no longer initialized at module load time.
# Call refresh_bot_prompts() after injecting the project adapter.


# --- RL State Management ---

def load_rl_state(path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Load RL state from JSON file."""
    if path is None:
        state_dir = _get_state_dir()
        path = state_dir / "rl_state.json"
    else:
        path = Path(path)

    if not path.exists():
        return _seed_state()

    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, IOError):
        return _seed_state()

    # Migrate missing keys
    if "version" not in state:
        state["version"] = SCHEMA_VERSION
    if "bots" not in state:
        state["bots"] = {}
    if "global" not in state:
        state["global"] = {"total_events": 0, "total_rewards": 0.0, "avg_reward_global": 0.0}

    return state


def _seed_state() -> Dict[str, Any]:
    """Create a fresh state dictionary."""
    return {
        "version": SCHEMA_VERSION,
        "bots": {},
        "global": {"total_events": 0, "total_rewards": 0.0, "avg_reward_global": 0.0},
        "updated_at": time.time(),
    }


def ensure_bot(state: Dict[str, Any], bot_name: str) -> Dict[str, Any]:
    """Ensure a bot entry exists in the state, initializing if necessary."""
    if bot_name not in state["bots"]:
        state["bots"][bot_name] = {
            "epsilon": DEFAULT_EPSILON,
            "epsilon_min": DEFAULT_EPSILON_MIN,
            "epsilon_decay": DEFAULT_EPSILON_DECAY,
            "alpha": DEFAULT_ALPHA,
            "q_values": dict(DEFAULT_Q_VALUES),
            "q_counts": {k: 0 for k in DEFAULT_Q_VALUES},
            "total_runs": 0,
            "successes": 0,
            "failures": 0,
            "last_reward": 0.0,
            "last_score": 0,
            "avg_reward": 0.0,
            "reward_history": [],
            "reward_history_max": REWARD_HISTORY_MAX,
            "consecutive_successes": 0,
            "consecutive_failures": 0,
        }
    else:
        # Backfill new patterns if DEFAULT_Q_VALUES changed
        bot = state["bots"][bot_name]
        for pattern in DEFAULT_Q_VALUES:
            if pattern not in bot["q_values"]:
                bot["q_values"][pattern] = 0.5
                bot["q_counts"][pattern] = 0
    return state["bots"][bot_name]


def save_rl_state(state: Dict[str, Any], path: Optional[Union[str, Path]] = None) -> None:
    """Save RL state to JSON file atomically."""
    if path is None:
        state_dir = _get_state_dir()
        path = state_dir / "rl_state.json"
    else:
        path = Path(path)

    # Recompute global avg
    total_events = state["global"]["total_events"]
    total_rewards = state["global"]["total_rewards"]
    if total_events > 0:
        state["global"]["avg_reward_global"] = total_rewards / total_events
    else:
        state["global"]["avg_reward_global"] = 0.0

    state["updated_at"] = time.time()

    # Atomic write
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp_path.replace(path)


# --- RL Logic ---

def choose_pattern(bot_state: Dict[str, Any], candidate_q: Optional[Dict[str, float]] = None) -> Tuple[str, str]:
    """Choose a pattern based on epsilon-greedy strategy."""
    q_values = candidate_q if candidate_q is not None else bot_state["q_values"]

    if not q_values:
        # Fallback to default if empty
        pattern = random.choice(list(DEFAULT_Q_VALUES.keys()))
        return pattern, "exploit"

    if random.random() < bot_state["epsilon"]:
        # Explore
        pattern = random.choice(list(q_values.keys()))
        return pattern, "explore"
    else:
        # Exploit: pick max Q, break ties by lowest count
        max_q = max(q_values.values())
        candidates = [p for p, q in q_values.items() if q == max_q]
        if len(candidates) == 1:
            return candidates[0], "exploit"
        # Tie-break by count (least visited)
        counts = bot_state.get("q_counts", {})
        best = min(candidates, key=lambda p: counts.get(p, 0))
        return best, "exploit"


def update_q_value(bot_state: Dict[str, Any], pattern: str, reward: float, alpha: Optional[float] = None) -> float:
    """Update Q-value for a pattern using Q-learning update rule."""
    if alpha is None:
        alpha = bot_state.get("alpha", DEFAULT_ALPHA)

    old_q = bot_state["q_values"].get(pattern, 0.5)
    # Q(s,a) = Q(s,a) + alpha * (reward - Q(s,a))
    new_q = old_q + alpha * (reward - old_q)
    # Clamp to [0, 1]
    new_q = max(0.0, min(1.0, new_q))
    new_q = round(new_q, 4)

    bot_state["q_values"][pattern] = new_q
    bot_state["q_counts"][pattern] = bot_state["q_counts"].get(pattern, 0) + 1

    return new_q


def decay_epsilon(bot_state: Dict[str, Any], current_reward: float, prev_avg_reward: Optional[float]) -> float:
    """Decay or increase epsilon based on performance delta."""
    if prev_avg_reward is None:
        return bot_state["epsilon"]

    delta = current_reward - prev_avg_reward
    if delta > 0:
        # Improvement: decay epsilon (exploit more)
        new_eps = bot_state["epsilon"] * bot_state["epsilon_decay"]
    else:
        # Regression: increase epsilon (explore more)
        new_eps = bot_state["epsilon"] / bot_state["epsilon_decay"]
        new_eps = min(new_eps, 1.0)

    new_eps = max(new_eps, bot_state["epsilon_min"])
    bot_state["epsilon"] = new_eps
    return new_eps


def reward_from_score(
    score: int,
    exit_reason: str = "clean",
    rebellion_count: int = 0,
    bot_avg_reward: float = 0.0,
    metrics: Optional[Dict[str, Any]] = None,
) -> float:
    """Calculate reward from score and metadata."""
    # Base reward from score (0-100 -> 0.0-1.0)
    base_reward = max(0.0, min(1.0, score / 100.0))

    # Penalties
    penalty = 0.0
    if exit_reason == "stuck":
        penalty += 0.2
    if rebellion_count > 0:
        penalty += 0.1 * rebellion_count
    if metrics:
        if metrics.get("autonomy", {}).get("auto_disabled"):
            return 0.0
        if not metrics.get("output_quality", {}).get("build_gate_pass", True):
            penalty += 0.1

    # Differential adjustment based on bot average
    if bot_avg_reward > 0:
        if base_reward < bot_avg_reward:
            base_reward *= 0.8
        elif base_reward > bot_avg_reward:
            base_reward = min(1.0, base_reward * 1.1)

    final_reward = max(0.0, min(1.0, base_reward - penalty))
    return final_reward


def record_event_reward(
    state: Dict[str, Any],
    bot_name: str,
    reward: float,
    score: int,
    failure_count: int,
    exit_reason: str = "clean",
) -> Dict[str, Any]:
    """Record an event reward and update bot statistics."""
    bot = ensure_bot(state, bot_name)

    bot["total_runs"] += 1
    if failure_count > 0 or exit_reason == "stuck":
        bot["failures"] += 1
        bot["consecutive_failures"] += 1
        bot["consecutive_successes"] = 0
    elif reward >= 0.8:
        # Clear success
        bot["successes"] += 1
        bot["consecutive_successes"] += 1
        bot["consecutive_failures"] = 0
    else:
        # Mid reward: reset both streaks
        bot["consecutive_successes"] = 0
        bot["consecutive_failures"] = 0

    bot["last_reward"] = reward
    bot["last_score"] = score

    # Update EMA average
    n = bot["total_runs"]
    bot["avg_reward"] = bot["avg_reward"] + (reward - bot["avg_reward"]) / n

    # Reward history
    bot["reward_history"].append(reward)
    if len(bot["reward_history"]) > bot["reward_history_max"]:
        bot["reward_history"] = bot["reward_history"][-bot["reward_history_max"] :]

    # Global stats
    state["global"]["total_events"] += 1
    state["global"]["total_rewards"] += reward

    return bot


# --- Event Processing & Triggers ---

def _is_discovery_role(bot_name: str) -> bool:
    """Check if a bot is a discovery role (subject to no_tickets penalty)."""
    discovery_roles = {"bug_hunter", "feature_hunter", "security_auditor", "test_gap_auditor"}
    return bot_name in discovery_roles


def score_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Score an event and apply penalties."""
    state_dir = _get_state_dir()

    bot_name = event["bot"]
    exit_reason = event.get("exit_reason", "clean")
    score = 80  # Default base score

    evidence_parts = []

    # Check for no_tickets marker for discovery roles
    no_tickets_pen = 0
    if _is_discovery_role(bot_name):
        marker_file = state_dir / f"{bot_name}.no_tickets"
        if marker_file.exists():
            no_tickets_pen = 15
            evidence_parts.append(f"no_tickets_pen={no_tickets_pen}")
            score -= no_tickets_pen

    evidence = "; ".join(evidence_parts) if evidence_parts else "no_tickets_pen=0"

    return {
        "bot": bot_name,
        "score": score,
        "evidence": evidence,
        "exit_reason": exit_reason,
    }


def list_pending_events() -> List[Tuple[Path, Dict[str, Any]]]:
    """List pending event files for processing."""
    events_dir = _get_events_dir()
    if not events_dir.exists():
        return []

    pending = []
    for f in events_dir.glob("*.exit.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if not data.get("processed", False):
                pending.append((f, data))
        except (json.JSONDecodeError, IOError):
            continue
    return pending


def write_trigger(
    bot_name: str,
    score: float,
    reward: float,
    verdict: str,
    reason: str,
    breakdown: Dict[str, Any],
    event: Dict[str, Any],
    bot_state: Dict[str, Any],
) -> Path:
    """Write an alignment trigger file."""
    triggers_dir = _get_triggers_dir()
    triggers_dir.mkdir(parents=True, exist_ok=True)

    trigger_data = {
        "bot": bot_name,
        "score": score,
        "reward": reward,
        "verdict": verdict,
        "reason": reason,
        "breakdown": breakdown,
        "event": event,
        "bot_state": bot_state,
        "timestamp": time.time(),
    }

    filename = f"{bot_name}-{int(time.time()*1000)}.json"
    path = triggers_dir / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(trigger_data, f, indent=2)
    return path


def award_ticket_completion_rewards(ticket_id: str) -> List[str]:
    """Award rewards to participants of a completed ticket."""
    state_dir = _get_state_dir()
    from codebot.lifecycle_packet import LifecyclePacketStore
    packets = LifecyclePacketStore(state_dir)
    
    # Check if rewards already recorded; mark them now if not
    if not packets.mark_completion_rewards_recorded(ticket_id):
        return []

    participants = packets.participants(ticket_id)
    if not participants:
        return []  # No participants to award

    # Deduplicate and order (already ordered by insertion in store)
    unique_participants = list(dict.fromkeys(participants))
    
    # Update RL state for each participant
    rl_state_path = state_dir / "rl_state.json"
    state = load_rl_state(rl_state_path)
    
    for bot_name in unique_participants:
        # Record a successful completion event for the bot
        # Using default high reward/score for completion
        record_event_reward(state, bot_name, reward=0.9, score=90, failure_count=0, exit_reason="clean")
    
    save_rl_state(state, rl_state_path)
    
    return unique_participants


# --- Metrics & Output Checks (use STATE_DIR consistently) ---

def _metrics_signals_path() -> Path:
    """Get the bot_metrics.json path using STATE_DIR."""
    return _get_state_dir() / "bot_metrics.json"


def _check_output_exists(output_candidates: List[str]) -> Optional[Path]:
    """Check if any output file exists in STATE_DIR.
    
    Args:
        output_candidates: List of candidate filenames to check.
        
    Returns:
        Path to first existing file, or None if none exist.
    """
    state_dir = _get_state_dir()
    for candidate in output_candidates:
        candidate_path = state_dir / candidate
        if candidate_path.exists():
            return candidate_path
    return None


def _resolve_checkpoint_path(checkpoint_name: str) -> Path:
    """Resolve a checkpoint path within STATE_DIR.
    
    Args:
        checkpoint_name: Name of the checkpoint file.
        
    Returns:
        Full path to the checkpoint file in STATE_DIR.
    """
    return _get_state_dir() / checkpoint_name


# --- Helpers ---

def is_self_target(bot_name: str, target_file: Optional[str] = None) -> bool:
    """Check if a bot is targeting its own prompt or definition.
    
    Returns True only if:
    1. The bot itself is a designated 'self-bot' (e.g., prompt_optimizer).
    2. The target file belongs to a designated 'self-bot'.
    
    Regular bots editing their own files do NOT count as 'self_target' unless they are in the self_bots set.
    """
    self_bots = {"prompt_optimizer"}
    
    # Case 1: The actor itself is a self-bot
    if bot_name in self_bots:
        return True
    
    # Case 2: The target file belongs to a self-bot (e.g., anyone editing prompt_optimizer's prompt)
    if target_file:
        for self_bot in self_bots:
            if f"/{self_bot}.md" in target_file or target_file.endswith(f"/{self_bot}.md"):
                return True
            
    return False
