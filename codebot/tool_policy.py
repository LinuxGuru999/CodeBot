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
- Glob characters (*, ?, [], {}, ~) in path positions are denied to prevent
  shell expansion from smuggling symlinks past validation.

TOCTOU Limitation and Compensating Controls
-------------------------------------------
Path validation uses os.path.realpath() which resolves symlinks at call time.
A hostile concurrent writer could theoretically swap a regular file to a
symlink between validation and subprocess.run() execution (microsecond-scale
race). True atomicity would require fd-based openat2(O_NOFOLLOW) or mount
namespace isolation, which shell=True cannot provide.

Compensating controls enforce the single-writer invariant:
1. Symlink component denial: resolve_workspace_path() rejects any path
   containing symlink components WITHIN the workspace boundary, preventing
   symlink-based escapes even if created concurrently.
2. Glob character denial: Shell expansion characters (*, ?, [], {}, ~) in
   path positions are denied for file-operating commands, preventing shell
   expansion from smuggling symlinked paths past validation.
3. Workspace directory lock: api_tools.bash() acquires an exclusive advisory
   lock on the workspace directory before command execution, serializing
   concurrent bash calls and preventing concurrent writers from swapping
   symlinks during the validation-to-execution window.
4. Pre-exec revalidation: bash() re-validates the command immediately before
   subprocess.run() as a defense-in-depth measure.

