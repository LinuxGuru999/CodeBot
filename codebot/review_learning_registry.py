#!/usr/bin/env python3
"""Review Learning Registry — single registration point for review-learning subsystem.

Purpose
-------
Owns lifecycle of the review-learning subsystem. Separates two independent
controls required by spec fixes #3/#4:

  review_learning.observation = true|false   (collect/store raw events & episodes)
  review_learning.policy_mode = OFF|SHADOW|ASSIST|CONTROL  (whether to score/act)

Turning off learned influence must NOT destroy training data. Policy mode is
explicit configuration — artifact presence never auto-promotes to SHADOW.

Why a single registry (fix #2)
------------------------------
v1 plan scattered eager top-level ``from codebot.rl_* import ...`` across
orchestrator, ticket_engine, health_loop, review_gate, etc. so ruff/vulture
wouldn't delete the subsystem. That couples startup, risks circular imports,
and makes DISABLED not really disabled. This registry is the ONE import
site that owns the subsystem. ``codebot_bootstrap`` wires it once at startup;
every other module imports *this* registry, not individual rl_* modules.
Static analysis should treat the registry import as the keep-alive signal
(``# keep: review-learning-registry`` marker).

Invariants
----------
- stdlib-only.
- Default: observation=true, policy_mode=OFF (explicit SHADOW required).
- DISABLED is an alias for observation=false + policy OFF.
- Per-project scoping via state_dir string key.
- Never mutates ticket state.
- Top-level imports of rl_event_log / rl_failure_taxonomy keep the event
  surface alive via this registry without scattering imports.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

# Keep the event surface alive via the registry (fix #2: single registration point
# owns imports, not scattered eager imports).
try:
    import codebot.rl_event_log as _rl_event_log  # noqa: F401  # keep: review-learning-registry
    import codebot.rl_failure_taxonomy as _rl_failure_taxonomy  # noqa: F401  # keep: review-learning-registry
except ImportError:
    _rl_event_log = None  # type: ignore[assignment]
    _rl_failure_taxonomy = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

VALID_POLICY_MODES = frozenset({"OFF", "SHADOW", "ASSIST", "CONTROL"})
# DISABLED is an alias; ADVISORY/ACTIVE accepted as synonyms for ASSIST/CONTROL
_ALIAS_MAP = {
    "DISABLED": "OFF",
    "ADVISORY": "ASSIST",
    "ACTIVE": "CONTROL",
}

# ---------------------------------------------------------------------------
# Per-project state (keyed by str(state_dir))
# ---------------------------------------------------------------------------

_observation_enabled: dict[str, bool] = {}
_policy_mode: dict[str, str] = {}
_registered: dict[str, bool] = {}

_adapter_instance: object | None = None


def set_project_adapter(adapter: object | None) -> None:
    global _adapter_instance
    _adapter_instance = adapter
    # Propagate to owned rl modules if available
    if _rl_event_log is not None:
        try:
            setter = getattr(_rl_event_log, "set_project_adapter", None)
            if setter is not None:
                setter(adapter)
        except Exception:
            pass


def get_adapter() -> object | None:
    return _adapter_instance


def _key(state_dir: Path | str | None) -> str:
    if state_dir is None:
        return "__default__"
    return str(Path(state_dir))


def _normalize_mode(mode: str | None) -> str:
    if not mode or not isinstance(mode, str):
        return "OFF"
    m = mode.strip().upper()
    if m in _ALIAS_MAP:
        m = _ALIAS_MAP[m]
    if m not in VALID_POLICY_MODES:
        logger.warning("review_learning_registry: unknown policy_mode %r -> OFF", mode)
        return "OFF"
    return m


def _config_path(state_dir: Path | str | None) -> Path | None:
    if state_dir is None:
        return None
    p = Path(state_dir)
    # state_dir is typically .codebot/state; config lives at .codebot/review_learning.yaml
    if p.name == "state":
        return p.parent / "review_learning.yaml"
    # fallback: state_dir itself or its parent .codebot
    candidate = p / "review_learning.yaml"
    if candidate.exists():
        return candidate
    if (p.parent / "review_learning.yaml").exists():
        return p.parent / "review_learning.yaml"
    return p.parent / "review_learning.yaml" if p.name == "state" else candidate


def _read_file_config(state_dir: Path | str | None) -> dict[str, Any]:
    cfg_path = _config_path(state_dir)
    if cfg_path is None or not cfg_path.exists():
        return {}
    try:
        text = cfg_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, Any] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        k, v = stripped.split(":", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'").strip()
        out[k] = v
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def set_observation_enabled(state_dir: Path | str | None, enabled: bool) -> None:
    _observation_enabled[_key(state_dir)] = bool(enabled)
    _registered[_key(state_dir)] = True


def is_observation_enabled(state_dir: Path | str | None = None) -> bool:
    k = _key(state_dir)
    if k in _observation_enabled:
        return _observation_enabled[k]
    # file fallback
    file_cfg = _read_file_config(state_dir)
    if "observation" in file_cfg:
        raw = str(file_cfg["observation"]).strip().lower()
        if raw in ("true", "1", "yes", "on", "enabled"):
            return True
        if raw in ("false", "0", "no", "off", "disabled"):
            return False
    if "review_learning.observation" in file_cfg:
        raw = str(file_cfg["review_learning.observation"]).strip().lower()
        if raw in ("true", "1", "yes", "on"):
            return True
        if raw in ("false", "0", "no", "off"):
            return False
    # Legacy single key "mode: DISABLED" implies observation false
    mode_raw = file_cfg.get("mode") or file_cfg.get("review_learning.mode") or file_cfg.get("policy_mode")
    if isinstance(mode_raw, str) and mode_raw.strip().upper() == "DISABLED":
        return False
    return True  # default true per fix #3


def set_policy_mode(state_dir: Path | str | None, mode: str) -> None:
    norm = _normalize_mode(mode)
    _policy_mode[_key(state_dir)] = norm
    _registered[_key(state_dir)] = True
    # DISABLED alias also disables observation
    if isinstance(mode, str) and mode.strip().upper() == "DISABLED":
        _observation_enabled[_key(state_dir)] = False


def get_policy_mode(state_dir: Path | str | None = None) -> str:
    k = _key(state_dir)
    if k in _policy_mode:
        return _policy_mode[k]
    file_cfg = _read_file_config(state_dir)
    # Prefer explicit policy_mode keys
    for fk in ("policy_mode", "review_learning.policy_mode", "review_learning_policy_mode", "mode", "review_learning.mode"):
        if fk in file_cfg:
            raw = str(file_cfg[fk]).strip()
            # DISABLED maps to OFF
            if raw.upper() == "DISABLED":
                return "OFF"
            norm = _normalize_mode(raw)
            return norm
    return "OFF"  # default OFF per fix #4 (explicit)


def set_review_learning_config(
    state_dir: Path | str | None,
    *,
    observation: bool | None = None,
    policy_mode: str | None = None,
) -> None:
    if observation is not None:
        set_observation_enabled(state_dir, observation)
    if policy_mode is not None:
        set_policy_mode(state_dir, policy_mode)


def get_review_learning_config(state_dir: Path | str | None = None) -> dict[str, Any]:
    return {
        "observation": is_observation_enabled(state_dir),
        "policy_mode": get_policy_mode(state_dir),
    }


def is_enabled(state_dir: Path | str | None = None) -> bool:
    """Whether policy has any influence (SHADOW/ASSIST/CONTROL)."""
    return get_policy_mode(state_dir) in ("SHADOW", "ASSIST", "CONTROL")


def is_shadow_enabled(state_dir: Path | str | None = None) -> bool:
    return get_policy_mode(state_dir) == "SHADOW"


def can_run_shadow(state_dir: Path | str | None = None) -> bool:
    """Shadow CAN run only if observation true and mode is SHADOW/ASSIST/CONTROL
    and a compatible artifact exists (checked by caller). This helper only
    checks mode gates, not artifact compatibility."""
    if not is_observation_enabled(state_dir):
        return False
    return get_policy_mode(state_dir) in ("SHADOW", "ASSIST", "CONTROL")


def register_review_learning(
    state_dir: Path | str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Idempotent registration for one project.

    Reads explicit config (dict or file) and populates per-project state.
    Returns the resolved config. Never auto-promotes to SHADOW on artifact
    presence (fix #4).
    """
    k = _key(state_dir)
    if k in _registered and config is None:
        return get_review_learning_config(state_dir)
    if config is not None:
        obs = config.get("observation")
        if obs is None:
            obs = config.get("review_learning.observation")
        mode = config.get("policy_mode")
        if mode is None:
            mode = config.get("review_learning.policy_mode") or config.get("mode")
        if obs is not None:
            # Coerce string bools
            if isinstance(obs, str):
                obs_bool = obs.strip().lower() in ("true", "1", "yes", "on", "enabled")
            else:
                obs_bool = bool(obs)
            set_observation_enabled(state_dir, obs_bool)
        if mode is not None:
            set_policy_mode(state_dir, str(mode))
        # Ensure defaults applied if not provided
        if k not in _observation_enabled:
            _observation_enabled[k] = True
        if k not in _policy_mode:
            _policy_mode[k] = "OFF"
    else:
        # Load from file if present, else defaults
        file_cfg = _read_file_config(state_dir)
        if file_cfg:
            # observation
            if "observation" in file_cfg:
                raw = str(file_cfg["observation"]).lower()
                set_observation_enabled(state_dir, raw in ("true", "1", "yes", "on"))
            elif "review_learning.observation" in file_cfg:
                raw = str(file_cfg["review_learning.observation"]).lower()
                set_observation_enabled(state_dir, raw in ("true", "1", "yes", "on"))
            # mode
            for fk in ("policy_mode", "review_learning.policy_mode", "mode", "review_learning.mode"):
                if fk in file_cfg:
                    set_policy_mode(state_dir, str(file_cfg[fk]))
                    break
        if k not in _observation_enabled:
            _observation_enabled[k] = True
        if k not in _policy_mode:
            _policy_mode[k] = "OFF"
    _registered[k] = True
    return get_review_learning_config(state_dir)


def reset_registry() -> None:
    """Test helper: clear all per-project state."""
    _observation_enabled.clear()
    _policy_mode.clear()
    _registered.clear()
