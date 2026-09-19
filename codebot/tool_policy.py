"""Define the sandbox boundary for model-invoked BotNet tools.

Purpose
-------
Provides path resolution and command parsing used by the API tool surface.

Why
---
Model-produced arguments are untrusted. Resolving them against one workspace
root prevents path and symlink escapes, while a small argv allowlist removes
the shell interpreter from the tool boundary.

Invariants
----------
- Resolved paths must remain below the supplied workspace root.
- Command parsing never permits shell metacharacters or non-allowlisted argv.
"""

import shlex
from pathlib import Path


SHELL_METACHARACTERS = frozenset("&;<>()$`\\\n")
ALLOWED_PIPE_TARGETS = frozenset({"head", "tail", "sort", "wc", "grep", "awk", "sed", "uniq", "cut", "tr"})
ALLOWED_GIT_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "grep", "rev-parse",
                                     "push", "pull", "fetch", "add", "commit", "checkout",
                                     "rm", "branch"})
ALLOWED_GIT_FLAGS = frozenset({"-m", "--no-commit", "--short", "--oneline", "-n", "--stat"})
ALLOWED_PYTEST_ARGS = frozenset({"-q", "-x", "-v", "--tb", "-k", "-m", "--durations"})
ALLOWED_PYTHON_FLAGS = frozenset({"-c", "-m", "-q"})
ALLOWED_PYTHON_COMMANDS = frozenset({"py_compile"})
ALLOWED_BARE_COMMANDS = frozenset({"ls", "wc", "head", "tail", "cat", "date", "realpath", "dirname", "basename", "cp", "mv", "mkdir", "find", "echo", "pwd", "sleep", "touch", "df", "free"})
ALLOWED_GH_SUBCOMMANDS = frozenset({"pr", "issue", "repo", "auth", "api", "gist"})
DANGEROUS_GH_ARGS = frozenset({"--hostname", "--delete-branch", "-X DELETE", "--admin"})
DANGEROUS_GIT_ARGS = frozenset({"--amend", "--force", "-f", "--hard", "reset", "rebase", "push --force"})


def resolve_workspace_path(path: str, workspace_root: Path) -> Path | None:
    """Resolve a path only when it remains inside the workspace root."""
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else workspace_root / candidate).resolve(strict=False)
    try:
        resolved.relative_to(workspace_root)
    except ValueError:
        return None
    return resolved


def allowlisted_command(command: str) -> list[str] | None:
    """Parse a model command into an approved argv vector."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    if "|" in argv:
        pipe_idx = argv.index("|")
        left_argv = argv[:pipe_idx]
        right_argv = argv[pipe_idx + 1:]
        if not left_argv or not right_argv:
            return None
        if not right_argv[0] in ALLOWED_PIPE_TARGETS:
            return None
        left_result = allowlisted_command(" ".join(shlex.quote(a) for a in left_argv))
        if left_result is None:
            return None
        for token in right_argv[1:]:
            if ".." in token:
                return None
        return left_argv + ["|"] + right_argv
    if any(any(character in token for character in SHELL_METACHARACTERS) for token in argv):
        return None
    if argv[0] == "git":
        offset = 1
        if len(argv) >= 4 and argv[1] == "-C":
            if ".." in argv[2]:
                return None
            if argv[2].startswith("/"):
                return None
            offset = 3
        if len(argv) < offset + 1 or argv[offset] not in ALLOWED_GIT_SUBCOMMANDS:
            return None
        for token in argv[2:]:
            if token in DANGEROUS_GIT_ARGS:
                return None
            if any(character in token for character in SHELL_METACHARACTERS):
                return None
        return argv
    if argv[0] == "rm":
        for token in argv[1:]:
            if token.startswith("-") and token not in ("-f",):
                return None
            if ".." in token:
                return None
            if token.startswith("/"):
                return None
        return argv
    if argv[0] in ("pytest", "python3", "python") or argv[0] in ALLOWED_PYTHON_COMMANDS:
        for token in argv[1:]:
            if token.startswith("-"):
                if token not in ALLOWED_PYTEST_ARGS and token not in ALLOWED_PYTHON_FLAGS and not token.startswith("--tb=") and not token.startswith("-k"):
                    return None
            elif ".." in token:
                return None
            elif token.startswith("/"):
                return None
        return argv
    if argv[0] in ALLOWED_BARE_COMMANDS:
        for token in argv[1:]:
            if ".." in token:
                return None
            if token.startswith("/"):
                return None
        return argv
    if argv[0] == "gh":
        if len(argv) < 2 or argv[1] not in ALLOWED_GH_SUBCOMMANDS:
            return None
        for token in argv[2:]:
            if ".." in token:
                return None
            if token.startswith("/"):
                return None
            if token in DANGEROUS_GH_ARGS:
                return None
        for i in range(len(argv) - 1):
            if argv[i] == "-X" and argv[i + 1] in ("DELETE", "delete"):
                return None
        return argv
    return None