The workspace is assumed to be single-writer trusted (only the agent writes
to it via the API tools, which hold the workspace lock during write/edit/bash
operations). This invariant is enforced programmatically via the workspace
directory lock mechanism."""

import os
import shlex
from pathlib import Path

# Default workspace root for path confinement when no explicit root is provided.
# Derived from BOT_WORKSPACE_ROOT env var or falls back to the project root
# (parent of the codebot package directory). Resolved to prevent symlink attacks.
WORKSPACE_ROOT = Path(os.getenv('BOT_WORKSPACE_ROOT', str(Path(__file__).parent.parent))).resolve()

SHELL_METACHARACTERS = frozenset("&;<>()$`\\\n")
SHELL_CONTROL_TOKENS = frozenset({"&&", "||", ";", ">", ">>", "<", "<<"})
BLOCKED_COMMANDS = frozenset({
    "sudo", "su", "mkfs", "dd", "shutdown", "reboot", "init",
    "kill", "killall", "pkill",
    "apt", "apt-get", "yum", "dnf", "brew",
    "chmod", "chown", "chgrp",
    "dash",
    "env", "xargs", "eval", "exec",
    # tee can write arbitrary data to any path; blocked to prevent
    # sandbox escape via 'tee /outside/path' even when FILE_OPERATING_COMMANDS
    # validates read paths. Write-capable utilities must be explicitly allowlisted.
    "tee",
})
DANGEROUS_GIT_ARGS = frozenset({"--force", "-f", "--hard"})
FILE_OPERATING_COMMANDS = frozenset({
    # Original file-operating commands
    "cat", "ls", "find", "cp", "mv", "rm", "head", "tail", "wc", "touch", "mkdir",
    # Grep family - read files with pattern matching
    "grep", "egrep", "fgrep", "rgrep", "zgrep",
    # Sed/Awk family - stream editors that read/write files
    "sed", "awk", "gawk", "mawk", "nawk",
    # File viewers and text processors
    "less", "more", "sort", "uniq", "cut", "paste", "diff", "patch",
    "nl", "tac", "strings", "od", "hexdump", "xxd",
    # Archive/compression tools that read files
    "tar", "gzip", "gunzip", "zcat", "bzip2", "bunzip2", "xz", "unzip",
    # File manipulation (tee excluded - moved to BLOCKED_COMMANDS due to write risk)
    "ln", "install",
})

# Shell and scripting interpreters that can execute arbitrary code
# Note: awk/sed are excluded here — they are file-operating commands validated
# via FILE_OPERATING_COMMANDS + _validate_bare_command_paths (pattern-skipping).
# Treating them as interpreters would incorrectly block legitimate
# 'sed s/a/b/ file' (pattern contains '/').
SHELL_INTERPRETERS = frozenset({
    "bash", "sh", "zsh", "dash", "ksh", "csh", "tcsh", "fish",
    "perl", "python", "python3", "ruby", "node", "php", "lua", "tcl",
})

# Flags that trigger code execution in interpreters
EXECUTION_FLAGS = frozenset({
    "-c", "--eval", "-e", "-i", "--interactive",
    "-r", "--rcfile", "--init-file", "--noprofile", "--norc",
    "-s", "--stdin", "-F", "-a"
})
# find(1) command-execution actions: all allow arbitrary command execution
# and therefore sandbox escape, regardless of path confinement.
FIND_EXEC_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})


# Default workspace root used when validate_command() is called without an
# explicit workspace_root. Mirrors codebot.api_tools.WORKSPACE_ROOT so that
# workspace_root=None still confines paths instead of skipping validation.
WORKSPACE_ROOT = Path(os.getenv("BOT_WORKSPACE_ROOT", str(Path(__file__).parent.parent))).resolve()


def resolve_workspace_path(path: str, workspace_root: Path) -> Path | None:
    """Resolve a path only when it remains inside the workspace root.

    Uses os.path.realpath to resolve symlinks (including dangling and
    intermediate directory symlinks) and performs normalized prefix checking
    with trailing-slash handling to prevent prefix attacks (e.g. /workspace
    vs /workspace-evil) and symlink-based escapes.

    SECURITY: Rejects any path containing symlink components WITHIN THE
    WORKSPACE to prevent TOCTOU race conditions where a symlink could be
    swapped between validation and execution. System-level symlinks (e.g.
    /tmp -> /private/tmp) are ignored as they are outside agent control.
    Also denies glob characters (*, ?, [], {}, ~) in path positions for
    file-operating commands to prevent shell expansion from smuggling symlinks.
    """
    candidate = Path(path)
    if candidate.is_absolute():
        abs_path = str(candidate)
    else:
        abs_path = os.path.join(str(workspace_root), str(candidate))

    # Normalize paths for component analysis
    norm_abs_path = os.path.normpath(abs_path)
    norm_ws_str = os.path.normpath(str(workspace_root))
    
    ws_parts = Path(norm_ws_str).parts
    abs_parts = Path(norm_abs_path).parts
    
    # Check for symlink components ONLY within the workspace boundary.
    # This avoids false positives from system symlinks (e.g. /tmp).
    if len(abs_parts) >= len(ws_parts) and abs_parts[:len(ws_parts)] == ws_parts:
        # Path is logically inside workspace; check components after workspace root
        for i in range(len(ws_parts), len(abs_parts)):
            check_path = Path(*abs_parts[:i+1])
            try:
                if check_path.is_symlink():
                    return None  # Symlink component detected inside workspace
            except OSError:
                return None

    # Resolve all symlinks using os.path.realpath for final containment check
    real_path = os.path.realpath(abs_path)
    # Normalize workspace_root with trailing separator to prevent prefix attacks
    norm_root = os.path.realpath(str(workspace_root))
    if not norm_root.endswith(os.sep):
        norm_root += os.sep
    # Check if resolved path is within workspace root (also allow exact match)
    if real_path == norm_root.rstrip(os.sep) or real_path.startswith(norm_root):
        return Path(real_path)
    return None


def _validate_bare_command_paths(argv: list[str], workspace_root: Path) -> bool:
    """Validate that all path arguments for file-operating bare commands stay within workspace.

    Returns True if the command is safe, False if any path escapes the workspace boundary.
    This prevents sandbox escape via absolute paths like 'cat /etc/passwd' or 'ls /'.
    """
    if not argv:
        return True
    base_cmd = Path(argv[0]).name  # Handle cases like /bin/cat
    if base_cmd not in FILE_OPERATING_COMMANDS:
        return True

    # Block find -exec/-execdir/-ok/-okdir entirely as they allow arbitrary
    # command execution which is hard to validate safely and escapes the sandbox
    if base_cmd == "find" and FIND_EXEC_ACTIONS.intersection(argv):
        return False

    # Determine which commands need special pattern/script handling
    # For grep family: first non-flag token is PATTERN (skip it)
    # For sed: first non-flag token is SCRIPT unless -e/-f present (skip it)
    # For awk: first non-flag token is PROGRAM unless -f present (skip it)
    grep_family = {"grep", "egrep", "fgrep", "rgrep", "zgrep"}
    sed_family = {"sed"}
    awk_family = {"awk", "gawk", "mawk", "nawk"}

    skip_next = False
    skip_next_is_path = False  # Track if the next skipped token is a file path (e.g., -f /path)
    pattern_skipped = False  # Track if we've skipped the pattern/script token
    for i, token in enumerate(argv[1:], start=1):
        if skip_next:
            skip_next = False
            # SECURITY: If the flag that triggered skip_next was a file-path flag
            # (-f/--file/--source), validate the skipped token as a workspace path.
            # This prevents 'awk -f /etc/passwd' or 'grep -f /etc/shadow' escape.
            if skip_next_is_path:
                skip_next_is_path = False
                if ".." in token:
                    return False
                GLOB_CHARS = frozenset("*?[]{}~")
                if any(c in token for c in GLOB_CHARS):
                    return False
                resolved = resolve_workspace_path(token, workspace_root)
                if resolved is None:
                    return False
            continue
        # Skip flags and their values that aren't paths
        if token.startswith("-"):
            # Handle explicit end-of-options separator
            if token == "--":
                # Everything after -- is a path/operand, not a flag/pattern
                # Mark pattern as already handled so next token is treated as path
                pattern_skipped = True
                continue
            
            # SECURITY: Check for path smuggling via --key=value syntax
            # e.g., cat --file=/etc/passwd or cat --output=*.txt
            if "=" in token:
                eq_idx = token.index("=")
                flag_part = token[:eq_idx]
                value_part = token[eq_idx+1:]
                # Deny glob chars in smuggled paths/values for file-operating commands
                GLOB_CHARS = frozenset("*?[]{}~")
                if any(c in value_part for c in GLOB_CHARS):
                    return False
                # If the value looks like a path (absolute, contains /, or ..), validate it
                if value_part.startswith("/") or "/" in value_part or ".." in value_part:
                    if ".." in value_part:
                        return False
                    resolved = resolve_workspace_path(value_part, workspace_root)
                    if resolved is None:
                        return False
                # Continue to next token, don't treat value_part as separate arg
                # But we must also check if this flag is a known pattern-supplier
                if flag_part in {"-e", "--regexp", "--expression", "-f", "--file", "--source"}:
                    if base_cmd in grep_family and flag_part in {"-e", "--regexp", "-f", "--file"}:
                        pattern_skipped = True
                    elif base_cmd in sed_family and flag_part in {"-e", "--expression", "-f", "--file"}:
                        pattern_skipped = True
                    elif base_cmd in awk_family and flag_part in {"-f", "--file", "--source"}:
                        pattern_skipped = True
                continue

            # Flags that take a value argument (space-separated)
            flag_with_value = {
                "-name", "-type", "-maxdepth", "-mindepth", "--color",
                # grep flags with values
                "-A", "-B", "-C", "--after-context", "--before-context", "--context",
                "-e", "--regexp", "-f", "--file",
                # sed flags with values
                "-i", "--in-place", "--expression",
                # awk flags with values
                "-F", "--field-separator", "--source",
            }
            if token in flag_with_value:
                skip_next = True
                # SECURITY: Track whether the skipped token is a file path that
                # must be validated against workspace. -f/--file/--source always
                # take file paths regardless of command family.
                file_path_flags = {"-f", "--file", "--source"}
                if token in file_path_flags:
                    skip_next_is_path = True
                # Flags that already supply the PATTERN/SCRIPT/PROGRAM mean the
                # positional pattern token is not needed; mark it as consumed so
                # the next non-flag token is treated as a file path.
                pattern_flags = {
                    "-e", "--regexp", "--expression", "-f", "--file", "--source",
                }
                if token in pattern_flags:
                    # Only for commands where this flag actually supplies pattern
                    if base_cmd in grep_family and token in {"-e", "--regexp", "-f", "--file"}:
                        pattern_skipped = True
                    elif base_cmd in sed_family and token in {"-e", "--expression", "-f", "--file"}:
                        pattern_skipped = True
                    elif base_cmd in awk_family and token in {"-f", "--file", "--source"}:
                        pattern_skipped = True
            continue
        # Skip shell operators that might appear in argv after splitting
        if token in ("|", ">>", ">", "<", "&&", "||", ";"):
            break  # Stop checking this segment; next segment is checked separately
        # Check for path traversal
        if ".." in token:
            return False

        # For grep/sed/awk, skip the first non-flag token as pattern/script
        if not pattern_skipped:
            if base_cmd in grep_family:
                pattern_skipped = True
                continue
            elif base_cmd in sed_family:
                pattern_skipped = True
                continue
            elif base_cmd in awk_family:
                pattern_skipped = True
                continue

        # For absolute paths or relative paths, resolve and verify they stay in workspace
        if token.startswith("/") or "/" in token or base_cmd in FILE_OPERATING_COMMANDS:
            # Only validate tokens that look like paths (not regex patterns, etc.)
            # For find, skip pattern arguments that follow -name/-type
            if base_cmd == "find" and i > 1 and argv[i-1] in ("-name", "-iname", "-path", "-regex"):
                continue
            # Deny glob/metachar characters in path positions for file-operating commands.
            # Shell expansion of *, ?, [], {}, ~ can smuggle symlinks past validation.
            GLOB_CHARS = frozenset("*?[]{}~")
            if any(c in token for c in GLOB_CHARS):
                return False
            resolved = resolve_workspace_path(token, workspace_root)
            if resolved is None:
                return False
    return True


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

    base_cmd = Path(argv[0]).name

    if base_cmd in BLOCKED_COMMANDS:
        return None

    # Block shell/scripting interpreters when invoked with execution flags or script paths.
    # This prevents sandbox escape via bash /tmp/script.sh, perl -e 'code', etc.
    if base_cmd in SHELL_INTERPRETERS:
        args = argv[1:]
        has_execution_flag = any(token in EXECUTION_FLAGS for token in args)
        
        # Check for file path arguments that could be scripts
        # A token is considered a potential script path if:
        # 1. It does not start with '-' (not a flag)
        # 2. It is not a shell operator
        # 3. For python/perl/ruby/node/php: allow -m (module) but block -c/-e
        #    For shells (bash/sh/zsh): block any non-flag argument as it's likely a script
        has_script_path = False
        
        # For python/python3, if -m is used, subsequent args are module args, not scripts.
        # We skip script path detection entirely for python -m.
        is_python_module_exec = base_cmd in ("python", "python3") and "-m" in args
        
        if not is_python_module_exec:
            for token in args:
                if token.startswith("-"):
                    continue
                if token in ("|", ">>", ">", "<", "&&", "||", ";"):
                    break
                # For shells, any non-flag arg is a script path
                if base_cmd in ("bash", "sh", "zsh", "ksh", "csh", "tcsh", "fish"):
                    has_script_path = True
                    break
                # For other interpreters, check if it looks like a file path
                elif base_cmd in ("perl", "ruby", "node", "php", "lua", "tcl", "awk", "sed"):
                    # Block if it contains path separators or looks like a file
                    if "/" in token or token.endswith(".pl") or token.endswith(".rb") or token.endswith(".js") or token.endswith(".php") or token.endswith(".lua") or token.endswith(".tcl") or token.endswith(".awk") or token.endswith(".sed"):
                        has_script_path = True
                        break
                    # Also block if it's just a filename without extension for these interpreters
                    # unless it's clearly a module name (no dots, no slashes)
                    elif "." in token and not token.startswith("-"):
                        has_script_path = True
                        break
                # For python/python3, we already handle -c above, but check for script paths
                # Allow 'python script.py' as it's a common legitimate usage, but block absolute paths outside workspace
                elif base_cmd in ("python", "python3"):
                    if token.startswith("/"):
                        # Absolute path - let existing workspace validation handle it
                        pass
                    elif "/" in token:
                        # Relative path with directory - likely a script file
                        has_script_path = True
                        break

        if has_execution_flag or has_script_path:
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
        if seg and Path(seg[0]).name in BLOCKED_COMMANDS:
            return None

    # Block find(1) command-execution actions in every pipeline segment,
    # with or without workspace_root: -exec/-execdir/-ok/-okdir all allow
    # arbitrary command execution and sandbox escape.
    for seg in segments:
        if seg and Path(seg[0]).name == "find" and FIND_EXEC_ACTIONS.intersection(seg):
            return None

    # Use explicit workspace_root if provided, otherwise fall back to module-level WORKSPACE_ROOT
    effective_root = workspace_root if workspace_root is not None else WORKSPACE_ROOT

    for token in argv:
        if token.startswith("-") or token in ("|", ">>", ">", "<", "&&", "||", ";"):
            continue
        if ".." in token:
            return None
        if token.startswith("/"):
            resolved = resolve_workspace_path(token, effective_root)
            if resolved is None:
                return None
    # Validate bare command paths per pipeline segment using the resolved root
    for seg in segments:
        if seg and not _validate_bare_command_paths(seg, effective_root):
            return None

    return [command]


# Backward-compatible alias for existing callers
allowlisted_command = validate_command
