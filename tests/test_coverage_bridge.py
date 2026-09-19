"""Unit tests for codebot.coverage_bridge module.

Covers severity mapping, line range grouping, MAX_LINES_PER_TICKET splitting,
protected_paths exclusion, evidence hash deduplication, max_tickets cap,
and empty report handling.
"""

from __future__ import annotations

import pytest

from codebot.coverage_bridge import (
    CoverageGap,
    _compute_evidence_hash,
    _classify_severity,
    _is_protected,
    _group_consecutive_lines,
    _split_into_chunks,
    generate_coverage_tickets,
    DEFAULT_MIN_COVERAGE_PCT,
    CRITICAL_THRESHOLD_PCT,
    HIGH_THRESHOLD_PCT,
    MEDIUM_THRESHOLD_PCT,
    MAX_LINES_PER_TICKET,
    PROTECTED_PATHS,
)


class TestCoverageGap:
    """Tests for CoverageGap dataclass."""

    def test_to_dict(self) -> None:
        gap = CoverageGap(
            module_path="codebot/foo.py",
            missing_lines=[1, 2, 3],
            coverage_pct=45.0,
            severity="high",
            evidence_hash="abc123",
        )
        d = gap.to_dict()
        assert d["module_path"] == "codebot/foo.py"
        assert d["missing_lines"] == [1, 2, 3]
        assert d["coverage_pct"] == 45.0
        assert d["severity"] == "high"
        assert d["evidence_hash"] == "abc123"

    def test_frozen(self) -> None:
        gap = CoverageGap(
            module_path="x.py",
            missing_lines=[],
            coverage_pct=100.0,
            severity="low",
            evidence_hash="hash",
        )
        with pytest.raises(AttributeError):
            gap.module_path = "y.py"  # type: ignore[misc]


class TestComputeEvidenceHash:
    """Tests for _compute_evidence_hash()."""

    def test_deterministic(self) -> None:
        h1 = _compute_evidence_hash("foo.py", [1, 2, 3])
        h2 = _compute_evidence_hash("foo.py", [1, 2, 3])
        assert h1 == h2

    def test_different_paths_different_hashes(self) -> None:
        h1 = _compute_evidence_hash("foo.py", [1, 2])
        h2 = _compute_evidence_hash("bar.py", [1, 2])
        assert h1 != h2

    def test_different_lines_different_hashes(self) -> None:
        h1 = _compute_evidence_hash("foo.py", [1, 2])
        h2 = _compute_evidence_hash("foo.py", [1, 3])
        assert h1 != h2

    def test_order_independent(self) -> None:
        """Hash should be same regardless of line order (sorted internally)."""
        h1 = _compute_evidence_hash("foo.py", [3, 1, 2])
        h2 = _compute_evidence_hash("foo.py", [1, 2, 3])
        assert h1 == h2

    def test_hash_length(self) -> None:
        h = _compute_evidence_hash("foo.py", [1])
        assert len(h) == 16  # First 16 chars of SHA-256 hex


class TestClassifySeverity:
    """Tests for _classify_severity()."""

    def test_critical(self) -> None:
        assert _classify_severity(29.9) == "critical"
        assert _classify_severity(0.0) == "critical"

    def test_high(self) -> None:
        assert _classify_severity(30.0) == "high"
        assert _classify_severity(49.9) == "high"

    def test_medium(self) -> None:
        assert _classify_severity(50.0) == "medium"
        assert _classify_severity(69.9) == "medium"

    def test_low(self) -> None:
        assert _classify_severity(70.0) == "low"
        assert _classify_severity(100.0) == "low"


class TestIsProtected:
    """Tests for _is_protected()."""

    def test_tests_dir_protected(self) -> None:
        assert _is_protected("tests/test_foo.py")
        assert _is_protected("tests/")

    def test_codebot_dir_protected(self) -> None:
        assert _is_protected(".codebot/state/file.json")

    def test_docs_dir_protected(self) -> None:
        assert _is_protected("docs/readme.md")

    def test_normal_path_not_protected(self) -> None:
        assert not _is_protected("codebot/foo.py")
        assert not _is_protected("src/bar.py")

    def test_partial_match_not_protected(self) -> None:
        """Path starting with 'test' but not 'tests/' should not be protected."""
        assert not _is_protected("testing/utils.py")


