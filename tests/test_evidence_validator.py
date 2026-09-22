"""Tests for codebot/evidence_validator.py — pre-ticket evidence verification."""

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codebot.evidence_validator import (
    EvidenceCheckResult,
    EvidenceValidationResult,
    is_generated_path,
    is_vendored_path,
    is_non_source_extension,
    validate_evidence_item,
    validate_finding_evidence,
    revalidate_before_ticket_creation,
    get_current_revision,
)


# ---------------------------------------------------------------------------
# Path classification tests
# ---------------------------------------------------------------------------

class TestPathClassification:
    def test_generated_pycache(self):
        assert is_generated_path("codebot/__pycache__/foo.pyc") is True

    def test_generated_node_modules(self):
        assert is_generated_path("frontend/node_modules/react/index.js") is True

    def test_generated_dist(self):
        assert is_generated_path("dist/bundle.js") is True

    def test_generated_build(self):
        assert is_generated_path("build/output.o") is True

    def test_generated_pyc(self):
        assert is_generated_path("codebot/foo.pyc") is True

    def test_generated_egg_info(self):
        assert is_generated_path("codebot.egg-info/PKG-INFO") is True

    def test_not_generated_source(self):
        assert is_generated_path("codebot/ticket_engine.py") is False

    def test_not_generated_test(self):
        assert is_generated_path("tests/test_foo.py") is False

    def test_vendored_vendor(self):
        assert is_vendored_path("vendor/lib/foo.py") is True

    def test_vendored_third_party(self):
        assert is_vendored_path("third_party/lib.py") is True

    def test_vendored_node_modules(self):
        assert is_vendored_path("node_modules/pkg/index.js") is True

    def test_not_vendored_source(self):
        assert is_vendored_path("codebot/api_runner.py") is False

    def test_non_source_pyc(self):
        assert is_non_source_extension("foo.pyc") is True

    def test_non_source_lock(self):
        assert is_non_source_extension("tickets.json.lock") is True

    def test_non_source_png(self):
        assert is_non_source_extension("logo.png") is True

    def test_source_py(self):
        assert is_non_source_extension("foo.py") is False

    def test_source_md(self):
        assert is_non_source_extension("README.md") is False


# ---------------------------------------------------------------------------
# EvidenceCheckResult tests
# ---------------------------------------------------------------------------

class TestEvidenceCheckResult:
    def test_default_is_valid(self):
        r = EvidenceCheckResult()
        assert r.is_valid is True
        assert r.rejection_reasons == ()

    def test_file_missing_invalid(self):
        r = EvidenceCheckResult(file_exists=False)
        assert r.is_valid is False
        assert "file_does_not_exist" in r.rejection_reasons

    def test_generated_invalid(self):
        r = EvidenceCheckResult(file_is_generated=True)
        assert r.is_valid is False
        assert "generated_file" in r.rejection_reasons

    def test_vendored_invalid(self):
        r = EvidenceCheckResult(file_is_vendored=True)
        assert r.is_valid is False
        assert "vendored_code" in r.rejection_reasons

    def test_symbol_missing_invalid(self):
        r = EvidenceCheckResult(symbol_exists=False)
        assert r.is_valid is False
        assert "symbol_not_found" in r.rejection_reasons

    def test_multiple_rejections(self):
        r = EvidenceCheckResult(file_exists=False, symbol_exists=False)
        assert len(r.rejection_reasons) == 2


# ---------------------------------------------------------------------------
# EvidenceValidationResult tests
# ---------------------------------------------------------------------------

class TestEvidenceValidationResult:
    def test_all_valid(self):
        r = EvidenceValidationResult(
            checks=(EvidenceCheckResult(), EvidenceCheckResult())
        )
        assert r.all_valid is True
        assert r.has_rejections is False

    def test_has_rejections(self):
        r = EvidenceValidationResult(
            checks=(EvidenceCheckResult(), EvidenceCheckResult(file_exists=False))
        )
        assert r.all_valid is False
        assert r.has_rejections is True

    def test_rejection_summary(self):
        r = EvidenceValidationResult(
            checks=(
                EvidenceCheckResult(file_exists=False),
                EvidenceCheckResult(file_is_generated=True),
            )
        )
        summary = r.rejection_summary
        assert "file_does_not_exist" in summary
        assert "generated_file" in summary

    def test_to_dict(self):
        r = EvidenceValidationResult(
            checks=(EvidenceCheckResult(),),
            hallucination_detected=True,
        )
        d = r.to_dict()
        assert d["hallucination_detected"] is True
        assert d["checks_count"] == 1


