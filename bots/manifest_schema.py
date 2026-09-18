"""Bot manifest validation and loading.

Purpose
-------
Provides strict validation for bot manifests (bots/manifests/<name>.json)
and loading helpers used by the orchestrator and batch scheduler.
Consumers call validate_manifest, load_manifest, or load_all_manifests.

Why
---
Manifests replace the hardcoded BOT_REGISTRY so each bot's schedule,
inputs, outputs and behavior are explicit JSON. Strict stdlib-only
validation prevents typos and type errors from silently misconfiguring
a bot. Rejecting unknown fields is the typo guard.

Invariants
----------
- stdlib json only, no yaml
- validate_manifest returns (ok, errors) without raising for schema violations
- load_manifest raises ValueError with path + field on invalid manifests
- load_all_manifests raises ValueError on first invalid manifest
"""

import json
from pathlib import Path
from typing import Any

_REQUIRED_FIELDS = {
    "name",
    "kind",
    "prompt_file",
    "model",
    "runner",
    "enabled",
    "interval_seconds",
    "heartbeat_timeout",
    "tier_priority",
    "max_restarts",
    "clean_exit_wait",
    "session_timeout",
    "input",
    "output",
    "noop_cap",
}

_OPTIONAL_FIELDS = {
    "complexity_filter",
    "shard",
    "scratchpad",
    "batch_tier",
    "noop_counter_file",
}

_ALLOWED_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS

_KIND_ENUM = {"scan", "queue", "command"}
_RUNNER_ENUM = {"opencode", "api"}
_BATCH_TIER_ENUM = {"standard", "high-limit"}


def _err(msg: str) -> str:
    return msg