class TestGroupConsecutiveLines:
    """Tests for _group_consecutive_lines()."""

    def test_empty_list(self) -> None:
        assert _group_consecutive_lines([]) == []

    def test_single_line(self) -> None:
        assert _group_consecutive_lines([5]) == [(5, 5)]

    def test_consecutive_lines(self) -> None:
        assert _group_consecutive_lines([1, 2, 3, 4]) == [(1, 4)]

    def test_non_consecutive_lines(self) -> None:
        result = _group_consecutive_lines([1, 3, 5])
        assert result == [(1, 1), (3, 3), (5, 5)]

    def test_mixed_ranges(self) -> None:
        result = _group_consecutive_lines([1, 2, 3, 5, 6, 8])
        assert result == [(1, 3), (5, 6), (8, 8)]

    def test_unsorted_input(self) -> None:
        """Should handle unsorted input correctly."""
        result = _group_consecutive_lines([5, 1, 3, 2, 4])
        assert result == [(1, 5)]

    def test_duplicates_removed(self) -> None:
        result = _group_consecutive_lines([1, 1, 2, 2, 3])
        assert result == [(1, 3)]


class TestSplitIntoChunks:
    """Tests for _split_into_chunks()."""

    def test_empty_list(self) -> None:
        assert _split_into_chunks([]) == []

    def test_single_chunk(self) -> None:
        lines = [1, 2, 3]
        chunks = _split_into_chunks(lines, max_size=10)
        assert chunks == [[1, 2, 3]]

    def test_multiple_chunks(self) -> None:
        lines = list(range(1, 11))  # 10 lines
        chunks = _split_into_chunks(lines, max_size=3)
        assert len(chunks) == 4  # 3+3+3+1
        assert chunks[0] == [1, 2, 3]
        assert chunks[1] == [4, 5, 6]
        assert chunks[2] == [7, 8, 9]
        assert chunks[3] == [10]

    def test_exact_chunk_size(self) -> None:
        lines = [1, 2, 3]
        chunks = _split_into_chunks(lines, max_size=3)
        assert chunks == [[1, 2, 3]]

    def test_deduplication(self) -> None:
        lines = [1, 1, 2, 2, 3]
        chunks = _split_into_chunks(lines, max_size=10)
        assert chunks == [[1, 2, 3]]

    def test_sorted_output(self) -> None:
        lines = [5, 1, 3, 2, 4]
        chunks = _split_into_chunks(lines, max_size=10)
        assert chunks == [[1, 2, 3, 4, 5]]


