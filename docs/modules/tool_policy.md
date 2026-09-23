# tool_policy.py

Provides path resolution and command parsing used by the API tool surface.

## Key Exports
- `validate_command()`: Function
- `resolve_workspace_path()`: Function
- `allowlisted_command()`: Function

## Invariants
- Resolved paths must remain below the supplied workspace root.
- Command parsing never permits shell metacharacters or non-allowlisted argv.
- Glob characters (`*`, `?`, `[`, `]`, `{`, `}`, `~`) in path positions for file-operating commands are denied.
- Any path containing symlink components within the workspace is rejected.
- `api_tools.py` imports `tool_policy` directly; failure to import results in immediate tool failure (fail-closed).

## TOCTOU Race Condition Limitation and Compensating Controls

Path validation uses `os.path.realpath()` which resolves symlinks at call time. A hostile concurrent writer could theoretically swap a regular file to a symlink between validation and `subprocess.Popen()` execution (microsecond-scale race). True atomicity for all command types would require fd-based `openat2(O_NOFOLLOW|RESOLVE_BENEATH)` or mount namespace/chroot isolation, which are unavailable in this unprivileged sandbox environment (`openat2` not available in current glibc/kernel, `unshare` returns `EPERM` with `CAP_SYS_ADMIN` absent).

This limitation is formally documented and mitigated by the following enforced compensating controls:

1. **No Shell Interpretation (`shell=False`)**: All commands, including pipelines, are executed via `subprocess.Popen` chains with `shell=False`. This eliminates shell metacharacter injection or expansion attacks after validation.
2. **Mandatory Workspace Locking**: `api_tools.bash()` acquires an exclusive advisory lock (`flock`) on the workspace directory before command execution. This enforces a single-writer invariant, serializing all bash/write/edit operations. If locking primitives are unavailable, the tool fails closed (raises `ImportError`).
3. **Post-Execution Symlink Re-validation**: After pipeline execution, `bash()` re-resolves every path token of validated file-operating segments via `resolve_workspace_path()`. If a swap occurred during the execve window, the output is discarded and replaced with a denial (fail-closed), bounding the residual race to DoS rather than silent exfiltration.
4. **Symlink Component Denial**: `resolve_workspace_path()` rejects any path containing symlink components within the workspace boundary during validation.
5. **Glob Character Denial**: Glob characters (`*`, `?`, `[`, `]`, `{`, `}`, `~`) in path positions for file-operating commands are denied to prevent shell expansion from smuggling symlinks past validation.

Residual Risk: The post-execution check is itself non-atomic (an attacker may swap back before re-validation), so the TOCTOU race between path validation and `execve()` remains theoretically exploitable by a concurrent process with direct write access to the workspace. This is accepted under the compensating controls above, as OS-level isolation is unavailable. The workspace MUST be permissioned (`chmod 0700`, owned by agent user) and multi-tenant deployments REQUIRE container/VM isolation or OS-level mandatory access control (SELinux/AppArmor).