def validate_manifest(path_or_dict: Any) -> tuple[bool, list[str]]:
    """Validate a manifest dict or a path to a JSON manifest.

    Args:
        path_or_dict: Either a dict with manifest fields or a path
            (str/Path) to a JSON file.

    Returns:
        (ok, errors) where ok is True iff manifest is valid.
    """
    # Resolve path_or_dict to dict
    source_label = ""
    data: Any = None
    if isinstance(path_or_dict, dict):
        data = path_or_dict
    elif isinstance(path_or_dict, (str, Path)):
        p = Path(path_or_dict)
        source_label = str(p)
        # If the string looks like a file path and file exists, read it
        # Also treat non-existent path strings that point to .json as path attempt
        # Otherwise if it's a string dict repr? not needed
        if p.exists():
            try:
                text = p.read_text(encoding="utf-8")
                data = json.loads(text)
            except json.JSONDecodeError as e:
                return False, [_err(f"{source_label}: invalid JSON: {e}")]
            except OSError as e:
                return False, [_err(f"{source_label}: read error: {e}")]
        else:
            # Check if caller passed a path that doesn't exist -> treat as error
            # But to support dict-like test passing string, we check if it looks like path
            # Heuristic: if string contains '/' or ends with .json, treat as missing file
            s = str(path_or_dict)
            if "/" in s or s.endswith(".json"):
                return False, [_err(f"{source_label}: file not found")]
            # Otherwise it's not a dict and not a valid path -> invalid
            return False, [_err(f"{source_label}: expected dict or path to JSON file")]
    else:
        return False, [_err(f"validate_manifest: expected dict or path, got {type(path_or_dict).__name__}")]

    # data must be dict
    if not isinstance(data, dict):
        return False, [_err(f"{source_label}: manifest must be a JSON object")]

    errors: list[str] = []

    # Unknown fields
    for key in data:
        if key not in _ALLOWED_FIELDS:
            errors.append(_err(f"{source_label}: unknown field '{key}'" if source_label else f"unknown field '{key}'"))

    # Missing required fields
    for field in _REQUIRED_FIELDS:
        if field not in data:
            errors.append(_err(f"{source_label}: missing required field '{field}'" if source_label else f"missing required field '{field}'"))

    # If missing required, still continue to validate present fields for richer errors
    # Field-level validations
    # name
    if "name" in data:
        v = data["name"]
        if not isinstance(v, str) or not v.strip():
            errors.append(_err(f"{source_label}: field 'name' must be non-empty string" if source_label else "field 'name' must be non-empty string"))
    # kind
    if "kind" in data:
        v = data["kind"]
        if not isinstance(v, str) or v not in _KIND_ENUM:
            errors.append(_err(f"{source_label}: field 'kind' must be one of scan|queue|command" if source_label else "field 'kind' must be one of scan|queue|command"))
    # prompt_file
    if "prompt_file" in data:
        v = data["prompt_file"]
        if not isinstance(v, str) or not v.strip():
            errors.append(_err(f"{source_label}: field 'prompt_file' must be non-empty string" if source_label else "field 'prompt_file' must be non-empty string"))
    # model
    if "model" in data:
        v = data["model"]
        if not isinstance(v, str) or not v.strip():
            errors.append(_err(f"{source_label}: field 'model' must be non-empty string" if source_label else "field 'model' must be non-empty string"))
    # runner
    if "runner" in data:
        v = data["runner"]
        if not isinstance(v, str) or v not in _RUNNER_ENUM:
            errors.append(_err(f"{source_label}: field 'runner' must be one of opencode|api" if source_label else "field 'runner' must be one of opencode|api"))
    # enabled
    if "enabled" in data:
        v = data["enabled"]
        if not isinstance(v, bool):
            errors.append(_err(f"{source_label}: field 'enabled' must be bool" if source_label else "field 'enabled' must be bool"))
    # interval_seconds
    if "interval_seconds" in data:
        v = data["interval_seconds"]
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            errors.append(_err(f"{source_label}: field 'interval_seconds' must be int > 0" if source_label else "field 'interval_seconds' must be int > 0"))
    # heartbeat_timeout
    if "heartbeat_timeout" in data:
        v = data["heartbeat_timeout"]
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            errors.append(_err(f"{source_label}: field 'heartbeat_timeout' must be int > 0" if source_label else "field 'heartbeat_timeout' must be int > 0"))
    # tier_priority
    if "tier_priority" in data:
        v = data["tier_priority"]
        if isinstance(v, bool) or not isinstance(v, int):
            errors.append(_err(f"{source_label}: field 'tier_priority' must be int" if source_label else "field 'tier_priority' must be int"))
    # max_restarts
    if "max_restarts" in data:
        v = data["max_restarts"]
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            errors.append(_err(f"{source_label}: field 'max_restarts' must be int > 0" if source_label else "field 'max_restarts' must be int > 0"))
    # clean_exit_wait
    if "clean_exit_wait" in data:
        v = data["clean_exit_wait"]
        if not isinstance(v, bool):
            errors.append(_err(f"{source_label}: field 'clean_exit_wait' must be bool" if source_label else "field 'clean_exit_wait' must be bool"))
    # session_timeout
    if "session_timeout" in data:
        v = data["session_timeout"]
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            errors.append(_err(f"{source_label}: field 'session_timeout' must be int > 0" if source_label else "field 'session_timeout' must be int > 0"))
    # input
    if "input" in data:
        v = data["input"]
        if not isinstance(v, list) or len(v) == 0:
            errors.append(_err(f"{source_label}: field 'input' must be non-empty list" if source_label else "field 'input' must be non-empty list"))
        else:
            for idx, entry in enumerate(v):
                if not isinstance(entry, dict):
                    errors.append(_err(f"{source_label}: field 'input[{idx}]' must be object" if source_label else f"field 'input[{idx}]' must be object"))
                    continue
                if "path" not in entry or not isinstance(entry["path"], str) or not entry["path"].strip():
                    errors.append(_err(f"{source_label}: field 'input[{idx}].path' must be non-empty string" if source_label else f"field 'input[{idx}].path' must be non-empty string"))
                if "type" not in entry or not isinstance(entry["type"], str) or not entry["type"].strip():
                    errors.append(_err(f"{source_label}: field 'input[{idx}].type' must be non-empty string" if source_label else f"field 'input[{idx}].type' must be non-empty string"))
                # filter optional if present must be str
                if "filter" in entry and not isinstance(entry["filter"], str):
                    errors.append(_err(f"{source_label}: field 'input[{idx}].filter' must be string" if source_label else f"field 'input[{idx}].filter' must be string"))
                # reject unknown keys in input entry
                for ek in entry:
                    if ek not in {"path", "type", "filter"}:
                        errors.append(_err(f"{source_label}: unknown field 'input[{idx}].{ek}'" if source_label else f"unknown field 'input[{idx}].{ek}'"))
    # output
    if "output" in data:
        v = data["output"]
        if not isinstance(v, list) or len(v) == 0:
            errors.append(_err(f"{source_label}: field 'output' must be non-empty list" if source_label else "field 'output' must be non-empty list"))
        else:
            for idx, entry in enumerate(v):
                if not isinstance(entry, dict):
                    errors.append(_err(f"{source_label}: field 'output[{idx}]' must be object" if source_label else f"field 'output[{idx}]' must be object"))
                    continue
                if "path" not in entry or not isinstance(entry["path"], str) or not entry["path"].strip():
                    errors.append(_err(f"{source_label}: field 'output[{idx}].path' must be non-empty string" if source_label else f"field 'output[{idx}].path' must be non-empty string"))
                if "type" not in entry or not isinstance(entry["type"], str) or not entry["type"].strip():
                    errors.append(_err(f"{source_label}: field 'output[{idx}].type' must be non-empty string" if source_label else f"field 'output[{idx}].type' must be non-empty string"))
                for ek in entry:
                    if ek not in {"path", "type"}:
                        errors.append(_err(f"{source_label}: unknown field 'output[{idx}].{ek}'" if source_label else f"unknown field 'output[{idx}].{ek}'"))
    # noop_cap
    if "noop_cap" in data:
        v = data["noop_cap"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            errors.append(_err(f"{source_label}: field 'noop_cap' must be int >= 0" if source_label else "field 'noop_cap' must be int >= 0"))
        else:
            # noop_counter_file conditional
            has_counter = "noop_counter_file" in data
            if v > 0:
                if not has_counter:
                    errors.append(_err(f"{source_label}: field 'noop_counter_file' required when 'noop_cap' > 0" if source_label else "field 'noop_counter_file' required when 'noop_cap' > 0"))
                else:
                    cv = data["noop_counter_file"]
                    if not isinstance(cv, str) or not cv.strip():
                        errors.append(_err(f"{source_label}: field 'noop_counter_file' must be non-empty string" if source_label else "field 'noop_counter_file' must be non-empty string"))
            else:  # v == 0
                if has_counter:
                    cv = data["noop_counter_file"]
                    if not isinstance(cv, str) or not cv.strip():
                        errors.append(_err(f"{source_label}: field 'noop_counter_file' must be non-empty string" if source_label else "field 'noop_counter_file' must be non-empty string"))
    else:
        # if noop_cap missing already errors, but check stray counter without cap?
        if "noop_counter_file" in data:
            cv = data["noop_counter_file"]
            if not isinstance(cv, str) or not cv.strip():
                errors.append(_err(f"{source_label}: field 'noop_counter_file' must be non-empty string" if source_label else "field 'noop_counter_file' must be non-empty string"))
    # Optional fields
    if "complexity_filter" in data:
        v = data["complexity_filter"]
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            errors.append(_err(f"{source_label}: field 'complexity_filter' must be list[str]" if source_label else "field 'complexity_filter' must be list[str]"))
        elif len(v) == 0:
            errors.append(_err(f"{source_label}: field 'complexity_filter' must be non-empty list[str]" if source_label else "field 'complexity_filter' must be non-empty list[str]"))
    if "shard" in data:
        v = data["shard"]
        if not isinstance(v, str) or not v.strip():
            errors.append(_err(f"{source_label}: field 'shard' must be non-empty string" if source_label else "field 'shard' must be non-empty string"))
    if "scratchpad" in data:
        v = data["scratchpad"]
        if not isinstance(v, bool):
            errors.append(_err(f"{source_label}: field 'scratchpad' must be bool" if source_label else "field 'scratchpad' must be bool"))
    if "batch_tier" in data:
        v = data["batch_tier"]
        if not isinstance(v, str) or v not in _BATCH_TIER_ENUM:
            errors.append(_err(f"{source_label}: field 'batch_tier' must be one of standard|high-limit" if source_label else "field 'batch_tier' must be one of standard|high-limit"))
    # noop_counter_file already handled, but validate type if present alone
    if "noop_counter_file" in data and "noop_cap" not in data:
        # already validated above when cap missing
        pass
    # Also need to catch case where noop_counter_file present but cap is 0? We already handled type.
    # Ensure source_label appears in errors when validating dict from path? Already.

    ok = len(errors) == 0
    return ok, errors


def load_manifest(path: str | Path) -> dict[str, Any]:
    """Load and validate a single manifest file.

    Args:
        path: Path to JSON manifest file.

    Returns:
        Manifest dict if valid.

    Raises:
        ValueError: If file is missing, not JSON, or fails validation.
                    Message includes manifest path + field.
    """
    p = Path(path)
    if not p.exists():
        raise ValueError(f"{p}: file not found")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"{p}: read error: {e}") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{p}: invalid JSON: {e}") from e
    ok, errors = validate_manifest(data)
    # Re-tag errors with path if they don't already contain it
    if not ok:
        # ensure path in message
        tagged = []
        for e in errors:
            if str(p) not in e:
                tagged.append(f"{p}: {e}")
            else:
                tagged.append(e)
        raise ValueError("; ".join(tagged))
    # Also re-validate with path label to include path in future errors if needed
    # Return data as loaded
    return data


def load_all_manifests(dir: str | Path = "bots/manifests") -> dict[str, dict[str, Any]]:
    """Load all manifests in a directory.

    Args:
        dir: Directory containing <name>.json manifests.

    Returns:
        Dict mapping bot name to manifest dict.

    Raises:
        ValueError: If any manifest is invalid (includes path + field).
    """
    d = Path(dir)
    if not d.exists():
        raise ValueError(f"{d}: manifest directory not found")
    if not d.is_dir():
        raise ValueError(f"{d}: not a directory")
    manifests: dict[str, dict[str, Any]] = {}
    # sorted for deterministic order
    for p in sorted(d.glob("*.json")):
        m = load_manifest(p)
        name = m.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{p}: field 'name' must be non-empty string")
        if name in manifests:
            raise ValueError(f"{p}: duplicate manifest name '{name}'")
        manifests[name] = m
    return manifests