# ---------------------------------------------------------------------------
# validate_evidence_item tests (with real filesystem)
# ---------------------------------------------------------------------------

class TestValidateEvidenceItem:
    @pytest.fixture
    def project_dir(self, tmp_path):
        src = tmp_path / "codebot"
        src.mkdir()
        (src / "ticket_engine.py").write_text(
            "class TicketStore:\n    def add(self, ticket):\n        pass\n\n"
            "def create_ticket(title):\n    pass\n",
            encoding="utf-8",
        )
        (src / "web_tools.py").write_text(
            "def web_fetch(url):\n    resp = get(url)\n    data = resp.read()\n    return data\n",
            encoding="utf-8",
        )
        return tmp_path

    def test_file_exists(self, project_dir):
        r = validate_evidence_item(project_dir, "codebot/ticket_engine.py")
        assert r.file_exists is True
        assert r.is_valid is True

    def test_file_not_exists(self, project_dir):
        r = validate_evidence_item(project_dir, "codebot/nonexistent.py")
        assert r.file_exists is False
        assert r.is_valid is False

    def test_symbol_exists(self, project_dir):
        r = validate_evidence_item(
            project_dir, "codebot/ticket_engine.py", symbol="TicketStore"
        )
        assert r.symbol_exists is True

    def test_symbol_not_exists(self, project_dir):
        r = validate_evidence_item(
            project_dir, "codebot/ticket_engine.py", symbol="NonexistentClass"
        )
        assert r.symbol_exists is False

    def test_line_exists(self, project_dir):
        r = validate_evidence_item(
            project_dir, "codebot/ticket_engine.py", line_number=2
        )
        assert r.line_exists is True

    def test_line_beyond_file(self, project_dir):
        r = validate_evidence_item(
            project_dir, "codebot/ticket_engine.py", line_number=99999
        )
        assert r.line_exists is False

    def test_content_match(self, project_dir):
        r = validate_evidence_item(
            project_dir, "codebot/ticket_engine.py",
            excerpt="class TicketStore"
        )
        assert r.content_matches is True

    def test_content_mismatch(self, project_dir):
        r = validate_evidence_item(
            project_dir, "codebot/ticket_engine.py",
            excerpt="def completely_absent_function_name_xyzzy"
        )
        assert r.content_matches is False

    def test_generated_path_rejected(self, project_dir):
        r = validate_evidence_item(project_dir, "codebot/__pycache__/foo.pyc")
        assert r.file_is_generated is True
        assert r.is_valid is False

    def test_vendored_path_rejected(self, project_dir):
        vendor = project_dir / "vendor"
        vendor.mkdir()
        (vendor / "lib.py").write_text("x = 1", encoding="utf-8")
        r = validate_evidence_item(project_dir, "vendor/lib.py")
        assert r.file_is_vendored is True
        assert r.is_valid is False

    def test_non_source_extension_rejected(self, project_dir):
        lock = project_dir / "test.lock"
        lock.write_text("lock", encoding="utf-8")
        r = validate_evidence_item(project_dir, "test.lock")
        assert r.file_is_non_source is True
        assert r.is_valid is False

    def test_path_traversal_rejected(self, project_dir):
        r = validate_evidence_item(project_dir, "../../etc/passwd")
        assert r.file_exists is False

    def test_absolute_path_rejected(self, project_dir):
        r = validate_evidence_item(project_dir, "/etc/passwd")
        assert r.file_exists is False

    def test_no_symbol_check_when_empty(self, project_dir):
        r = validate_evidence_item(
            project_dir, "codebot/ticket_engine.py", symbol=""
        )
        assert r.symbol_exists is True

    def test_no_line_check_when_zero(self, project_dir):
        r = validate_evidence_item(
            project_dir, "codebot/ticket_engine.py", line_number=0
        )
        assert r.line_exists is True


