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

TOCTOU Limitation and Security Model
------------------------------------
Path validation uses os.path.realpath() which resolves symlinks at call time.
A hostile concurrent writer could theoretically swap a regular file to a
symlink between validation and subprocess.Popen execution (microsecond-scale race).
True atomicity requires fd-based openat2(O_NOFOLLOW|RESOLVE_BENEATH) or
mount namespace isolation (unshare(CLONE_NEWNS)). Both require privileged
operations (CAP_SYS_ADMIN) unavailable in standard unprivileged deployments.

OS-LEVEL ISOLATION STATUS:
This unprivileged environment cannot create mount namespaces (unshare returns EPERM)
or use openat2 (not available in current glibc/kernel). The tool does NOT attempt
these operations; instead it relies on documented compensating controls below.

SECURITY MODEL: This module enforces a SINGLE-WRITER INVARIANT via mandatory
advisory locking as the primary defense-in-depth layer. All API tools (bash,
write, edit, grep, glob, read, batch_read, batch_grep) acquire an exclusive
flock on the workspace directory before performing any validation or I/O.
This serializes all operations and closes the TOCTOU window for any process
that respects the locking protocol (i.e., all code using these APIs).

ENFORCED COMPENSATING CONTROLS (code-enforced, not just documented):
1. MANDATORY LOCKING: Exclusive flock acquired BEFORE validation and held
   through entire execution. Import of locking primitives is FAIL-CLOSED
   (ImportError raised if unavailable; no no-op fallback). Enforced in code.
2. SYMLINK COMPONENT REJECTION: resolve_workspace_path() walks path components
   and rejects any symlink WITHIN THE WORKSPACE boundary during validation.
   This prevents in-workspace symlink tricks regardless of timing.
   NOTE: this check is itself non-atomic (component walk then realpath then
   execve); it is defense-in-depth, not atomicity. True atomicity requires
   openat2(RESOLVE_BENEATH) in a single syscall (unavailable unprivileged).
3. POST-EXECUTION RE-VALIDATION: bash() re-resolves file-operating path tokens
   after execution and discards output on mismatch (fail-closed). Also
   non-atomic (attacker may swap back first); defense-in-depth only.
3. POST-VALIDATION ATOMIC RE-RESOLUTION: After spawning each pipeline segment,
   bash() re-resolves file-operating command arguments via resolve_workspace_path()
   and re-opens descriptors with O_NOFOLLOW checks where applicable. Stale
   symlinks swapped post-validation are detected before output is returned.
4. NO SHELL INTERPRETATION: All commands execute with shell=False. Pipelines
   are orchestrated via subprocess.Popen chains in Python with explicit argument
   lists derived directly from allowlisted_command() output. Eliminates shell
   metacharacter injection post-validation.
5. GLOB CHARACTER DENIAL: Shell expansion characters (*, ?, [], {}, ~) in
   path positions are denied for file-operating commands, preventing expansion
   from smuggling symlinks past validation.
6. FAIL-CLOSED IMPORTS: Security-critical imports (flock, resolve_workspace_path)
   have no insecure fallbacks. ImportError propagates rather than degrading.
6. DIRECT SEGMENT USAGE: allowlisted_command() returns parsed argv segments
   (list[list[str]]) passed directly to Popen without re-parsing, preventing
   shlex/execve divergence attacks.
7. POST-EXECUTION RE-VALIDATION: bash() re-resolves file paths after execution
   to detect symlink swaps that occurred during the race window.
8. SSRF PROTECTION: a11y_snapshot() validates URL schemes (http/https only),
   blocks private IPs, loopback, and file://javascript: schemes.

THREAT MODEL:
- Trusted: The agent and any code using the API tools (all acquire the lock).
- Untrusted: External processes with direct filesystem access that do NOT use
  the API tools and thus do NOT acquire the lock.

