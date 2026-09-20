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
    """Parse a model command into an approved argv vector.

    Returns parsed argv if the command is safe, or None if blocked.
    Uses a blocklist model: everything is allowed except BLOCKED_COMMANDS,
    shell metacharacters, and path traversal outside the workspace.
    """
    # Block shell metacharacters in raw input BEFORE shlex parsing.
    # shlex consumes characters like \n as whitespace, hiding them from post-parse checks.
    # Exception: python -c needs quotes/special chars for inline code.
    is_python_c = command.lstrip().startswith(("python3 -c", "python -c"))
    if not is_python_c:
        if any(c in command for c in SHELL_METACHARACTERS):
            return None

    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None

    base_cmd = argv[0]

    # Block dangerous commands
    if base_cmd in BLOCKED_COMMANDS:
        return None

    # Block dangerous git args
    if base_cmd == "git":
        for token in argv[1:]:
            if token in DANGEROUS_GIT_ARGS:
                return None
        # Block 'reset --hard' pattern (two separate tokens)
        if "reset" in argv and "--hard" in argv:
            return None

    # Path traversal checks
    if workspace_root is not None:
        if _has_path_escape(argv, workspace_root):
            return None
    else:
        # Without workspace_root, still block .. traversal
        for token in argv[1:]:
            if not token.startswith("-") and ".." in token:
                return None

    return argv


# Backward-compatible alias for existing callers
allowlisted_command = validate_command