# ---------------------------------------------------------------------------
# validate_finding_evidence tests
# ---------------------------------------------------------------------------

class TestValidateFindingEvidence:
    @pytest.fixture
    def project_dir(self, tmp_path):
        src = tmp_path / "codebot"
        src.mkdir()
        (src / "foo.py").write_text("def bar():\n    pass\n", encoding="utf-8")
        return tmp_path

    def test_all_valid(self, project_dir):
        items = [
            {"file_path": "codebot/foo.py", "symbol": "bar", "line_number": 1},
        ]
        result = validate_finding_evidence(project_dir, items)
        assert result.all_valid is True
        assert result.hallucination_detected is False

    def test_hallucination_detected(self, project_dir):
        items = [
            {"file_path": "codebot/nonexistent.py"},
        ]
        result = validate_finding_evidence(project_dir, items)
        assert result.hallucination_detected is True

    def test_generated_reference_flagged(self, project_dir):
        items = [
            {"file_path": "codebot/__pycache__/foo.pyc"},
        ]
        result = validate_finding_evidence(project_dir, items)
        assert result.generated_file_reference is True

    def test_vendor_reference_flagged(self, project_dir):
        vendor = project_dir / "vendor"
        vendor.mkdir()
        (vendor / "lib.py").write_text("x = 1")
        items = [{"file_path": "vendor/lib.py"}]
        result = validate_finding_evidence(project_dir, items)
        assert result.vendor_reference is True

    def test_empty_file_path_is_observation_only(self, project_dir):
        items = [{"file_path": "", "observation": "some observation"}]
        result = validate_finding_evidence(project_dir, items)
        assert result.all_valid is True

    def test_multiple_items_mixed(self, project_dir):
        items = [
            {"file_path": "codebot/foo.py", "symbol": "bar"},
            {"file_path": "codebot/missing.py"},
        ]
        result = validate_finding_evidence(project_dir, items)
        assert result.hallucination_detected is True
        assert result.has_rejections is True


# ---------------------------------------------------------------------------
# revalidate_before_ticket_creation tests
# ---------------------------------------------------------------------------

class TestRevalidateBeforeTicketCreation:
    @pytest.fixture
    def project_dir(self, tmp_path):
        src = tmp_path / "codebot"
        src.mkdir()
        (src / "foo.py").write_text("def bar():\n    pass\n", encoding="utf-8")
        return tmp_path

    def test_valid_evidence_allows_creation(self, project_dir):
        items = [{"file_path": "codebot/foo.py", "symbol": "bar"}]
        ok, reason = revalidate_before_ticket_creation(project_dir, items)
        assert ok is True

    def test_no_evidence_rejects(self, project_dir):
        ok, reason = revalidate_before_ticket_creation(project_dir, [])
        assert ok is False
        assert "no evidence" in reason

    def test_hallucinated_rejects(self, project_dir):
        items = [{"file_path": "codebot/imaginary.py"}]
        ok, reason = revalidate_before_ticket_creation(project_dir, items)
        assert ok is False
        assert "hallucinated" in reason

    def test_generated_rejects(self, project_dir):
        items = [{"file_path": "codebot/__pycache__/foo.pyc"}]
        ok, reason = revalidate_before_ticket_creation(project_dir, items)
        assert ok is False
        assert "generated" in reason

    def test_vendored_rejects(self, project_dir):
        vendor = project_dir / "vendor"
        vendor.mkdir()
        (vendor / "lib.py").write_text("x = 1")
        items = [{"file_path": "vendor/lib.py"}]
        ok, reason = revalidate_before_ticket_creation(project_dir, items)
        assert ok is False
        assert "vendored" in reason


# ---------------------------------------------------------------------------
# get_current_revision tests
# ---------------------------------------------------------------------------

class TestGetCurrentRevision:
    def test_returns_string_for_git_repo(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=str(tmp_path), capture_output=True)
        rev = get_current_revision(tmp_path)
        assert isinstance(rev, str)
        assert len(rev) == 40 or rev == ""

    def test_returns_empty_for_non_repo(self, tmp_path):
        rev = get_current_revision(tmp_path)
        assert rev == "" or isinstance(rev, str)
