"""Tests for findings_log.py persistence and rotation.

Covers:
- append_finding format validation (JSONL)
- read_findings limit enforcement
- rotate_findings file truncation behavior
"""

import json
import tempfile
from pathlib import Path

import pytest

from codebot.findings_log import (
    append_finding,
    read_findings,
    rotate_findings,
    FINDINGS_SCHEMA_KEYS,
    MAX_FINDINGS_LINES,
    MAX_FINDINGS_READ,
)


@pytest.fixture
def temp_findings_file(tmp_path: Path) -> Path:
    """Create a temporary findings file path."""
    return tmp_path / "test_findings.jsonl"


class TestAppendFinding:
    """Tests for append_finding function."""

    def test_append_finding_writes_valid_json_line(self, temp_findings_file: Path):
        """Verify append_finding writes a valid JSON line with all schema keys."""
        append_finding(
            bot="test_bot",
            finding_type="bug",
            module="test_module",
            finding="Test finding description",
            severity="high",
            path=temp_findings_file,
        )

        assert temp_findings_file.exists()
        content = temp_findings_file.read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        
        assert len(lines) == 1
        record = json.loads(lines[0])
        
        # Verify all schema keys are present
        assert FINDINGS_SCHEMA_KEYS.issubset(record.keys())
        assert record["bot"] == "test_bot"
        assert record["type"] == "bug"
        assert record["module"] == "test_module"
        assert record["finding"] == "Test finding description"
        assert record["severity"] == "high"
        assert "ts" in record
        assert isinstance(record["ts"], float)

    def test_append_finding_creates_parent_directory(self, tmp_path: Path):
        """Verify append_finding creates parent directories if they don't exist."""
        nested_path = tmp_path / "nested" / "dir" / "findings.jsonl"
        
        append_finding(
            bot="test_bot",
            finding_type="feature",
            module="test_module",
            finding="Test",
            severity="low",
            path=nested_path,
        )

        assert nested_path.exists()
        assert nested_path.parent.exists()

    def test_append_finding_multiple_appends(self, temp_findings_file: Path):
        """Verify multiple appends create multiple lines."""
        append_finding("bot1", "type1", "mod1", "finding1", "high", path=temp_findings_file)
        append_finding("bot2", "type2", "mod2", "finding2", "medium", path=temp_findings_file)
        append_finding("bot3", "type3", "mod3", "finding3", "low", path=temp_findings_file)

        content = temp_findings_file.read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        
        assert len(lines) == 3
        
        # Verify each line is valid JSON
        for i, line in enumerate(lines):
            record = json.loads(line)
            assert record["bot"] == f"bot{i+1}"


class TestReadFindings:
    """Tests for read_findings function."""

    def test_read_findings_empty_file(self, temp_findings_file: Path):
        """Verify read_findings returns empty list for non-existent file."""
        results = read_findings(path=temp_findings_file)
        assert results == []

    def test_read_findings_returns_all_when_under_limit(self, temp_findings_file: Path):
        """Verify read_findings returns all findings when under limit."""
        for i in range(5):
            append_finding(f"bot{i}", "type", "mod", f"finding{i}", "low", path=temp_findings_file)

        results = read_findings(path=temp_findings_file, limit=10)
        
        assert len(results) == 5
        assert results[0]["bot"] == "bot0"
        assert results[4]["bot"] == "bot4"

    def test_read_findings_respects_limit_parameter(self, temp_findings_file: Path):
        """Verify read_findings respects the limit parameter and returns most recent."""
        for i in range(100):
            append_finding(f"bot{i}", "type", "mod", f"finding{i}", "low", path=temp_findings_file)

        results = read_findings(path=temp_findings_file, limit=10)
        
        assert len(results) == 10
        # Should return the most recent 10
        assert results[0]["bot"] == "bot90"
        assert results[9]["bot"] == "bot99"

    def test_read_findings_default_limit(self, temp_findings_file: Path):
        """Verify read_findings uses default limit when not specified."""
        # Create more than MAX_FINDINGS_READ findings
        for i in range(MAX_FINDINGS_READ + 100):
            append_finding(f"bot{i}", "type", "mod", f"finding{i}", "low", path=temp_findings_file)

        results = read_findings(path=temp_findings_file)
        
        assert len(results) == MAX_FINDINGS_READ
        # Should be the most recent ones
        assert results[0]["bot"] == f"bot{100}"

    def test_read_findings_skips_corrupt_lines(self, temp_findings_file: Path):
        """Verify read_findings skips corrupt JSON lines without crashing."""
        # Write some valid and some corrupt lines
        content = [
            json.dumps({"ts": 1.0, "bot": "bot1", "type": "t", "module": "m", "finding": "f", "severity": "s"}),
            "not valid json",
            json.dumps({"ts": 2.0, "bot": "bot2", "type": "t", "module": "m", "finding": "f", "severity": "s"}),
            "{incomplete json",
            json.dumps({"ts": 3.0, "bot": "bot3", "type": "t", "module": "m", "finding": "f", "severity": "s"}),
        ]
        temp_findings_file.write_text("\n".join(content) + "\n", encoding="utf-8")

        results = read_findings(path=temp_findings_file)
        
        assert len(results) == 3
        assert results[0]["bot"] == "bot1"
        assert results[1]["bot"] == "bot2"
        assert results[2]["bot"] == "bot3"

    def test_read_findings_skips_lines_missing_schema_keys(self, temp_findings_file: Path):
        """Verify read_findings skips lines missing required schema keys."""
        content = [
            json.dumps({"ts": 1.0, "bot": "bot1", "type": "t", "module": "m", "finding": "f", "severity": "s"}),
            json.dumps({"ts": 2.0, "bot": "bot2"}),  # Missing keys
            json.dumps({"ts": 3.0, "bot": "bot3", "type": "t", "module": "m", "finding": "f", "severity": "s"}),
        ]
        temp_findings_file.write_text("\n".join(content) + "\n", encoding="utf-8")

        results = read_findings(path=temp_findings_file)
        
        assert len(results) == 2
        assert results[0]["bot"] == "bot1"
        assert results[1]["bot"] == "bot3"

    def test_read_findings_handles_empty_lines(self, temp_findings_file: Path):
        """Verify read_findings handles files with empty lines."""
        content = [
            json.dumps({"ts": 1.0, "bot": "bot1", "type": "t", "module": "m", "finding": "f", "severity": "s"}),
            "",
            "   ",
            json.dumps({"ts": 2.0, "bot": "bot2", "type": "t", "module": "m", "finding": "f", "severity": "s"}),
        ]
        temp_findings_file.write_text("\n".join(content) + "\n", encoding="utf-8")

        results = read_findings(path=temp_findings_file)
        
        assert len(results) == 2


