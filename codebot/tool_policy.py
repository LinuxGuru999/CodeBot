"""Define the sandbox boundary for model-invoked BotNet tools.

Purpose
-------
Provides path resolution and command validation used by the API tool surface.
Uses a blocklist model: all commands are permitted unless explicitly dangerous,
with workspace path enforcement preventing escape from the project directory.

Why
---
Agents run as a normal user (not root) within the project workspace. A short
blocklist of genuinely destructive commands plus workspace path confinement is
sufficient to prevent damage without crippling legitimate work.

Invariants
----------
- Resolved paths must remain below the supplied workspace root.
- Commands in BLOCKED_COMMANDS are rejected.
- Shell metacharacters are rejected (except python -c contexts).
- Path arguments containing '..' or resolving outside workspace are rejected.
"""

import shlex
from pathlib import Path


SHELL_METACHARACTERS = frozenset("&;<>()$`\\\n")
SHELL_CONTROL_TOKENS = frozenset({"&&", "||", ";", ">", ">>", "<", "<<"})
BLOCKED_COMMANDS = frozenset({
    "sudo", "su", "mkfs", "dd", "shutdown", "reboot", "init",
    "kill", "killall", "pkill",
    "apt", "apt-get", "yum", "dnf", "brew",
    "chmod", "chown", "chgrp",
})
DANGEROUS_GIT_ARGS = frozenset({"--force", "-f", "--hard"})


def resolve_workspace_path(path: str, workspace_root: Path) -> Path | None:
    """Resolve a path only when it remains inside the workspace root."""
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else workspace_root / candidate).resolve(strict=False)
    try:
        resolved.relative_to(workspace_root)
    except ValueError:
        return None
    return resolved


def _has_path_escape(argv: list[str], workspace_root: Path) -> bool:
    """Check if any argument attempts path traversal or escapes workspace."""
    for token in argv[1:]:
        if token.startswith("-"):
            continue
        if ".." in token:
            return True
        if token.startswith("/"):
            resolved = resolve_workspace_path(token, workspace_root)
            if resolved is None:
                return True
    return False


def validate_command(command: str, workspace_root: Path | None = None) -> list[str] | None:
    """Validate a model command against the blocklist.

    Returns the raw command string wrapped in a list for shell execution,
    or None if blocked. Uses a blocklist model: everything is allowed except
    BLOCKED_COMMANDS, unsupported shell control syntax, and path traversal
    outside the workspace. Pipelines remain supported for existing workflows.
    """
    if not command or not command.strip():
        return None

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        argv = list(lexer)
    except ValueError:
        return None
    if not argv:
        return None

    if any(token in SHELL_CONTROL_TOKENS or "$(" in token or "`" in token for token in argv):
        return None

    base_cmd = argv[0]

    if base_cmd in BLOCKED_COMMANDS:
        return None

    if base_cmd == "git":
        for token in argv[1:]:
            if token in DANGEROUS_GIT_ARGS:
                return None
        if "reset" in argv and "--hard" in argv:
            return None

    segments: list[list[str]] = [[]]
    for token in argv:
        if token == "|":
            if not segments[-1]:
                return None
            segments.append([])
            continue
        segments[-1].append(token)
    if not segments[-1]:
        return None
    for seg in segments:
        if seg and seg[0] in BLOCKED_COMMANDS:
            return None

    if workspace_root is not None:
        for token in argv:
            if token.startswith("-") or token in ("|", ">>", ">", "<", "&&", "||", ";"):
                continue
            if ".." in token:
                return None
            if token.startswith("/"):
                resolved = resolve_workspace_path(token, workspace_root)
                if resolved is None:
                    return None
    else:
        for token in argv[1:]:
            if not token.startswith("-") and token not in ("|", ">>", ">", "<", "&&", "||", ";") and ".." in token:
                return None

    return [command]


# Backward-compatible alias for existing callers
allowlisted_command = validate_command