class TestGenerateCoverageTickets:
    """Tests for generate_coverage_tickets()."""

    def test_empty_modules(self) -> None:
        tickets = generate_coverage_tickets({"modules": {}})
        assert tickets == []

    def test_no_missing_lines(self) -> None:
        coverage_data = {
            "modules": {
                "foo.py": {"missing_lines": [], "coverage_pct": 100.0},
            }
        }
        tickets = generate_coverage_tickets(coverage_data)
        assert tickets == []

    def test_protected_path_skipped(self) -> None:
        coverage_data = {
            "modules": {
                "tests/test_foo.py": {"missing_lines": [1, 2], "coverage_pct": 0.0},
            }
        }
        tickets = generate_coverage_tickets(coverage_data)
        assert tickets == []

    def test_low_coverage_generates_ticket(self) -> None:
        coverage_data = {
            "modules": {
                "codebot/foo.py": {"missing_lines": [1, 2, 3], "coverage_pct": 20.0},
            }
        }
        tickets = generate_coverage_tickets(coverage_data)
        assert len(tickets) == 1
        assert tickets[0]["severity"] == "critical"
        assert "codebot/foo.py" in tickets[0]["title"]

    def test_medium_coverage_generates_ticket(self) -> None:
        coverage_data = {
            "modules": {
                "codebot/bar.py": {"missing_lines": [10, 20], "coverage_pct": 55.0},
            }
        }
        tickets = generate_coverage_tickets(coverage_data)
        assert len(tickets) == 1
        assert tickets[0]["severity"] == "medium"

    def test_high_coverage_skipped(self) -> None:
        """Coverage >= 70% should be skipped (low severity)."""
        coverage_data = {
            "modules": {
                "codebot/good.py": {"missing_lines": [1], "coverage_pct": 75.0},
            }
        }
        tickets = generate_coverage_tickets(coverage_data)
        assert tickets == []

    def test_deduplication_with_existing_hashes(self) -> None:
        coverage_data = {
            "modules": {
                "codebot/foo.py": {"missing_lines": [1, 2], "coverage_pct": 40.0},
            }
        }
        # Compute the hash that would be generated
        from codebot.coverage_bridge import _compute_evidence_hash
        existing_hash = _compute_evidence_hash("codebot/foo.py", [1, 2])
        tickets = generate_coverage_tickets(coverage_data, existing_hashes={existing_hash})
        assert tickets == []

    def test_max_tickets_cap(self) -> None:
        """Should not generate more than max_tickets."""
        modules = {}
        for i in range(30):
            modules[f"codebot/mod{i}.py"] = {
                "missing_lines": [1],
                "coverage_pct": 20.0,  # Critical
            }
        coverage_data = {"modules": modules}
        tickets = generate_coverage_tickets(coverage_data, max_tickets=5)
        assert len(tickets) == 5

    def test_sorting_by_severity_then_path(self) -> None:
        coverage_data = {
            "modules": {
                "codebot/z.py": {"missing_lines": [1], "coverage_pct": 20.0},  # Critical
                "codebot/a.py": {"missing_lines": [1], "coverage_pct": 20.0},  # Critical
                "codebot/m.py": {"missing_lines": [1], "coverage_pct": 40.0},  # High
            }
        }
        tickets = generate_coverage_tickets(coverage_data)
        # Critical tickets first, sorted by path
        assert tickets[0]["affected_modules"] == ["codebot/a.py"]
        assert tickets[1]["affected_modules"] == ["codebot/z.py"]
        # Then high
        assert tickets[2]["affected_modules"] == ["codebot/m.py"]

    def test_ticket_structure(self) -> None:
        coverage_data = {
            "modules": {
                "codebot/foo.py": {"missing_lines": [1, 2, 3], "coverage_pct": 40.0},
            }
        }
        tickets = generate_coverage_tickets(coverage_data)
        ticket = tickets[0]
        assert "title" in ticket
        assert "ticket_class" in ticket
        assert ticket["ticket_class"] == "test"
        assert "severity" in ticket
        assert "source" in ticket
        assert ticket["source"] == "coverage_bridge"
        assert "evidence" in ticket
        assert "problem_statement" in ticket
        assert "desired_state" in ticket
        assert "acceptance_criteria" in ticket
        assert "affected_modules" in ticket
        assert "evidence_hash" in ticket

    def test_line_range_formatting(self) -> None:
        """Test that line ranges are formatted correctly in title."""
        coverage_data = {
            "modules": {
                "codebot/foo.py": {"missing_lines": [1, 2, 3, 5, 7], "coverage_pct": 40.0},
            }
        }
        tickets = generate_coverage_tickets(coverage_data)
        title = tickets[0]["title"]
        # Should contain range notation like "1-3"
        assert "1-3" in title or "1, 2, 3" in title

    def test_single_line_formatting(self) -> None:
        """Single lines should not use range notation."""
        coverage_data = {
            "modules": {
                "codebot/foo.py": {"missing_lines": [42], "coverage_pct": 40.0},
            }
        }
        tickets = generate_coverage_tickets(coverage_data)
        title = tickets[0]["title"]
        assert "42" in title

    def test_split_into_multiple_tickets_when_many_lines(self) -> None:
        """Many missing lines should be split into multiple tickets."""
        missing = list(range(1, 101))  # 100 lines
        coverage_data = {
            "modules": {
                "codebot/big.py": {"missing_lines": missing, "coverage_pct": 20.0},
            }
        }
        tickets = generate_coverage_tickets(coverage_data)
        # MAX_LINES_PER_TICKET = 50, so 100 lines -> 2 tickets
        assert len(tickets) == 2
        # Both should reference the same module
        assert all("big.py" in t["title"] for t in tickets)

    def test_acceptance_criteria_content(self) -> None:
        coverage_data = {
            "modules": {
                "codebot/foo.py": {"missing_lines": [1], "coverage_pct": 40.0},
            }
        }
        tickets = generate_coverage_tickets(coverage_data)
        criteria = tickets[0]["acceptance_criteria"]
        assert any("pytest passes" in c for c in criteria)
        assert any("coverage" in c.lower() for c in criteria)

    def test_rollback_strategy(self) -> None:
        coverage_data = {
            "modules": {
                "codebot/foo.py": {"missing_lines": [1], "coverage_pct": 40.0},
            }
        }
        tickets = generate_coverage_tickets(coverage_data)
        assert tickets[0]["rollback_strategy"] == "revert commit"

    def test_risk_level(self) -> None:
        coverage_data = {
            "modules": {
                "codebot/foo.py": {"missing_lines": [1], "coverage_pct": 40.0},
            }
        }
        tickets = generate_coverage_tickets(coverage_data)
        assert tickets[0]["risk"] == "low"

    def test_schema_version(self) -> None:
        coverage_data = {
            "modules": {
                "codebot/foo.py": {"missing_lines": [1], "coverage_pct": 40.0},
            }
        }
        tickets = generate_coverage_tickets(coverage_data)
        assert tickets[0]["schema_version"] == "2.0"
