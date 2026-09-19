"""Tests for findings_log.py path resolution.

Verifies that DEFAULT_FINDINGS_PATH resolves to .codebot/state/findings.jsonl
(project state directory) rather than codebot/state/findings.jsonl (source package).

Ticket: CB-6797169-601B
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codebot.findings_log import (
    DEFAULT_FINDINGS_PATH,
    STATE_DIR,
    append_finding,
    read_findings,
)


class TestFindingsLogPathResolution:
    """Verify findings_log writes to the project state directory."""

    def test_state_dir_is_project_dot_codebot_state(self) -> None:
        """STATE_DIR must resolve to <project_root>/.codebot/state, not codebot/state."""
        # STATE_DIR should contain '.codebot' as a component
        parts = STATE_DIR.parts
        assert ".codebot" in parts, (
            f"STATE_DIR {STATE_DIR} does not contain '.codebot' component. "
            "Findings would be written inside the source package."
        )

    def test_default_findings_path_under_dot_codebot(self) -> None:
        """DEFAULT_FINDINGS_PATH must be under .codebot/state/, not codebot/state/."""
        path_str = str(DEFAULT_FINDINGS_PATH)
        assert ".codebot" in path_str, (
            f"DEFAULT_FINDINGS_PATH {DEFAULT_FINDINGS_PATH} is not under .codebot/. "
            "Discovery agents writing here will be invisible to the scheduler."
        )
        assert path_str.endswith("findings.jsonl")

    def test_default_findings_path_not_in_source_package(self) -> None:
        """Ensure findings path doesn't resolve inside codebot/ source directory."""
        # The path should NOT be codebot/state/findings.jsonl
        # It SHOULD be .codebot/state/findings.jsonl
        parts = DEFAULT_FINDINGS_PATH.parts
        # Check that 'state' is preceded by '.codebot', not 'codebot'
        state_idx = None
        for i, part in enumerate(parts):
            if part == "state":
                state_idx = i
                break
        assert state_idx is not None, "No 'state' directory in path"
        parent_dir = parts[state_idx - 1]
        assert parent_dir == ".codebot", (
            f"'state' directory parent is '{parent_dir}', expected '.codebot'. "
            "Findings are being written to wrong location."
        )


class TestFindingsLogReadWrite:
    """Verify append and read work with explicit tmp_path."""

    def test_append_and_read_roundtrip(self, tmp_path: Path) -> None:
        """Findings written via append_finding are readable via read_findings."""
        findings_file = tmp_path / "findings.jsonl"
        append_finding(
            bot="test_bot",
            finding_type="bug",
            module="test_module.py",
            finding="Test finding description",
            severity="medium",
            path=findings_file,
        )
        results = read_findings(path=findings_file)
        assert len(results) == 1
        assert results[0]["bot"] == "test_bot"
        assert results[0]["type"] == "bug"
        assert results[0]["module"] == "test_module.py"
        assert results[0]["finding"] == "Test finding description"
        assert results[0]["severity"] == "medium"
        assert "ts" in results[0]

    def test_read_findings_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """read_findings on nonexistent file returns empty list."""
        missing = tmp_path / "nonexistent.jsonl"
        assert read_findings(path=missing) == []

    def test_read_findings_skips_corrupt_lines(self, tmp_path: Path) -> None:
        """Corrupt JSON lines are skipped without crashing."""
        findings_file = tmp_path / "findings.jsonl"
        # Write one valid and one corrupt line
        valid_record = {
            "ts": 1000.0,
            "bot": "good_bot",
            "type": "bug",
            "module": "mod.py",
            "finding": "valid",
            "severity": "low",
        }
        findings_file.write_text(
            json.dumps(valid_record) + "\n{corrupt json\n",
            encoding="utf-8",
        )
        results = read_findings(path=findings_file)
        assert len(results) == 1
        assert results[0]["bot"] == "good_bot"

    def test_read_findings_respects_limit(self, tmp_path: Path) -> None:
        """read_findings only returns last N entries per limit parameter."""
        findings_file = tmp_path / "findings.jsonl"
        for i in range(10):
            append_finding(
                bot=f"bot_{i}",
                finding_type="bug",
                module="m.py",
                finding=f"f{i}",
                severity="low",
                path=findings_file,
            )
        results = read_findings(path=findings_file, limit=3)
        assert len(results) == 3
        # Should be the last 3
        assert results[0]["bot"] == "bot_7"
        assert results[2]["bot"] == "bot_9"
