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

import os
from pathlib import Path
from typing import Any


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


def get_git_ssh_command() -> str:
    key_path = get_ssh_key_path()
    if key_path:
        return f"ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    return "ssh -o StrictHostKeyChecking=accept-new"


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
    candidates = [
        Path.home() / ".config" / "opencode" / "botnet.env",
        Path.home() / ".config" / "codebot" / f"{name}.txt",
    ]
    for path in candidates:
        if path.exists():
            try:
                # Check file size before reading
                file_size = path.stat().st_size
                if file_size > max_size:
                    # Log warning via print/stderr since we can't import logging easily without context
                    # In a real app, use proper logging. Here we just truncate.
                    pass  # We will read only first 4KB below
                
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
