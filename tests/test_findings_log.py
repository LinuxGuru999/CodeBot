"""Tests for findings_log.py — findings persistence, rotation, retrieval."""

import json
import os
import time
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.findings_log import (
    FINDINGS_SCHEMA_KEYS,
    MAX_FINDINGS_LINES,
    MAX_FINDINGS_READ,
    append_finding,
    get_default_findings_path,
    read_findings,
    rotate_findings,
)


class TestAppendFinding:
    def test_basic_append(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        append_finding("bot1", "bug", "mod.py", "finding text", "high", path=p)
        findings = read_findings(path=p)
        assert len(findings) == 1
        assert findings[0]["bot"] == "bot1"
        assert findings[0]["type"] == "bug"
        assert findings[0]["module"] == "mod.py"
        assert findings[0]["finding"] == "finding text"
        assert findings[0]["severity"] == "high"
        assert "ts" in findings[0]

    def test_multiple_appends(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        for i in range(5):
            append_finding(f"bot{i}", "bug", "m.py", f"f{i}", "low", path=p)
        assert len(read_findings(path=p)) == 5

    def test_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "a" / "b" / "findings.jsonl"
        append_finding("bot", "bug", "m.py", "finding", "low", path=p)
        assert p.exists()

    def test_jsonl_format(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        append_finding("bot", "bug", "m.py", "text", "low", path=p)
        lines = p.read_text(encoding="utf-8").splitlines()
        obj = json.loads(lines[0])
        assert FINDINGS_SCHEMA_KEYS.issubset(obj.keys())

    def test_timestamp_recent(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        before = time.time()
        append_finding("bot", "bug", "m.py", "text", "low", path=p)
        after = time.time()
        findings = read_findings(path=p)
        assert before <= findings[0]["ts"] <= after

    def test_different_severities(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        for sev in ("low", "medium", "high", "critical"):
            append_finding("bot", "bug", "m.py", f"sev {sev}", sev, path=p)
        findings = read_findings(path=p)
        assert {f["severity"] for f in findings} == {"low", "medium", "high", "critical"}

    def test_different_types(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        for ftype in ("bug", "security", "performance", "test_gap"):
            append_finding("bot", ftype, "m.py", "text", "low", path=p)
        findings = read_findings(path=p)
        assert {f["type"] for f in findings} == {"bug", "security", "performance", "test_gap"}


class TestReadFindings:
    def test_missing_file_returns_empty(self, tmp_path):
        assert read_findings(path=tmp_path / "missing.jsonl") == []

    def test_limit_respected(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        for i in range(10):
            append_finding(f"bot{i}", "bug", "m.py", f"f{i}", "low", path=p)
        assert len(read_findings(path=p, limit=3)) == 3
        assert read_findings(path=p, limit=3)[0]["bot"] == "bot7"

    def test_default_limit(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        for i in range(5):
            append_finding(f"bot{i}", "bug", "m.py", f"f{i}", "low", path=p)
        assert len(read_findings(path=p)) == 5

    def test_skips_corrupt_lines(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        p.write_text(
            json.dumps({"ts": 1.0, "bot": "good", "type": "bug", "module": "m.py", "finding": "ok", "severity": "low"}) + "\n"
            + "{corrupt\n"
            + json.dumps({"ts": 2.0, "bot": "good2", "type": "bug", "module": "m.py", "finding": "ok2", "severity": "low"}) + "\n",
            encoding="utf-8",
        )
        findings = read_findings(path=p)
        assert len(findings) == 2

    def test_skips_incomplete_schema(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        p.write_text(
            json.dumps({"ts": 1.0, "bot": "bad"}) + "\n"
            + json.dumps({"ts": 2.0, "bot": "good", "type": "bug", "module": "m.py", "finding": "ok", "severity": "low"}) + "\n",
            encoding="utf-8",
        )
        findings = read_findings(path=p)
        assert len(findings) == 1
        assert findings[0]["bot"] == "good"

    def test_skips_empty_lines(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        append_finding("bot", "bug", "m.py", "text", "low", path=p)
        content = p.read_text(encoding="utf-8")
        p.write_text(content + "\n\n", encoding="utf-8")
        assert len(read_findings(path=p)) == 1

    def test_returns_tail(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        for i in range(10):
            append_finding(f"bot{i}", "bug", "m.py", f"f{i}", "low", path=p)
        findings = read_findings(path=p, limit=2)
        assert findings[0]["bot"] == "bot8"
        assert findings[1]["bot"] == "bot9"


class TestRotateFindings:
    def test_no_rotate_under_limit(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        for i in range(5):
            append_finding(f"bot{i}", "bug", "m.py", f"f{i}", "low", path=p)
        rotate_findings(path=p, max_lines=10)
        assert len(read_findings(path=p)) == 5

    def test_rotate_truncates(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        for i in range(10):
            append_finding(f"bot{i}", "bug", "m.py", f"f{i}", "low", path=p)
        rotate_findings(path=p, max_lines=5)
        findings = read_findings(path=p)
        assert len(findings) == 5
        assert findings[0]["bot"] == "bot5"

    def test_rotate_missing_file_noop(self, tmp_path):
        rotate_findings(path=tmp_path / "missing.jsonl", max_lines=5)

    def test_append_triggers_rotation(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        for i in range(MAX_FINDINGS_LINES + 5):
            record = {"ts": float(i), "bot": f"bot{i}", "type": "bug", "module": "m.py", "finding": f"f{i}", "severity": "low"}
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        rotate_findings(path=p)
        lines = p.read_text(encoding="utf-8").splitlines()
        assert len(lines) == MAX_FINDINGS_LINES

    def test_rotate_exact_limit_no_truncation(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        for i in range(5):
            append_finding(f"bot{i}", "bug", "m.py", f"f{i}", "low", path=p)
        rotate_findings(path=p, max_lines=5)
        assert len(read_findings(path=p)) == 5


class TestFindingsSchema:
    def test_schema_keys(self):
        assert FINDINGS_SCHEMA_KEYS == frozenset({"ts", "bot", "type", "module", "finding", "severity"})

    def test_constants(self):
        assert MAX_FINDINGS_LINES == 10000
        assert MAX_FINDINGS_READ == 5000

    def test_path_resolution_via_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEBOT_STATE_DIR", str(tmp_path))
        import importlib
        import codebot.findings_log as fl
        path = fl._resolve_state_dir()
        assert path == tmp_path

    def test_path_resolution_via_project_root_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CODEBOT_STATE_DIR", raising=False)
        monkeypatch.setenv("CODEBOT_PROJECT_ROOT", str(tmp_path))
        import codebot.findings_log as fl
        assert fl._resolve_state_dir() == tmp_path / ".codebot" / "state"

    def test_get_default_findings_path(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        findings = read_findings(path=p)
        assert findings == []
        append_finding("bot", "bug", "m.py", "text", "low", path=p)
        assert len(read_findings(path=p)) == 1


class TestDeduplicationAwareness:
    def test_same_finding_appended_twice_both_stored(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        append_finding("bot", "bug", "m.py", "same text", "low", path=p)
        append_finding("bot", "bug", "m.py", "same text", "low", path=p)
        assert len(read_findings(path=p)) == 2

    def test_findings_ordered_by_append_time(self, tmp_path):
        p = tmp_path / "findings.jsonl"
        append_finding("bot", "bug", "m.py", "first", "low", path=p)
        time.sleep(0.01)
        append_finding("bot", "bug", "m.py", "second", "low", path=p)
        findings = read_findings(path=p)
        assert findings[0]["finding"] == "first"
        assert findings[1]["finding"] == "second"
