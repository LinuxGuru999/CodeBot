#!/usr/bin/env python3
"""Role prompt loader — assembles final LLM prompts from role templates + project context.

Purpose
-------
Reads a portable role prompt from codebot/roles/{role_name}.md and injects
project-specific context from the ProjectAdapter, producing the final system
prompt sent to the LLM. This replaces the old *_BOT.md files which had
Monitor knowledge baked in.

Why
---
CODEBOT-ROADMAP.md §2.H requires ROLE + TASK + MODEL + TOOL_POLICY composition.
The role template defines behavior; the adapter defines project specifics.
This separation means the same bug_hunter.md works on any repository.

Invariants
----------
- stdlib-only
- Role templates are never modified at runtime
- Project context is appended, never interpolated into template text
- Missing role file degrades gracefully (returns minimal prompt)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("role_prompt")

_ROLES_DIR = Path(__file__).parent / "roles"

_template_cache: dict[str, tuple[float, str]] = {}
_context_cache: dict[int, str] = {}


def clear_role_prompt_cache() -> None:
    _template_cache.clear()
    _context_cache.clear()

# Maps legacy Monitor bot names to CodeBot role names.
# Used during migration so existing BOT_REGISTRY entries resolve correctly.
LEGACY_ROLE_MAP: dict[str, str] = {
    "issues": "bug_hunter",
    "bug_triage": "ticket_triager",
    "features": "architecture_auditor",
    "decomposer": "decomposer",
    "code_quality": "simplicity_reviewer",
    "security_auditor": "security_auditor",
    "test_coverage": "test_gap_auditor",
    "build": "quality_gate",
    "e2e_smoke": "correctness_reviewer",
    "doc_sync": "documentation_implementer",
    "dependency": "dependency_auditor",
    "goal_steering": "goal_steering",
    "ui_improve": "ux_auditor",
    "release": "release_manager",
    "prompt_opt": "prompt_optimizer",
    "alignment": "alignment_scorer",
    "github_bot": "github_mirror",
    "gitsync": "git_sync",
    "worker-1": "general_implementer",
    "worker-2": "general_implementer",
    "worker-3": "general_implementer",
    "worker-4": "backend_implementer",
    "worker-5": "backend_implementer",
    "worker-6": "backend_implementer",
    "worker-7": "frontend_implementer",
    "worker-8": "test_implementer",
    "worker-9": "migration_implementer",
    "worker-10": "documentation_implementer",
    "worker-11": "general_implementer",
    "worker-12": "general_implementer",
}


def resolve_role_name(bot_name: str) -> str:
    return LEGACY_ROLE_MAP.get(bot_name, bot_name)


def load_role_template(role_name: str) -> str:
    safe_name = role_name.replace("/", "_").replace("\\", "_")
    path = _ROLES_DIR / f"{safe_name}.md"
    if path.exists():
        try:
            mtime = path.stat().st_mtime
            cached = _template_cache.get(safe_name)
            if cached is not None and cached[0] == mtime:
                return cached[1]
            text = path.read_text(encoding="utf-8")
            _template_cache[safe_name] = (mtime, text)
            return text
        except OSError as e:
            logger.warning("failed to read role template %s: %s", path, e)
    logger.debug("no role template found for '%s', using minimal prompt", role_name)
    return f"# Role: {role_name}\n\nYou are {role_name}, an agent in the CodeBot platform.\n"


def build_project_context(adapter: Any) -> str:
    if adapter is None:
        return ""
    key = id(adapter)
    cached = _context_cache.get(key)
    if cached is not None:
        return cached
    lines: list[str] = []
    lines.append("---")
    lines.append("")
    lines.append("## Project Context")
    lines.append("")
    try:
        lines.append(f"**Project**: {adapter.project_name()}")
    except Exception:
        pass
    try:
        p = adapter.paths()
        lines.append(f"**Repository root**: `{p.repository_root}`")
        lines.append(f"**State directory**: `{p.state_dir}`")
        lines.append(f"**Logs directory**: `{p.logs_dir}`")
        lines.append(f"**Docs directory**: `{p.docs_dir}`")
        lines.append(f"**Constitution**: `{p.constitution_file}`")
    except Exception:
        pass
    try:
        comps = adapter.components()
        if comps:
            lines.append("")
            lines.append("### Components")
            lines.append("")
            lines.append("| Name | Path | Type | Language | Description |")
            lines.append("|------|------|------|----------|-------------|")
            for c in comps:
                lines.append(f"| {c.name} | `{c.path}` | {c.component_type} | {c.language} | {c.description} |")
    except Exception:
        pass
    try:
        tc = adapter.test_config()
        lines.append("")
        lines.append("### Testing")
        lines.append("")
        lines.append(f"- **Framework**: {tc.framework}")
        lines.append(f"- **Command**: `{tc.test_command}`")
        lines.append(f"- **Directories**: {', '.join(tc.test_directories)}")
    except Exception:
        pass
    try:
        dp = adapter.dependency_policy()
        lines.append("")
        lines.append("### Dependencies")
        lines.append("")
        lines.append(f"- **Policy**: {dp.policy}")
        if dp.allowed_third_party:
            allowed = ", ".join(d.get("name", "?") for d in dp.allowed_third_party)
            lines.append(f"- **Allowed third-party**: {allowed}")
    except Exception:
        pass
    try:
        ac = adapter.autonomy_config()
        lines.append("")
        lines.append("### Autonomy")
        lines.append("")
        lines.append(f"- **Level**: {ac.level}")
        if ac.human_approval_required_for:
            lines.append(f"- **Human approval required**: {', '.join(ac.human_approval_required_for)}")
        if ac.autonomous_allowed_for:
            lines.append(f"- **Autonomous allowed**: {', '.join(ac.autonomous_allowed_for)}")
    except Exception:
        pass
    lines.append("")
    lines.append("### Critical Rules")
    lines.append("")
    lines.append("1. Read `.codebot/constitution.md` before making any changes. Protected invariants cannot be weakened.")
    lines.append("2. All output/state paths MUST be absolute, derived from the project contract above. NEVER use bare relative paths.")
    lines.append("3. Follow the Same-PR rule: when you change code, update the docs it touches in the same change.")
    lines.append("4. Write heartbeat after every atomic task. The orchestrator monitors this file — no update = stuck = restart.")
    lines.append("5. Respect drain flag: if state directory contains `.drain`, exit cleanly without doing work.")
    lines.append("")
    ctx = "\n".join(lines)
    _context_cache[key] = ctx
    return ctx


def assemble_prompt(
    role_name: str,
    adapter: Any = None,
    ticket_context: str = "",
    extra_instructions: str = "",
) -> str:
    template = load_role_template(role_name)
    parts = [template]
    project_ctx = build_project_context(adapter)
    if project_ctx:
        parts.append(project_ctx)
    if ticket_context:
        parts.append("---")
        parts.append("")
        parts.append("## Current Ticket")
        parts.append("")
        parts.append(ticket_context)
        parts.append("")
    if extra_instructions:
        parts.append("---")
        parts.append("")
        parts.append("## Additional Instructions")
        parts.append("")
        parts.append(extra_instructions)
        parts.append("")
    return "\n".join(parts)


def format_ticket_context(ticket: Any) -> str:
    lines = [
        f"**ID**: {ticket.id}",
        f"**Title**: {ticket.title}",
        f"**Class**: {ticket.ticket_class.value if hasattr(ticket.ticket_class, 'value') else ticket.ticket_class}",
        f"**Severity**: {ticket.severity.value if hasattr(ticket.severity, 'value') else ticket.severity}",
        f"**Problem**: {ticket.problem_statement}",
        f"**Desired State**: {ticket.desired_state}",
    ]
    if ticket.acceptance_criteria:
        lines.append("")
        lines.append("**Acceptance Criteria**:")
        for i, ac in enumerate(ticket.acceptance_criteria, 1):
            lines.append(f"  {i}. {ac}")
    if ticket.affected_modules:
        lines.append(f"\n**Affected Modules**: {', '.join(ticket.affected_modules)}")
    if ticket.evidence:
        lines.append(f"\n**Evidence**:\n```\n{ticket.evidence[:500]}\n```")
    return "\n".join(lines)
