#!/usr/bin/env python3
"""Tests for codebot.credentials module.

Covers:
- get_github_token: env precedence (GH_TOKEN > GITHUB_TOKEN > file)
- get_control_token: env precedence (CONTROL_TOKEN > file)
- get_ssh_key_path: env path, ~/.ssh fallback, missing keys
- get_ssh_auth_sock: env var passthrough
- get_git_ssh_command: key present vs absent, StrictHostKeyChecking=accept-new
- get_dry_run: truthy/falsy parsing
- setup_git_environment: correct env dict assembly
- _read_secret_file: key=value, bare value, comments, missing file
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from codebot import credentials


class TestGetGithubToken:
    def test_gh_token_env_precedence(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "gh-123")
        monkeypatch.setenv("GITHUB_TOKEN", "gh-456")
        assert credentials.get_github_token() == "gh-123"

    def test_github_token_env_fallback(self, monkeypatch):
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "gh-456")
        assert credentials.get_github_token() == "gh-456"

    def test_empty_when_missing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        # Patch secret file to return empty
        with patch.object(credentials, "_read_secret_file", return_value=""):
            assert credentials.get_github_token() == ""


class TestGetControlToken:
    def test_env_precedence(self, monkeypatch):
        monkeypatch.setenv("CONTROL_TOKEN", "ctrl-abc")
        assert credentials.get_control_token() == "ctrl-abc"

    def test_fallback_to_file(self, monkeypatch):
        monkeypatch.delenv("CONTROL_TOKEN", raising=False)
        with patch.object(credentials, "_read_secret_file", return_value="ctrl-file"):
            assert credentials.get_control_token() == "ctrl-file"

    def test_empty_when_missing(self, monkeypatch):
        monkeypatch.delenv("CONTROL_TOKEN", raising=False)
        with patch.object(credentials, "_read_secret_file", return_value=""):
            assert credentials.get_control_token() == ""


class TestGetSshKeyPath:
    def test_env_path_valid(self, monkeypatch, tmp_path):
        key_file = tmp_path / "my_key"
        key_file.touch()
        monkeypatch.setenv("SSH_PRIVATE_KEY_PATH", str(key_file))
        assert credentials.get_ssh_key_path() == key_file

    def test_env_path_missing_falls_back(self, monkeypatch, tmp_path):
        non_existent = tmp_path / "no_such_key"
        monkeypatch.setenv("SSH_PRIVATE_KEY_PATH", str(non_existent))
        # Should fall back to ~/.ssh scan. We'll mock home to a temp dir with no keys.
        with patch.object(Path, "home", return_value=tmp_path):
            result = credentials.get_ssh_key_path()
            assert result is None

    def test_ssh_dir_fallback_ecdsa(self, monkeypatch, tmp_path):
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        key = ssh_dir / "id_ecdsa"
        key.touch()
        monkeypatch.delenv("SSH_PRIVATE_KEY_PATH", raising=False)
        with patch.object(Path, "home", return_value=tmp_path):
            assert credentials.get_ssh_key_path() == key

    def test_ssh_dir_fallback_ed25519(self, monkeypatch, tmp_path):
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        key = ssh_dir / "id_ed25519"
        key.touch()
        monkeypatch.delenv("SSH_PRIVATE_KEY_PATH", raising=False)
        with patch.object(Path, "home", return_value=tmp_path):
            assert credentials.get_ssh_key_path() == key

    def test_ssh_dir_fallback_rsa(self, monkeypatch, tmp_path):
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        key = ssh_dir / "id_rsa"
        key.touch()
        monkeypatch.delenv("SSH_PRIVATE_KEY_PATH", raising=False)
        with patch.object(Path, "home", return_value=tmp_path):
            assert credentials.get_ssh_key_path() == key

    def test_none_when_no_keys(self, monkeypatch, tmp_path):
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        monkeypatch.delenv("SSH_PRIVATE_KEY_PATH", raising=False)
        with patch.object(Path, "home", return_value=tmp_path):
            assert credentials.get_ssh_key_path() is None


class TestGetSshAuthSock:
    def test_returns_env_value(self, monkeypatch):
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
        assert credentials.get_ssh_auth_sock() == "/tmp/agent.sock"

    def test_empty_when_missing(self, monkeypatch):
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
        assert credentials.get_ssh_auth_sock() == ""


class TestGetGitSshCommand:
    def test_with_key_path(self, monkeypatch, tmp_path):
        key = tmp_path / "key"
        key.touch()
        monkeypatch.setenv("SSH_PRIVATE_KEY_PATH", str(key))
        cmd = credentials.get_git_ssh_command()
        assert f"-i {key}" in cmd
        assert "StrictHostKeyChecking=yes" in cmd
        assert "IdentitiesOnly=yes" in cmd
        assert "UserKnownHostsFile=" in cmd

    def test_without_key_path(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SSH_PRIVATE_KEY_PATH", raising=False)
        with patch.object(Path, "home", return_value=tmp_path):
            cmd = credentials.get_git_ssh_command()
            assert "-i " not in cmd
            assert "StrictHostKeyChecking=yes" in cmd
            assert "UserKnownHostsFile=" in cmd

    def test_strict_host_key_rejects_accept_new(self, monkeypatch, tmp_path):
        """CB-1061483-B8B3: accept-new must NOT be used — it allows MITM on first connect."""
        monkeypatch.delenv("SSH_PRIVATE_KEY_PATH", raising=False)
        with patch.object(Path, "home", return_value=tmp_path):
            cmd = credentials.get_git_ssh_command()
        assert "accept-new" not in cmd, (
            "StrictHostKeyChecking=accept-new allows MITM on first connection. "
            "Use 'yes' with a pre-populated known_hosts instead."
        )
        assert "StrictHostKeyChecking=no" not in cmd, (
            "StrictHostKeyChecking=no disables host key verification entirely."
        )

    def test_uses_bundled_known_hosts_not_system(self, monkeypatch, tmp_path):
        """CB-1061483-B8B3: SSH must use bundled known_hosts to prevent MITM."""
        monkeypatch.delenv("SSH_PRIVATE_KEY_PATH", raising=False)
        with patch.object(Path, "home", return_value=tmp_path):
            cmd = credentials.get_git_ssh_command()
        bundled_path = credentials._get_known_hosts_path()
        assert bundled_path in cmd, (
            f"SSH command must reference bundled known_hosts at {bundled_path}"
        )

    def test_bundled_known_hosts_contains_only_github(self):
        """CB-1061483-B8B3: bundled known_hosts must only contain github.com entries."""
        import re
        hosts_path = credentials._get_known_hosts_path()
        with open(hosts_path, "r") as f:
            lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        hosts = set()
        for line in lines:
            # known_hosts format: <host> <keytype> <key> [comment]
            parts = line.split()
            if parts:
                hosts.add(parts[0])
        assert hosts == {"github.com"}, (
            f"Expected only github.com in bundled known_hosts, got: {hosts}. "
            "Only pre-pinned hosts should be trusted to prevent MITM."
        )


class TestGetDryRun:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1", True),
            ("true", True),
            ("yes", True),
            ("on", True),
            ("TRUE", True),
            ("0", False),
            ("false", False),
            ("no", False),
            ("off", False),
            ("", False),
        ],
    )
    def test_parsing(self, monkeypatch, value, expected):
        monkeypatch.setenv("GITHUB_DRY_RUN", value)
        assert credentials.get_dry_run() is expected

    def test_default_is_true(self, monkeypatch):
        monkeypatch.delenv("GITHUB_DRY_RUN", raising=False)
        assert credentials.get_dry_run() is True


class TestSetupGitEnvironment:
    def test_includes_ssh_command(self, monkeypatch, tmp_path):
        key = tmp_path / "key"
        key.touch()
        monkeypatch.setenv("SSH_PRIVATE_KEY_PATH", str(key))
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_DRY_RUN", "1")  # dry run so token isn't added

        env = credentials.setup_git_environment()
        assert "GIT_SSH_COMMAND" in env
        assert str(key) in env["GIT_SSH_COMMAND"]

    def test_includes_auth_sock(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SSH_PRIVATE_KEY_PATH", raising=False)
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/sock")
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_DRY_RUN", "1")

        with patch.object(Path, "home", return_value=tmp_path):
            env = credentials.setup_git_environment()
        assert env.get("SSH_AUTH_SOCK") == "/tmp/sock"

    def test_includes_gh_token_when_not_dry_run(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SSH_PRIVATE_KEY_PATH", raising=False)
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
        monkeypatch.setenv("GH_TOKEN", "secret-token")
        monkeypatch.setenv("GITHUB_DRY_RUN", "0")

        with patch.object(Path, "home", return_value=tmp_path):
            env = credentials.setup_git_environment()
        assert env.get("GH_TOKEN") == "secret-token"

    def test_excludes_gh_token_when_dry_run(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SSH_PRIVATE_KEY_PATH", raising=False)
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_DRY_RUN", "1")

        with patch.object(Path, "home", return_value=tmp_path):
            with patch.object(credentials, "_read_secret_file", return_value="secret-token"):
                env = credentials.setup_git_environment()
        # GH_TOKEN should not be added by setup_git_environment when dry run is active
        # Note: dict(os.environ) won't contain GH_TOKEN since we deleted it above
        assert env.get("GH_TOKEN") != "secret-token"


class TestReadSecretFile:
    def test_key_value_format(self, tmp_path):
        config_dir = tmp_path / ".config" / "codebot"
        config_dir.mkdir(parents=True)
        secret_file = config_dir / "test_secret.txt"
        secret_file.write_text("test_secret=my-secret-value\n")

        with patch.object(Path, "home", return_value=tmp_path):
            result = credentials._read_secret_file("test_secret")
        assert result == "my-secret-value"

    def test_bare_value_format(self, tmp_path):
        config_dir = tmp_path / ".config" / "codebot"
        config_dir.mkdir(parents=True)
        secret_file = config_dir / "test_secret.txt"
        secret_file.write_text("just-the-value\n")

        with patch.object(Path, "home", return_value=tmp_path):
            result = credentials._read_secret_file("test_secret")
        assert result == "just-the-value"

    def test_skips_comments(self, tmp_path):
        config_dir = tmp_path / ".config" / "codebot"
        config_dir.mkdir(parents=True)
        secret_file = config_dir / "test_secret.txt"
        secret_file.write_text("# comment\ntest_secret=value-after-comment\n")

        with patch.object(Path, "home", return_value=tmp_path):
            result = credentials._read_secret_file("test_secret")
        assert result == "value-after-comment"

    def test_quoted_values(self, tmp_path):
        config_dir = tmp_path / ".config" / "codebot"
        config_dir.mkdir(parents=True)
        secret_file = config_dir / "test_secret.txt"
        secret_file.write_text('test_secret="quoted-value"\n')

        with patch.object(Path, "home", return_value=tmp_path):
            result = credentials._read_secret_file("test_secret")
        assert result == "quoted-value"

    def test_missing_file_returns_empty(self, tmp_path):
        with patch.object(Path, "home", return_value=tmp_path):
            result = credentials._read_secret_file("nonexistent")
        assert result == ""

    def test_botnet_env_fallback(self, tmp_path):
        opencode_dir = tmp_path / ".config" / "opencode"
        opencode_dir.mkdir(parents=True)
        botnet_env = opencode_dir / "botnet.env"
        botnet_env.write_text("gh_token=token-from-botnet-env\n")

        with patch.object(Path, "home", return_value=tmp_path):
            result = credentials._read_secret_file("gh_token")
        assert result == "token-from-botnet-env"
