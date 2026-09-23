# Security & Credential Management

This document outlines how CodeBot handles secrets, credentials, sandboxing, and secure communications.

## Sandbox Security Model & TOCTOU Mitigation

### Threat Model: TOCTOU Race Conditions

The `bash()` tool executes shell commands within a workspace boundary. A Time-of-Check-to-Time-of-Use (TOCTOU) race condition exists where a hostile concurrent process could swap a validated file path with a symlink pointing outside the workspace between the validation step (`resolve_workspace_path`) and the execution step (`subprocess.Popen`).

### Compensating Controls (Enforced)

To mitigate this risk without privileged OS-level isolation (which is unavailable in the standard unprivileged deployment context), CodeBot enforces the following compensating controls:

1.  **Fail-Closed Mandatory Locking**: All API tools that perform I/O (`bash`, `write`, `edit`) acquire an exclusive advisory lock (`flock`) on the workspace root before validating paths or executing commands. If locking primitives are unavailable on the host platform, the system raises a `RuntimeError` and refuses to start. This enforces a **Single-Writer Invariant** for all code using the API tools, serializing access and closing the TOCTOU window for cooperating processes.
2.  **Symlink Component Rejection**: The `resolve_workspace_path` function walks each component of the path within the workspace boundary. If any component is a symlink, the path is rejected immediately. This prevents in-workspace symlink swaps from being used to escape the boundary, regardless of timing.
3.  **No Shell Interpretation**: The `bash()` tool never uses `shell=True`. Commands are parsed into argument lists and executed via `subprocess.Popen` chains. Pipelines are orchestrated in Python. This prevents shell metacharacter injection and ensures that only validated arguments are passed to the kernel.
4.  **Glob Character Denial**: Path arguments containing shell expansion characters (`*`, `?`, `[]`, `{}`, `~`) are rejected for file-operating commands to prevent expansion from smuggling symlinks past validation.

### Residual Risk & Limitations

Advisory locks (`flock`) do not prevent non-cooperating processes (those that do not use the API tools and thus do not acquire the lock) from modifying the filesystem. A hostile process with direct write access to the workspace could theoretically swap a symlink between validation and execution if it bypasses the API layer.

**Mitigation**: The workspace directory should be permissioned to restrict write access to the CodeBot user/process only. For environments requiring protection against hostile concurrent writers with direct filesystem access, OS-level isolation (containerization, mount namespaces, or chroot) must be provided by the hosting infrastructure. CodeBot documents this limitation and relies on the above compensating controls for standard deployments.

## Credential Resolution

CodeBot resolves credentials using a strict precedence order: **Environment Variables** take precedence over **File-Based Secrets**. This allows flexible deployment in both local development environments and containerized production systems.

### GitHub Token

Used for API interactions with GitHub (e.g., creating issues, pushing commits).

1.  **Environment Variables**: 
    *   `GH_TOKEN`
    *   `GITHUB_TOKEN`
2.  **File-Based Secret**: 
    *   Resolved via `_read_secret_file("gh_token")`.
    *   Looks in `~/.config/opencode/botnet.env` or `~/.config/codebot/gh_token.txt`.

### Control Token

Used for authenticating requests to the CodeBot Control Server.

1.  **Environment Variable**: 
    *   `CONTROL_TOKEN`
2.  **File-Based Secret**: 
    *   Resolved via `_read_secret_file("control_token")`.
    *   Looks in `~/.config/opencode/botnet.env` or `~/.config/codebot/control_token.txt`.

### SSH Key

Used for Git operations over SSH.

1.  **Environment Variable**: 
    *   `SSH_PRIVATE_KEY_PATH`: Absolute path to the private key file.
2.  **Default Paths**: 
    *   If no env var is set, CodeBot searches `~/.ssh/` in this order:
        1.  `id_ecdsa`
        2.  `id_ed25519`
        3.  `id_rsa`

### Secret File Format

File-based secrets are read from two primary locations:

1.  **Shared Env File**: `~/.config/opencode/botnet.env`
    *   Supports `KEY=VALUE` format.
    *   Supports bare values (single line per file).
    *   Lines starting with `#` are ignored.
2.  **Individual Files**: `~/.config/codebot/{name}.txt`
    *   Contains the raw secret value.

**Note**: Secret files are bounded to 4KB. Larger files are ignored for security.

## SSH Host Key Verification

CodeBot enforces strict SSH host key verification to prevent Man-in-the-Middle (MITM) attacks.

*   **Strict Mode**: `StrictHostKeyChecking=yes` is always enabled.
*   **Known Hosts**: CodeBot uses a bundled known_hosts file located at `codebot/resources/github_known_hosts` to verify GitHub's host keys.

## Best Practices

1.  **Prefer Environment Variables**: In containerized environments (Docker, Kubernetes, Fly.io), inject secrets as environment variables.
2.  **File Permissions**: Ensure secret files are readable only by the user running CodeBot (mode `0600`).
3.  **Rotate Regularly**: Rotate tokens and keys periodically.
4.  **Never Commit Secrets**: Never hardcode secrets in source code or commit secret files to the repository.
