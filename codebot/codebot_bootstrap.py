#!/usr/bin/env python3
"""CodeBot bootstrap — loads project adapter and wires it into all core modules.

Purpose
-------
Entry point for adapter injection. Called once at startup before the
orchestrator begins scheduling bots. Discovers the ProjectAdapter from
the .codebot/project.yaml config, instantiates the correct adapter class,
and injects it into every core module that has a set_project_adapter() seam.

Why
---
T4.3 requires incremental adapter injection without modifying the orchestrator's
main() entry point. This bootstrap module is called by start_botnet.sh or
directly before orchestrator startup, ensuring all path resolution goes through
the adapter from the first tick.

Invariants
----------
- stdlib-only (importlib, pathlib, json)
- Idempotent: calling wire_adapter() multiple times is safe
- Fails open: if no adapter found, core modules use default paths
- Never modifies source code; only mutates runtime module globals
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("codebot_bootstrap")

# Core modules that accept adapter injection via set_project_adapter()
_ADAPTER_MODULES = [
    "codebot.orchestrator",
    "codebot.api_runner",
    "codebot.rl_engine",
    "codebot.token_budget",
    "codebot.prompt_gateway",
]

_wired = False


def discover_adapter_class(project_root: Path) -> Any | None:
    config_path = project_root / ".codebot" / "project.yaml"
    if not config_path.exists():
        logger.info("no .codebot/project.yaml found — using default paths")
        return None
    project_name = _read_project_name(config_path)
    adapter_candidates = [
        f"codebot.{project_name}_adapter",
        f"{project_name}_adapter",
    ]
    if project_name == "monitor":
        adapter_candidates.append("codebot.monitor_adapter")
    for adapter_module in adapter_candidates:
        class_name = "".join(w.capitalize() for w in adapter_module.rsplit(".", 1)[-1].replace("_", " ").split()).replace(" ", "")
        if not class_name.endswith("Adapter"):
            class_name += "Adapter"
        try:
            mod = importlib.import_module(adapter_module)
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if isinstance(attr, type) and attr_name != "ProjectAdapter":
                    try:
                        from codebot.project_adapter import ProjectAdapter
                        if issubclass(attr, ProjectAdapter) and attr is not ProjectAdapter:
                            instance = _try_instantiate(attr, project_root)
                            if instance is not None:
                                logger.info("loaded adapter %s.%s", adapter_module, attr_name)
                                return instance
                    except ImportError:
                        pass
        except ImportError:
            continue
        except Exception as e:
            logger.debug("adapter candidate %s failed: %s", adapter_module, e)
            continue
    logger.info("no project adapter found for '%s' — using default paths", project_name)
    return None


def _read_project_name(config_path: Path) -> str:
    try:
        text = config_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("name:"):
                val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                return val.replace("-", "_").replace(" ", "_").lower()
    except Exception:
        pass
    return "unknown"


def _try_instantiate(cls: type, project_root: Path) -> Any | None:
    try:
        return cls(project_root)
    except TypeError:
        pass
    try:
        return cls()
    except TypeError:
        pass
    return None


def wire_adapter(adapter: Any) -> None:
    global _wired
    if _wired:
        return
    for module_path in _ADAPTER_MODULES:
        try:
            mod = importlib.import_module(module_path)
            setter = getattr(mod, "set_project_adapter", None)
            if setter is not None:
                setter(adapter)
                logger.info("injected adapter into %s", module_path)
            else:
                logger.debug("%s has no set_project_adapter() — skipping", module_path)
        except ImportError:
            logger.debug("module %s not importable — skipping", module_path)
        except Exception as e:
            logger.warning("failed to inject adapter into %s: %s", module_path, e)
    _wired = True


def bootstrap(project_root: Path | None = None) -> Any | None:
    root = project_root or Path.cwd()
    adapter = discover_adapter_class(root)
    if adapter is not None:
        wire_adapter(adapter)
        errors = adapter.validate_project()
        if errors:
            for err in errors:
                logger.warning("project validation: %s", err)
        else:
            logger.info("project %s validated successfully", adapter.project_name())
    return adapter


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    result = bootstrap()
    if result:
        print(f"CodeBot bootstrapped with adapter: {result.project_name()}")
    else:
        print("CodeBot bootstrapped with default paths (no adapter)")