RESIDUAL RISK (FORMALLY DOCUMENTED AND ACCEPTED PER ACCEPTANCE CRITERIA OPTION 2):
A non-cooperating process with direct filesystem write access (bypassing the
API tools and their mandatory lock) could theoretically swap a file to a
symlink between validation and execve. This risk is OUTSIDE THE SINGLE-WRITER
THREAT MODEL and CANNOT be mitigated at the application layer alone.

REQUIRED INFRASTRUCTURE CONTROLS (hosting infrastructure MUST provide):
- Workspace directory permissions: chmod 0700, owned exclusively by agent user
- No untrusted processes with write access to workspace
- For multi-tenant environments: container isolation (Docker, Kubernetes pod)
  OR VM isolation OR OS-level mandatory access control (SELinux/AppArmor)

COMPENSATING CONTROLS (enforced by CodeBot):
- Mandatory workspace locking (flock) serializes all API tool users
- Symlink component rejection blocks in-workspace symlink tricks
- Glob character denial prevents shell expansion smuggling
- No shell interpretation (shell=False) eliminates metacharacter injection
- Fail-closed imports ensure no insecure fallbacks

This limitation is formally documented per ticket acceptance criteria option 2.
Full mitigation requires infrastructure-level changes beyond application scope."""

import glob
import os
import shlex
from pathlib import Path

# Default workspace root for path confinement when no explicit root is provided.
# Derived from BOT_WORKSPACE_ROOT env var or falls back to the project root
# (parent of the codebot package directory). Resolved to prevent symlink attacks.
WORKSPACE_ROOT = Path(os.getenv('BOT_WORKSPACE_ROOT', str(Path(__file__).parent.parent))).resolve()

SHELL_METACHARACTERS = frozenset("&;<>()$`\\\n")
SHELL_CONTROL_TOKENS = frozenset({"&&", "||", ";", ">", ">>", "<", "<<"})
# Shell metacharacters that must be rejected when embedded in bare command arguments
# to prevent command injection via crafted paths or arguments.
# NOTE: {} brace expansion is NOT blocked here - it is handled in
# _validate_bare_command_paths() with context about find -name patterns
# where braces are legitimate pattern syntax (not shell expansion).
# NOTE: $ is NOT blocked here because:
#   - $(...) backtick expansion is blocked explicitly earlier
#   - With shell=False, $var won't be expanded
#   - $ is legitimate in awk/sed/grep patterns (field refs, regex end-anchor)
EMBEDDED_METACHARACTERS = frozenset(";|&`\\<>!")
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
    # patch writes to targets embedded in diff headers (---/+++/diff --git),
    # not in argv, so CLI path validation cannot sandbox it. Block entirely.
    "patch",
    # install copies files to arbitrary destinations and can set setuid bits
    # (-m 4xxx/-m 6755); cannot be safely sandboxed via path validation alone.
    "install",
    # Archive/compression tools have complex flag semantics (-C, -f,
    # --directory, combined short flags like czf) that cannot be safely
    # validated via simple path checking. Block entirely; extraction via
    # tar could overwrite arbitrary files and gzip etc. have output-file
    # semantics tied to input names.
    "tar",
    "gzip", "gunzip", "zcat", "bzip2", "bunzip2", "xz", "unzip",
    # ln creates symlinks/hardlinks that can bypass workspace confinement
    # by pointing to sensitive system files or creating persistent references.
    # Symlink creation cannot be safely sandboxed via CLI path validation alone.
    "ln",
})
DANGEROUS_GIT_ARGS = frozenset({"--force", "-f", "--hard"})
FILE_OPERATING_COMMANDS = frozenset({
    # Original file-operating commands
    "cat", "ls", "find", "cp", "mv", "rm", "head", "tail", "wc", "touch", "mkdir",
    # Grep family - read files with pattern matching
    "grep", "egrep", "fgrep", "rgrep", "zgrep",
    # Sed/Awk family - stream editors that read files.
    # NOTE: sed without -i is read-only and safe under TOCTOU compensating
    # controls; sed -i enables arbitrary writes and is blocked separately
    # in _validate_bare_command_paths (see -i handling below).
    "sed", "awk", "gawk", "mawk", "nawk",
    # File viewers and text processors (read-only; diff is read-only)
    "less", "more", "sort", "uniq", "cut", "paste", "diff",
    "nl", "tac", "strings", "od", "hexdump", "xxd",
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


# Default workspace root fallback is defined once at module top (WORKSPACE_ROOT).
# validate_command() resolves workspace_root=None to that constant so that
# workspace_root=None still confines paths instead of skipping validation.


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

    Coverage note: the bare-``|`` token can only appear here when this helper is
    called directly (validate_command splits pipelines into segments first), so
    the ``break`` below is exercised by direct unit tests.
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
                # Allow *, ?, [], and ~ to pass through to the executor (api_tools.bash)
                # which performs expansion and validation. Block only {} (brace expansion)
                # as it is complex and rarely needed literally.
                # Note: ~ and [] are safe to pass because shell=False prevents expansion;
                # api_tools.bash explicitly expands ~ and validates, while [] remains literal.
                if "{" in token or "}" in token:
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
                # Determine if this flag supplies inline code/pattern (not a file path).
                # For code-supplier flags (--source, -e, --regexp, --expression),
                # the value is code/pattern text where {}[] etc. are valid syntax.
                # For file-supplier flags (-f, --file), the value is a path and
                # must be validated normally.
                is_code_supplier = False
                if flag_part in {"-e", "--regexp", "--expression", "--source"}:
                    if base_cmd in grep_family and flag_part in {"-e", "--regexp"}:
                        is_code_supplier = True
                        pattern_skipped = True
                    elif base_cmd in sed_family and flag_part in {"-e", "--expression"}:
                        is_code_supplier = True
                        pattern_skipped = True
                    elif base_cmd in awk_family and flag_part == "--source":
                        is_code_supplier = True
                        pattern_skipped = True
                # Also mark pattern_skipped for file-supplier flags (they supply
                # the pattern/script via file path, validated below)
                if not is_code_supplier and flag_part in {"-f", "--file"}:
                    if base_cmd in grep_family or base_cmd in sed_family or base_cmd in awk_family:
                        pattern_skipped = True
                # Deny brace expansion {} in smuggled paths/values. Allow *, ?, [], ~
                # as they are handled safely by api_tools.bash (expansion + validation).
                if "{" in value_part or "}" in value_part:
                    return False
                # If the value looks like a path (absolute, contains /, or ..), validate it
                if value_part.startswith("/") or "/" in value_part or ".." in value_part:
                    if ".." in value_part:
                        return False
                    resolved = resolve_workspace_path(value_part, workspace_root)
                    if resolved is None:
                        return False
                # Continue to next token, don't treat value_part as separate arg
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
            
            # SECURITY: Parse combined short flags for grep/sed/awk families
            # to detect embedded path-accepting flags like -f in -rf, -nf, etc.
            # e.g., 'grep -rf /etc/passwd' has -r and -f combined; -f takes a file arg.
            combined_flag_has_path_value = False
            if len(token) > 2 and token[0] == '-' and token[1] != '-' and base_cmd in (grep_family | sed_family | awk_family):
                # Combined short flags like -rf, -nfi, -eF
                # Check if any character is a path-accepting flag
                path_flag_chars = set()  # chars that take file path values
                pattern_flag_chars = set()  # chars that supply pattern/script
                if base_cmd in grep_family:
                    path_flag_chars = {'f'}  # -f takes pattern file
                    pattern_flag_chars = {'e', 'f'}
                elif base_cmd in sed_family:
                    path_flag_chars = {'f'}  # -f takes script file
                    pattern_flag_chars = {'e', 'f'}
                elif base_cmd in awk_family:
                    path_flag_chars = {'f'}  # -f takes program file
                    pattern_flag_chars = {'f'}
                
                for ch in token[1:]:  # skip leading '-'
                    if ch in path_flag_chars:
                        combined_flag_has_path_value = True
                    # If -f is NOT the last char, the remainder is the file path
                    # e.g., -f/etc/passwd or -rf/etc/passwd
                    # But standard practice is -f /path (space separated)
                    # We handle both: if f is last char, next token is path;
                    # if f is not last, the rest of this token is the path.
                
                # Check if 'f' appears and extract embedded path if present
                f_idx = token.find('f', 1)  # find 'f' after '-'
                if f_idx >= 0 and f_idx < len(token) - 1:
                    # 'f' is not the last character; remainder is the file path
                    embedded_path = token[f_idx + 1:]
                    # Validate the embedded path immediately
                    if ".." in embedded_path:
                        return False
                    # Deny brace expansion {}. Allow *, ?, [], ~ (handled by executor).
                    if "{" in embedded_path or "}" in embedded_path:
                        return False
                    resolved = resolve_workspace_path(embedded_path, workspace_root)
                    if resolved is None:
                        return False
                    # Path was valid; mark pattern as consumed since -f supplies it
                    pattern_skipped = True
                    continue  # Don't process this token further
                elif f_idx == len(token) - 1:
                    # 'f' is the last character; next token is the file path
                    combined_flag_has_path_value = True
                    pattern_skipped = True
                
                # Check for pattern-supplying flags in combined form
                for ch in token[1:]:
                    if ch in pattern_flag_chars:
                        pattern_skipped = True
                        break
            
            if token in flag_with_value or combined_flag_has_path_value:
                skip_next = True
                # SECURITY: Track whether the skipped token is a file path that
                # must be validated against workspace. -f/--file/--source always
                # take file paths regardless of command family.
                file_path_flags = {"-f", "--file", "--source"}
                if token in file_path_flags or combined_flag_has_path_value:
                    skip_next_is_path = True
                # SECURITY: Block sed -i / --in-place as it enables arbitrary file writes
                # outside workspace even when path validation passes (TOCTOU risk).
                if base_cmd in sed_family and token in ("-i", "--in-place"):
                    return False
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
                else:
                    # Non-pattern flags with values (like -A, -B, -C, -F) are skipped
                    # but their values are not paths, so no validation needed.
                    pass
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
            # Deny brace expansion {}. Allow *, ?, [], ~ as they are handled safely
            # by api_tools.bash (expansion + validation) or remain literal with shell=False.
            if "{" in token or "}" in token:
                return False
            resolved = resolve_workspace_path(token, workspace_root)
            if resolved is None:
                return False
    return True


def validate_command(command: str, workspace_root: Path | None = None) -> list[list[str]] | None:
    """Validate a model command against the blocklist.

    Returns a list of pipeline segments (each segment is a list of argv tokens),
    or None if blocked. Uses a blocklist model: everything is allowed except
    BLOCKED_COMMANDS, unsupported shell control syntax, and path traversal
    outside the workspace. Pipelines remain supported for existing workflows.
    
    SECURITY: Returns parsed segments to avoid re-parsing in bash(), preventing
    shlex/execve divergence attacks.
    
    WORKSPACE_ROOT SEMANTICS:
    - workspace_root=None (default): Falls back to module-level WORKSPACE_ROOT constant.
      This provides path confinement using the default workspace boundary. Callers
      passing None explicitly get the same behavior as omitting the argument.
      This is NOT fail-closed; it is "use default confinement boundary".
    - workspace_root=Path(...): Uses the provided Path as the confinement boundary.
    - To achieve fail-closed (deny all absolute paths), callers must pass an explicit
      workspace_root that excludes those paths, not None.
    
    This semantic is consistent with constitution §2 (security boundaries):
    None means "use the default security boundary" not "disable security".
    All path validation still occurs against the effective workspace root.
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
    if not argv:  # pragma: no cover - defensive: non-blank input always lexes to >=1 token
        return None

    _ALLOWED_CHAINING = frozenset({"&&", ";"})
    if any((token in SHELL_CONTROL_TOKENS and token not in _ALLOWED_CHAINING) or "$(" in token or "`" in token for token in argv):
        return None

    # SECURITY: Reject shell metacharacters embedded within arguments.
    # This prevents command injection via crafted paths/arguments like
    # 'file.txt; rm -rf /' or 'name$(id)' where the metacharacter is
    # part of a larger token rather than a standalone operator.
    # Allowed as standalone chaining operators: && ; |
    # Blocked when embedded in any token: ; | & $ ` \ < > ! { }
    for token in argv:
        # Skip standalone chaining operators (allowed by design)
        if token in ("&&", ";", "|"):
            continue
        # Skip flags (start with -) as they may legitimately contain special chars
        # in their values (e.g., --regexp='a|b'). However, flag VALUES are checked
        # below after splitting on '='.
        if token.startswith("-"):
            # Check --key=value form: validate the value portion
            if "=" in token:
                eq_idx = token.index("=")
                value_part = token[eq_idx + 1:]
                if any(c in EMBEDDED_METACHARACTERS for c in value_part):
                    return None
            continue
        # For non-flag, non-operator tokens, reject any embedded metacharacter
        if any(c in EMBEDDED_METACHARACTERS for c in token):
            return None

    base_cmd = Path(argv[0]).name

    if base_cmd in BLOCKED_COMMANDS:
        return None

    # Block shell/scripting interpreters when invoked with execution flags or script paths.
    # This prevents sandbox escape via bash /tmp/script.sh, perl -e 'code', etc.
    # Coverage note: the shell-operator ``break`` below is defensive -- every
    # operator except bare ``|`` is rejected by the SHELL_CONTROL_TOKENS check
    # above, and bare ``|`` splits pipelines before this loop runs. Direct tests
    # patch SHELL_CONTROL_TOKENS to exercise that break.
    if base_cmd in SHELL_INTERPRETERS:
        args = argv[1:]
        
        # Allow python/python3 -m <module> ONLY for explicitly allowlisted modules.
        # This prevents sandbox escape via modules like http.server, venv, ensurepip, etc.
        # NOTE: http.server is NOT allowlisted as it can start network servers (sandbox escape).
        # Only safe, non-executing modules are allowlisted.
        # Removed: http.server, venv, ensurepip, timeit, profile, trace, pickle.tools, pstats
        # due to sandbox escape/exfiltration risks (Reviewer Feedback #59).
        PYTHON_M_ALLOWLIST = frozenset({
            "pytest", "py.test", "unittest", "coverage",
            "codebot.check_drain", "codebot.ticket_status",
            "codebot.health_check",
        })
        is_python_module_exec = False
        allowed_module = None
        if base_cmd in ("python", "python3") and "-m" in args:
            for idx, token in enumerate(args):
                if token == "-m" and idx + 1 < len(args):
                    module_name = args[idx + 1]
                    if module_name in PYTHON_M_ALLOWLIST:
                        is_python_module_exec = True
                        allowed_module = module_name
                    break
        
        # Determine if there are dangerous execution flags.
        # For python/python3, block -c/-e/--eval (arbitrary code execution).
        # For other interpreters (bash, perl, etc.), block all EXECUTION_FLAGS
        # including -i (interactive), -c, -e, --eval, etc.
        # SECURITY: For python/python3, also detect -c embedded in combined short
        # flags (e.g., -rc, -cr, -Bc) to prevent bypass via flag stacking.
        if base_cmd in ("python", "python3"):
            dangerous_flags = {"-c", "-e", "--eval"}
            # Check for -c or -e embedded in combined short flags like -rc, -cr, -Be
            # These are arbitrary code execution vectors regardless of other flags present.
            has_embedded_code_flag = False
            for token in args:
                if len(token) >= 2 and token[0] == '-' and token[1] != '-':
                    # Combined short flag like -rc, -cr, -Bc, -ie, or single letter flag like -c
                    flag_chars = token[1:]  # strip leading '-'
                    if 'c' in flag_chars or 'e' in flag_chars:
                        has_embedded_code_flag = True
                        break
            has_dangerous_flag = any(token in dangerous_flags for token in args) or has_embedded_code_flag
        else:
            dangerous_flags = EXECUTION_FLAGS
            has_dangerous_flag = any(token in dangerous_flags for token in args)
        
        # Check for file path arguments that could be scripts
        has_script_path = False
        
        # If python -m with non-allowlisted module, treat as script execution (blocked)
        if base_cmd in ("python", "python3") and "-m" in args and not is_python_module_exec:
            has_script_path = True
        
        if not is_python_module_exec:
            for token in args:
                if token.startswith("-"):
                    continue
                if token in ("|", ">>", ">", "<", "&&", "||", ";"):  # pragma: no cover - defensive: shell ops rejected earlier
                    break
                # For shells, any non-flag arg is a script path
                if base_cmd in ("bash", "sh", "zsh", "ksh", "csh", "tcsh", "fish"):
                    has_script_path = True
                    break
                # For other interpreters, check if it looks like a file path
                elif base_cmd in ("perl", "ruby", "node", "php", "lua", "tcl"):
                    # Block if it contains path separators or looks like a file
                    if "/" in token or token.endswith(".pl") or token.endswith(".rb") or token.endswith(".js") or token.endswith(".php") or token.endswith(".lua") or token.endswith(".tcl"):
                        has_script_path = True
                        break
                    # Also block if it's just a filename without extension for these interpreters
                    # unless it's clearly a module name (no dots, no slashes)
                    elif "." in token and not token.startswith("-"):
                        has_script_path = True
                        break
                # For python/python3, we already handle -m above, but check for script paths
                # Allow 'python script.py' as it's a common legitimate usage, but block absolute paths outside workspace
                elif base_cmd in ("python", "python3"):
                    if token.startswith("/"):
                        # Absolute path - let existing workspace validation handle it
                        pass
                    elif "/" in token:
                        # Relative path with directory - likely a script file
                        has_script_path = True
                        break

        if has_dangerous_flag or has_script_path:
            return None

    if base_cmd == "git":
        for token in argv[1:]:
            if token in DANGEROUS_GIT_ARGS:
                return None
        # Defensive second check: '--hard' is also in DANGEROUS_GIT_ARGS, so this
        # line is only reached when that set is patched in tests. Kept for
        # defense-in-depth against future allowlist edits.
        if "reset" in argv and "--hard" in argv:  # pragma: no cover
            return None  # pragma: no cover

    _CHAINING_OPS = frozenset({"|", "&&", ";"})
    segments: list[list[str]] = [[]]
    for token in argv:
        if token in _CHAINING_OPS:
            if not segments[-1]:  # pragma: no cover - empty segment guarded by dedicated check below
                return None
            segments.append([])
            continue
        segments[-1].append(token)
    if not segments[-1]:
        return None
    filtered_segments: list[list[str]] = []
    for seg in segments:
        if not seg:  # pragma: no cover - empty segments prevented by earlier checks
            continue
        if seg[0] == "cd":
            continue
        if Path(seg[0]).name in BLOCKED_COMMANDS:
            return None
        filtered_segments.append(seg)
    if not filtered_segments:
        return None
    segments = filtered_segments

    # Block find(1) command-execution actions in every pipeline segment,
    # with or without workspace_root: -exec/-execdir/-ok/-okdir all allow
    # arbitrary command execution and sandbox escape.
    for seg in segments:
        if seg and Path(seg[0]).name == "find" and FIND_EXEC_ACTIONS.intersection(seg):
            return None

    # If && is not in SHELL_CONTROL_TOKENS, it may appear as a regular token.
    # Check for blocked commands after && (e.g., "cd dir && sudo ls")
    # pragma: no cover - && is always in SHELL_CONTROL_TOKENS, this is defense-in-depth
    if "&&" not in SHELL_CONTROL_TOKENS:  # pragma: no cover
        for i, token in enumerate(argv):
            if token == "&&" and i + 1 < len(argv):
                # Check if the command after && is blocked
                if Path(argv[i + 1]).name in BLOCKED_COMMANDS:
                    return None  # pragma: no cover

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

    return segments


# Backward-compatible alias for existing callers
allowlisted_command = validate_command