class TestRotateFindings:
    """Tests for rotate_findings function."""

    def test_rotate_findings_no_op_when_under_limit(self, temp_findings_file: Path):
        """Verify rotate_findings does nothing when file is under max_lines."""
        for i in range(100):
            append_finding(f"bot{i}", "type", "mod", f"finding{i}", "low", path=temp_findings_file)

        original_content = temp_findings_file.read_text(encoding="utf-8")
        rotate_findings(path=temp_findings_file, max_lines=1000)
        new_content = temp_findings_file.read_text(encoding="utf-8")

        assert original_content == new_content

    def test_rotate_findings_truncates_when_over_limit(self, temp_findings_file: Path):
        """Verify rotate_findings truncates file to max_lines keeping most recent."""
        # Create file with 150 lines
        for i in range(150):
            append_finding(f"bot{i}", "type", "mod", f"finding{i}", "low", path=temp_findings_file)

        rotate_findings(path=temp_findings_file, max_lines=100)

        content = temp_findings_file.read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        
        assert len(lines) == 100
        # Should keep the most recent 100 (bot50 to bot149)
        first_record = json.loads(lines[0])
        last_record = json.loads(lines[-1])
        
        assert first_record["bot"] == "bot50"
        assert last_record["bot"] == "bot149"

    def test_rotate_findings_exact_limit(self, temp_findings_file: Path):
        """Verify rotate_findings handles exact limit boundary."""
        for i in range(100):
            append_finding(f"bot{i}", "type", "mod", f"finding{i}", "low", path=temp_findings_file)

        rotate_findings(path=temp_findings_file, max_lines=100)

        content = temp_findings_file.read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        
        assert len(lines) == 100
        assert json.loads(lines[0])["bot"] == "bot0"
        assert json.loads(lines[-1])["bot"] == "bot99"

    def test_rotate_findings_preserves_json_validity(self, temp_findings_file: Path):
        """Verify rotate_findings produces valid JSONL after truncation."""
        for i in range(200):
            append_finding(f"bot{i}", "type", "mod", f"finding{i}", "low", path=temp_findings_file)

        rotate_findings(path=temp_findings_file, max_lines=150)

        content = temp_findings_file.read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        
        assert len(lines) == 150
        
        # Verify every line is valid JSON with schema keys
        for line in lines:
            record = json.loads(line)
            assert FINDINGS_SCHEMA_KEYS.issubset(record.keys())

    def test_rotate_findings_non_existent_file(self, temp_findings_file: Path):
        """Verify rotate_findings handles non-existent file gracefully."""
        # Should not raise any exception
        rotate_findings(path=temp_findings_file, max_lines=100)
        assert not temp_findings_file.exists()

    def test_rotate_findings_appends_newline_after_truncation(self, temp_findings_file: Path):
        """Verify rotate_findings ensures file ends with newline after truncation."""
        for i in range(150):
            append_finding(f"bot{i}", "type", "mod", f"finding{i}", "low", path=temp_findings_file)

        rotate_findings(path=temp_findings_file, max_lines=100)

        content = temp_findings_file.read_text(encoding="utf-8")
        
        assert content.endswith("\n")


class TestIntegration:
    """Integration tests combining append, read, and rotate."""

    def test_append_rotate_read_workflow(self, temp_findings_file: Path):
        """Verify complete workflow: append many, rotate, read limited."""
        # Append 200 findings
        for i in range(200):
            append_finding(f"bot{i}", "type", "mod", f"finding{i}", "low", path=temp_findings_file)

        # Rotate to keep only 100
        rotate_findings(path=temp_findings_file, max_lines=100)

        # Read only 50
        results = read_findings(path=temp_findings_file, limit=50)

        assert len(results) == 50
        # Should be the most recent 50 from the rotated set (bot150 to bot199)
        assert results[0]["bot"] == "bot150"
        assert results[-1]["bot"] == "bot199"

    def test_concurrent_append_simulation(self, temp_findings_file: Path):
        """Simulate concurrent appends and verify integrity."""
        bots = ["bot_a", "bot_b", "bot_c"]
        
        # Simulate interleaved appends
        for i in range(30):
            for bot in bots:
                append_finding(bot, "type", "mod", f"finding_{i}_{bot}", "medium", path=temp_findings_file)

        results = read_findings(path=temp_findings_file)
        
        assert len(results) == 90
        
        # Verify all records are valid
        for record in results:
            assert FINDINGS_SCHEMA_KEYS.issubset(record.keys())
            assert record["bot"] in bots
