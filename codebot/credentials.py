#!/usr/bin/env python3
"""Credential manager for CodeBot git and API operations.

Purpose
-------
Resolves SSH keys, GitHub tokens, and API credentials from environment
variables, secret files, or adapter configuration. Never hardcodes secrets.

Why
---
CodeBot operates as a standalone entity managing its own repository.
It needs its own credential resolution that works both locally (env vars)
and in containers (secrets mounted as env vars).

Invariants
----------
- stdlib-only
- Secrets never logged or written to state files
- Environment variables take precedence over file-based secrets
- Missing credentials degrade gracefully (operations fail, don't crash)
"""

from __future__ import annotations

import logging
import os
import shlex
import stat
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def get_github_token() -> str:
    return (
        os.environ.get("GH_TOKEN", "")
        or os.environ.get("GITHUB_TOKEN", "")
        or _read_secret_file("gh_token")
    )


def get_control_token() -> str:
    return (
        os.environ.get("CONTROL_TOKEN", "")
        or _read_secret_file("control_token")
    )


def get_ssh_key_path() -> Path | None:
    env_path = os.environ.get("SSH_PRIVATE_KEY_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
    ssh_dir = Path.home() / ".ssh"
    for name in ("id_ecdsa", "id_ed25519", "id_rsa"):
        p = ssh_dir / name
        if p.exists():
            return p
    return None


def get_ssh_auth_sock() -> str:
    return os.environ.get("SSH_AUTH_SOCK", "")


def _get_known_hosts_path() -> str:
    """Return absolute path to the bundled GitHub known_hosts file."""
    return str(Path(__file__).resolve().parent / "resources" / "github_known_hosts")


def get_git_ssh_command() -> str:
    key_path = get_ssh_key_path()
    known_hosts = _get_known_hosts_path()
    base = (
        f"ssh -o StrictHostKeyChecking=yes "
        f"-o UserKnownHostsFile={shlex.quote(known_hosts)}"
    )
    if key_path:
        # Quote the key path to prevent shell injection.
        # Note: pathlib normalizes paths (e.g. stripping trailing slashes),
        # so we quote whatever string representation it provides.
        return f"{base} -o IdentitiesOnly=yes -i {shlex.quote(str(key_path))}"
    return base


def get_dry_run() -> bool:
    val = os.environ.get("GITHUB_DRY_RUN", "1").lower()
    return val in ("1", "true", "yes", "on")


def setup_git_environment() -> dict[str, str]:
    env = dict(os.environ)
    ssh_cmd = get_git_ssh_command()
    if ssh_cmd:
        env["GIT_SSH_COMMAND"] = ssh_cmd
    sock = get_ssh_auth_sock()
    if sock:
        env["SSH_AUTH_SOCK"] = sock
    token = get_github_token()
    if token and not get_dry_run():
        env["GH_TOKEN"] = token
    return env


def _read_secret_file(name: str) -> str:
    """Read secret from file with 4KB size bound to prevent memory exhaustion."""
    max_size = 4096  # 4KB limit
    custom_secret_path = os.environ.get("CODEBOT_SECRET_PATH")
    if custom_secret_path:
        candidates = [Path(custom_secret_path) / f"{name}.txt"]
    else:
        candidates = [
            Path.home() / ".config" / "opencode" / "botnet.env",
            Path.home() / ".config" / "codebot" / f"{name}.txt",
        ]
    for path in candidates:
        if path.exists():
            try:
                file_stat = path.stat()

                # Validate file permissions: warn if group/other can access
                mode = stat.S_IMODE(file_stat.st_mode)
                insecure_mask = stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH
                if mode & insecure_mask:
                    logger.warning(
                        "Secret file %s has insecure permissions: %s (expected 0o600 or stricter)",
                        path,
                        oct(mode),
                    )

                # Check file size before reading
                file_size = file_stat.st_size
                if file_size > max_size:
                    # Reject oversized files to prevent partial reads and potential memory issues
                    continue

                # Read with size bound
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read(max_size)

                text = text.strip()
                if text:
                    for line in text.splitlines():
                        line = line.strip()
                        if line.startswith(f"{name}="):
                            return line.split("=", 1)[1].strip().strip('"').strip("'")
                        if line and not line.startswith("#") and not "=" in line:
                            return line
            except OSError:
                continue
    return ""
