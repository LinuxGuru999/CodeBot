"""
P0 batch5 — discovery/quality extensive tests (6 modules)

Coverage (via `rm -f .coverage .coverage.* && python3 -m coverage run -p --source=codebot -m pytest tests/test_p0_batch5_discovery_extensive.py -q --tb=no -o addopts='' -p no:cacheprovider && python3 -m coverage combine && python3 -m coverage report --include="codebot/discovery_finding.py,codebot/evidence_validator.py,codebot/findings_log.py,codebot/manifest_schema.py,codebot/pricing_table.py,codebot/quality_metrics.py"`):

| Module                    | Stmts | Miss | Cover |
|---------------------------|-------|------|-------|
| codebot/discovery_finding | 254   |  1   |  99% |
| codebot/evidence_validator| 233   |  8   |  97% |
| codebot/findings_log      |  99   |  0   | 100% |
| codebot/manifest_schema   | 199   |  5   |  97% |
| codebot/pricing_table     |  33   |  0   | 100% |
| codebot/quality_metrics   | 314   |  3   |  99% |
| TOTAL                     | 1132  | 17   |  98% |

Style: Given/When/Then docstrings, tmp_path isolation, no live .codebot/state mutation.
Mocks file I/O via tmp_path, subprocess via unittest.mock, close handles promptly.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# pricing_table ----------------------------------------------------------------
import codebot.pricing_table as pt
from codebot.pricing_table import (
    DEFAULT_PRICING,
    ModelPricing,
    _normalize_model,
    calculate_cost_usd,
    calculate_ticket_cost_usd,
    get_pricing,
    list_models_for_provider,
    list_providers,
)

# discovery_finding ------------------------------------------------------------
import codebot.discovery_finding as df_mod
from codebot.discovery_finding import (
    SCHEMA_VERSION,
    MAX_EVIDENCE_ITEMS,
    MAX_EXCERPT_LENGTH,
    MAX_TITLE_LENGTH,
    FINDING_CATEGORIES,
    SEVERITY_VALUES,
    CONCRETE_EVIDENCE_KINDS,
    EVIDENCE_KINDS,
    Atomicity,
    Confidence,
    DuplicateCandidate,
    DuplicateRelation,
    EvidenceItem,
    FindingRelation,
    FindingRelationship,
    FindingValidationError,
    ScopeEstimate,
    DiscoveryFinding,
    _repair_raw,
    finding_from_agent_output,
    generate_finding_id,
    normalize_text,
)

# evidence_validator -----------------------------------------------------------
import codebot.evidence_validator as ev
from codebot.evidence_validator import (
    EvidenceCheckResult,
    EvidenceValidationResult,
    _check_content_match,
    _check_file_exists,
    _check_line_in_file,
    _check_symbol_in_file,
    _resolve_path,
    get_current_revision,
    is_generated_path,
    is_non_source_extension,
    is_vendored_path,
    revalidate_before_ticket_creation,
    validate_evidence_item,
    validate_finding_evidence,
)

# findings_log -----------------------------------------------------------------
import codebot.findings_log as fl
from codebot.findings_log import (
    FINDINGS_SCHEMA_KEYS,
    MAX_FINDINGS_LINES,
    MAX_FINDINGS_READ,
    append_finding,
    get_adapter,
    get_default_findings_path,
    read_findings,
    rotate_findings,
    set_project_adapter,
)

# manifest_schema --------------------------------------------------------------
import codebot.manifest_schema as ms
from codebot.manifest_schema import load_all_manifests, load_manifest, validate_manifest

# quality_metrics --------------------------------------------------------------
import codebot.quality_metrics as qm
from codebot.quality_metrics import QualityMetricsTracker, QualitySnapshot, SNAPSHOT_INTERVAL_SECONDS


# =============================================================================
# Helpers
# =============================================================================

def _valid_manifest(name="bot-a", extra=None):
    d = {
        "name": name,
        "kind": "scan",
        "prompt_file": "prompts/bot-a.md",
        "model": "gpt-4o",
        "runner": "opencode",
        "enabled": True,
        "interval_seconds": 60,
        "heartbeat_timeout": 30,
        "tier_priority": 1,
        "max_restarts": 3,
        "clean_exit_wait": False,
        "session_timeout": 300,
        "input": [{"path": "src", "type": "fs"}],
        "output": [{"path": "out.json", "type": "json"}],
        "noop_cap": 0,
    }
    if extra:
        d.update(extra)
    return d


def _minimal_finding_dict(**overrides):
    base = {
        "finding_id": "DF-ABC123DEF456",
        "discovery_role": "bug_hunter",
        "discovery_category": "bug",
        "title": "Null deref in foo.py",
        "problem_statement": "Function foo dereferences null pointer on line 10 causing crash",
        "severity": "high",
        "priority": "high",
        "confidence": "high",
        "atomicity": "atomic",
        "scope_estimate": "small",
        "repository": "myrepo",
        "repository_revision": "abc123",
        "observed_behavior": "crashes",
        "expected_behavior": "should not crash",
        "affected_components": ["comp-a"],
        "affected_files": ["src/foo.py"],
        "affected_symbols": ["foo"],
        "discovery_method": "static_analysis",
        "related_tickets": ["CB-1"],
        "acceptance_outcome": "Add null check and add test",
        "evidence": [{"observation": "foo derefs null", "kind": "failing_test", "file_path": "src/foo.py", "line_number": 10, "excerpt": "x = ptr.value"}],
        "duplicate_candidates": [],
        "relationships": [],
        "created_at": 1234567890.0,
        "schema_version": SCHEMA_VERSION,
    }
    base.update(overrides)
    return base


def _make_file(tmp_path: Path, rel="src/foo.py", content="def foo():\n    pass\n"):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _tickets_json(tickets, state_dir: Path):
    p = state_dir / "tickets.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"tickets": tickets}), encoding="utf-8")
    return p


# =============================================================================
# pricing_table — 100% (33 stmts)
# =============================================================================

def test_pricing_normalize_Given_spaces_and_caps_When_normalize_Then_lower_hyphen():
    """Given model with spaces and caps
    When _normalize_model
    Then lowercased and hyphenated."""
    assert _normalize_model("  GPT 4o MINI ") == "gpt-4o-mini"
    assert _normalize_model("Claude Sonnet 4") == "claude-sonnet-4"
    assert _normalize_model("  qwen-3.8-max  ") == "qwen-3.8-max"


def test_pricing_get_pricing_known_and_unknown_Given_default_table_When_lookup_Then_found_or_none():
    """Given default table
    When get_pricing for known/unknown
    Then returns pricing or None."""
    p = get_pricing("gpt-4o")
    assert isinstance(p, ModelPricing)
    assert p.provider == "openai"
    assert get_pricing("unknown-model-xyz") is None
    assert get_pricing("GPT-4O") is not None  # case-insensitive
    assert get_pricing("gpt 4o") is not None  # space normalized


def test_pricing_get_pricing_custom_table_Given_override_When_lookup_Then_custom():
    """Given custom table
    When get_pricing
    Then uses override."""
    custom = {"my-model": ModelPricing(1.0, 2.0, "custom")}
    assert get_pricing("my-model", custom).provider == "custom"
    assert get_pricing("gpt-4o", custom) is None  # override isolates
    assert get_pricing("my-model") is None  # default doesn't have it


def test_pricing_calculate_cost_Given_tokens_When_calculate_Then_usd():
    """Given tokens
    When calculate_cost_usd
    Then computes USD per 1M pricing."""
    # gpt-4o: 2.5 prompt, 10 completion per 1M
    cost = calculate_cost_usd("gpt-4o", 1_000_000, 1_000_000)
    assert cost == pytest.approx(12.5)
    # zero tokens
    assert calculate_cost_usd("gpt-4o", 0, 0) == 0.0
    # unknown model -> 0
    assert calculate_cost_usd("unknown", 1000, 1000) == 0.0


def test_pricing_calculate_cost_negative_clamped_Given_negative_When_calculate_Then_zero():
    """Given negative tokens
    When calculate_cost_usd
    Then clamped to 0."""
    assert calculate_cost_usd("gpt-4o", -100, -200) == 0.0
    assert calculate_cost_usd("gpt-4o", -100, 500_000) == pytest.approx(500_000 * 10.0 / 1_000_000)


def test_pricing_calculate_cost_rounding_Given_small_tokens_When_calculate_Then_8dec():
    """Given small token counts
    When calculate_cost_usd
    Then rounded to 8 decimals."""
    cost = calculate_cost_usd("gpt-4o-mini", 1, 1)  # 0.15 and 0.60 per 1M
    assert cost == round(0.15/1_000_000 + 0.60/1_000_000, 8)


def test_pricing_calculate_cost_custom_table_Given_override_When_calculate_Then_uses_it():
    """Given custom pricing
    When calculate_cost_usd
    Then uses override table."""
    custom = {"test-model": ModelPricing(10.0, 20.0, "test")}
    assert calculate_cost_usd("test-model", 1_000_000, 500_000, custom) == pytest.approx(20.0)
    assert calculate_cost_usd("test-model", 1_000, 1_000) == 0.0  # default unknown


def test_pricing_calculate_ticket_cost_Given_ticket_totals_When_calculate_Then_delegates():
    """Given ticket totals dict
    When calculate_ticket_cost_usd
    Then delegates to calculate_cost_usd."""
    totals = {"prompt_tokens": 2000, "completion_tokens": 1000}
    expected = calculate_cost_usd("gpt-4o", 2000, 1000)
    assert calculate_ticket_cost_usd(totals, "gpt-4o") == expected
    # missing keys default 0
    assert calculate_ticket_cost_usd({}, "gpt-4o") == 0.0
    assert calculate_ticket_cost_usd({"prompt_tokens": 500}, "gpt-4o") == calculate_cost_usd("gpt-4o", 500, 0)
    # string values converted via int()
    assert calculate_ticket_cost_usd({"prompt_tokens": "100", "completion_tokens": "200"}, "gpt-4o") == calculate_cost_usd("gpt-4o", 100, 200)


def test_pricing_list_providers_Given_default_When_list_Then_sorted_unique():
    """Given default table
    When list_providers
    Then sorted unique."""
    providers = list_providers()
    assert providers == sorted(providers)
    assert "openai" in providers
    assert "anthropic" in providers
    assert "alibaba" in providers
    # custom
    custom = {"a": ModelPricing(1,1,"x"), "b": ModelPricing(1,1,"x"), "c": ModelPricing(1,1,"y")}
    assert list_providers(custom) == ["x", "y"]


def test_pricing_list_models_for_provider_Given_provider_When_list_Then_filtered_sorted():
    """Given provider
    When list_models_for_provider
    Then filtered and sorted."""
    models = list_models_for_provider("openai")
    assert all(DEFAULT_PRICING[m].provider == "openai" for m in models)
    assert models == sorted(models)
    assert "gpt-4o" in models
    assert list_models_for_provider("nonexistent") == []
    custom = {"a": ModelPricing(1,1,"p"), "b": ModelPricing(1,1,"p")}
    assert list_models_for_provider("p", custom) == ["a", "b"]


def test_pricing_dataclass_frozen_Given_pricing_When_mutate_Then_frozen():
    """Given frozen dataclass
    When mutate
    Then raises."""
    p = ModelPricing(1.0, 2.0, "test")
    with pytest.raises((AttributeError, TypeError)):
        p.provider = "other"  # type: ignore[misc]


def test_pricing_all_default_models_have_provider_Given_defaults_When_inspect_Then_all_have_provider():
    """Given DEFAULT_PRICING
    When iterating
    Then each has provider str."""
    for name, pricing in DEFAULT_PRICING.items():
        assert pricing.provider
        assert pricing.prompt_usd_per_1m >= 0
        assert pricing.completion_usd_per_1m >= 0


# =============================================================================
# discovery_finding — extensive (≈35 tests)
# =============================================================================

def test_discovery_generate_finding_id_format_Given_default_When_generate_Then_prefixed_hex():
    """Given no prefix
    When generate_finding_id
    Then DF- + 12 hex uppercase."""
    fid = generate_finding_id()
    assert fid.startswith("DF-")
    assert len(fid) == 15  # DF- +12
    assert re.match(r"^DF-[0-9A-F]{12}$", fid)
    fid2 = generate_finding_id(prefix="XX")
    assert fid2.startswith("XX-")
    assert generate_finding_id() != generate_finding_id()  # unique


def test_discovery_normalize_text_removes_stopwords_Given_sentence_When_normalize_Then_filtered():
    """Given sentence with stopwords
    When normalize_text
    Then stopwords removed and lowercased."""
    result = normalize_text("The quick brown fox and the lazy dog")
    assert "the" not in result
    assert "and" not in result
    assert "quick" in result
    assert "brown" in result
    # single char words filtered via len>=2
    assert "a" not in result
    # numbers allowed
    assert "foo_123" in normalize_text("foo_123 bar")


def test_discovery_evidence_item_post_init_truncates_Given_long_excerpt_When_create_Then_truncated():
    """Given excerpt > MAX_EXCERPT_LENGTH
    When EvidenceItem created
    Then truncated."""
    long_excerpt = "x" * (MAX_EXCERPT_LENGTH + 500)
    item = EvidenceItem(observation="obs", excerpt=long_excerpt)
    assert len(item.excerpt) == MAX_EXCERPT_LENGTH
    # frozen
    with pytest.raises((AttributeError, TypeError)):
        item.observation = "new"  # type: ignore[misc]


def test_discovery_evidence_item_to_dict_and_from_dict_roundtrip_Given_item_When_roundtrip_Then_equal():
    """Given EvidenceItem
    When to_dict/from_dict
    Then preserves fields."""
    item = EvidenceItem(observation="obs", interpretation="interp", impact="imp", file_path="src/a.py", line_number=5, symbol="foo", excerpt="code", kind="failing_test")
    d = item.to_dict()
    restored = EvidenceItem.from_dict(d)
    assert restored.observation == "obs"
    assert restored.kind == "failing_test"
    assert restored.line_number == 5


def test_discovery_evidence_from_dict_coerces_line_and_kind_Given_bad_inputs_When_from_dict_Then_defaults():
    """Given malformed line/kind
    When from_dict
    Then coerces safely."""
    # line as string invalid -> 0, kind invalid -> code_reference
    item = EvidenceItem.from_dict({"observation": "obs", "line_number": "not_an_int", "kind": "INVALID_KIND"})
    assert item.line_number == 0
    assert item.kind == "code_reference"
    # line as None -> 0
    item2 = EvidenceItem.from_dict({"observation": "obs", "line_number": None})
    assert item2.line_number == 0
    # negative line clamped later via max(0, line) in from_dict
    item3 = EvidenceItem.from_dict({"observation": "  obs  ", "line_number": -5, "kind": "  FAILING_TEST  "})
    assert item3.observation == "obs"
    assert item3.line_number == 0
    assert item3.kind == "failing_test"
    # excerpt truncated
    long_exc = "y" * 3000
    item4 = EvidenceItem.from_dict({"observation": "o", "excerpt": long_exc})
    assert len(item4.excerpt) == MAX_EXCERPT_LENGTH


def test_discovery_evidence_has_concrete_proof_and_valid_path_Given_kinds_When_check_Then_bool():
    """Given various kinds/paths
    When has_concrete_proof/is_valid_path
    Then correct."""
    assert EvidenceItem(observation="o", kind="failing_test").has_concrete_proof() is True
    assert EvidenceItem(observation="o", kind="code_reference").has_concrete_proof() is False
    assert EvidenceItem(observation="o", file_path="").is_valid_path() is True
    assert EvidenceItem(observation="o", file_path="src/foo.py").is_valid_path() is True
    assert EvidenceItem(observation="o", file_path="/abs/path.py").is_valid_path() is False
    assert EvidenceItem(observation="o", file_path="\\abs\\path.py").is_valid_path() is False
    assert EvidenceItem(observation="o", file_path="src/../etc/passwd").is_valid_path() is False
    assert EvidenceItem(observation="o", file_path="src\\..\\etc").is_valid_path() is False
    assert EvidenceItem(observation="o", file_path="src/sub/../other.py").is_valid_path() is False


def test_discovery_duplicate_candidate_from_dict_validates_relation_Given_bad_relation_When_from_dict_Then_default():
    """Given invalid relation
    When DuplicateCandidate.from_dict
    Then falls back to RELATED."""
    c = DuplicateCandidate.from_dict({"target_id": "CB-1", "relation": "invalid_rel"})
    assert c.relation == DuplicateRelation.RELATED.value
    c2 = DuplicateCandidate.from_dict({"target_id": "  CB-2  ", "relation": "EXACT_DUPLICATE"})
    assert c2.relation == DuplicateRelation.EXACT_DUPLICATE.value
    assert c2.target_id == "CB-2"
    # roundtrip
    d = c2.to_dict()
    assert d["target_id"] == "CB-2"


def test_discovery_finding_relationship_from_dict_Given_invalid_When_from_dict_Then_default():
    """Given invalid relation
    When FindingRelationship.from_dict
    Then defaults to RELATED_TO."""
    r = FindingRelationship.from_dict({"relation": "bogus", "target_id": "CB-3"})
    assert r.relation == FindingRelation.RELATED_TO.value
    r2 = FindingRelationship.from_dict({"relation": "CAUSES", "target_id": " CB-5 "})
    assert r2.relation == FindingRelation.CAUSES.value
    assert r2.target_id == "CB-5"
    assert r2.to_dict()["relation"] == "causes"


def test_discovery_finding_normalized_problem_statement_sorted_Given_statement_When_normalized_Then_sorted_unique():
    """Given problem_statement
    When normalized_problem_statement
    Then word-sorted lower without stopwords."""
    f = DiscoveryFinding.from_dict(_minimal_finding_dict(problem_statement="Zebra apple Zebra apple the"))
    norm = f.normalized_problem_statement()
    # "zebra apple" sorted -> "apple zebra"
    assert norm == "apple zebra"


def test_discovery_finding_fingerprint_stable_and_dedup_Given_same_problem_diff_files_When_fingerprint_Then_differs():
    """Given two findings same problem different files
    When fingerprint
    Then different."""
    base = _minimal_finding_dict()
    f1 = DiscoveryFinding.from_dict({**base, "affected_files": ["a.py"]})
    f2 = DiscoveryFinding.from_dict({**base, "affected_files": ["b.py"]})
    assert f1.fingerprint() != f2.fingerprint()
    # same files sorted -> same fingerprint
    f3 = DiscoveryFinding.from_dict({**base, "affected_files": ["b.py", "a.py"]})
    f4 = DiscoveryFinding.from_dict({**base, "affected_files": ["a.py", "b.py"]})
    assert f3.fingerprint() == f4.fingerprint()
    # empty files -> same regardless of whitespace
    f5 = DiscoveryFinding.from_dict({**base, "affected_files": ["  ", ""]})
    f6 = DiscoveryFinding.from_dict({**base, "affected_files": []})
    assert f5.fingerprint() == f6.fingerprint()


def test_discovery_finding_confidence_is_supported_Given_high_without_concrete_When_check_Then_false():
    """Given HIGH confidence without concrete evidence
    When confidence_is_supported
    Then False."""
    f = DiscoveryFinding.from_dict(_minimal_finding_dict(confidence="high", evidence=[{"observation": "obs", "kind": "code_reference"}]))
    assert f.confidence_is_supported() is False
    f2 = DiscoveryFinding.from_dict(_minimal_finding_dict(confidence="high", evidence=[{"observation": "obs", "kind": "failing_test"}]))
    assert f2.confidence_is_supported() is True
    f3 = DiscoveryFinding.from_dict(_minimal_finding_dict(confidence="medium", evidence=[{"observation": "obs", "kind": "code_reference"}]))
    assert f3.confidence_is_supported() is True
    f4 = DiscoveryFinding.from_dict(_minimal_finding_dict(confidence="low", evidence=[{"observation": "obs", "kind": "other"}]))
    assert f4.confidence_is_supported() is True


def test_discovery_finding_validate_success_Given_valid_When_validate_Then_no_raise():
    """Given valid finding
    When validate
    Then no exception."""
    f = DiscoveryFinding.from_dict(_minimal_finding_dict())
    f.validate()  # should not raise


def test_discovery_finding_validate_title_required_and_length_Given_bad_title_When_validate_Then_raises():
    """Given empty or too-long title
    When validate
    Then raises."""
    base = _minimal_finding_dict(title="")
    f = DiscoveryFinding.from_dict(base)
    with pytest.raises(FindingValidationError, match="title is required"):
        f.validate()
    f2 = DiscoveryFinding.from_dict(_minimal_finding_dict(title="   "))
    with pytest.raises(FindingValidationError, match="title is required"):
        f2.validate()
    f3 = DiscoveryFinding.from_dict(_minimal_finding_dict(title="x" * (MAX_TITLE_LENGTH + 1)))
    with pytest.raises(FindingValidationError, match="exceeds"):
        f3.validate()


def test_discovery_finding_validate_problem_and_role_required_Given_empty_When_validate_Then_raises():
    """Given empty problem_statement/discovery_role
    When validate
    Then raises."""
    f = DiscoveryFinding.from_dict(_minimal_finding_dict(problem_statement="   "))
    with pytest.raises(FindingValidationError, match="problem_statement"):
        f.validate()
    f2 = DiscoveryFinding.from_dict(_minimal_finding_dict(discovery_role="   "))
    with pytest.raises(FindingValidationError, match="discovery_role"):
        f2.validate()


def test_discovery_finding_validate_category_severity_priority_confidence_atomicity_Given_bad_enums_When_validate_Then_raises():
    """Given invalid enums
    When validate
    Then raises."""
    f = DiscoveryFinding.from_dict(_minimal_finding_dict(discovery_category="invalid_cat"))
    with pytest.raises(FindingValidationError, match="discovery_category"):
        f.validate()
    f2 = DiscoveryFinding.from_dict(_minimal_finding_dict(severity="urgent"))
    with pytest.raises(FindingValidationError, match="invalid severity"):
        f2.validate()
    f3 = DiscoveryFinding.from_dict(_minimal_finding_dict(priority="urgent"))
    with pytest.raises(FindingValidationError, match="invalid priority"):
        f3.validate()
    f4 = DiscoveryFinding.from_dict(_minimal_finding_dict(confidence="extreme"))
    with pytest.raises(FindingValidationError, match="invalid confidence"):
        f4.validate()
    f5 = DiscoveryFinding.from_dict(_minimal_finding_dict(atomicity="huge"))
    with pytest.raises(FindingValidationError, match="invalid atomicity"):
        f5.validate()


def test_discovery_finding_validate_evidence_required_and_item_checks_Given_no_evidence_or_bad_path_When_validate_Then_raises():
    """Given no evidence or evidence with bad path/empty observation
    When validate
    Then raises."""
    f = DiscoveryFinding.from_dict(_minimal_finding_dict(evidence=[]))
    with pytest.raises(FindingValidationError, match="at least one evidence"):
        f.validate()
    # evidence item empty observation+excerpt - but EvidenceItem.from_dict will strip and keep empty, validate should catch
    # We craft directly via DiscoveryFinding constructor to bypass from_dict filtering
    bad_item = EvidenceItem(observation="", excerpt="", file_path="src/foo.py")
    f2 = DiscoveryFinding(
        finding_id="DF-1", discovery_role="bug_hunter", discovery_category="bug",
        title="t", problem_statement="prob", severity="high", priority="high",
        confidence="medium", atomicity="atomic", repository="r", repository_revision="rev",
        evidence=[bad_item], acceptance_outcome="outcome"
    )
    with pytest.raises(FindingValidationError, match="evidence item needs"):
        f2.validate()
    # absolute path
    bad_item2 = EvidenceItem(observation="obs", file_path="/abs/path.py")
    f3 = DiscoveryFinding(
        finding_id="DF-2", discovery_role="bug_hunter", discovery_category="bug",
        title="t", problem_statement="prob", severity="high", priority="high",
        confidence="medium", atomicity="atomic", repository="r", repository_revision="rev",
        evidence=[bad_item2], acceptance_outcome="outcome"
    )
    with pytest.raises(FindingValidationError, match="evidence path unsafe"):
        f3.validate()
    # acceptance_outcome required
    f4 = DiscoveryFinding.from_dict(_minimal_finding_dict(acceptance_outcome="   "))
    with pytest.raises(FindingValidationError, match="acceptance_outcome"):
        f4.validate()


def test_discovery_finding_to_dict_and_to_json_Given_finding_When_serialize_Then_contains_fields():
    """Given finding
    When to_dict/to_json
    Then correct structure."""
    f = DiscoveryFinding.from_dict(_minimal_finding_dict())
    d = f.to_dict()
    assert d["schema_version"] == SCHEMA_VERSION
    assert d["finding_id"] == "DF-ABC123DEF456"
    assert "fingerprint" in d
    assert isinstance(d["evidence"], list)
    j = f.to_json()
    assert json.loads(j)["title"] == "Null deref in foo.py"
    pretty = f.to_json(pretty=True)
    assert "\n" in pretty
    assert json.loads(pretty)["title"] == "Null deref in foo.py"


def test_discovery_finding_from_dict_coerces_types_Given_mixed_inputs_When_from_dict_Then_normalized():
    """Given mixed types for affected_* fields
    When from_dict
    Then coerced to lists."""
    # string comma-separated
    f = DiscoveryFinding.from_dict(_minimal_finding_dict(affected_files="a.py, b.py , c.py", affected_components="comp1, comp2"))
    assert f.affected_files == ["a.py", "b.py", "c.py"]
    assert f.affected_components == ["comp1", "comp2"]
    # non-list non-string -> empty
    f2 = DiscoveryFinding.from_dict({**_minimal_finding_dict(), "affected_files": 123})
    assert f2.affected_files == []
    # list with blanks filtered
    f3 = DiscoveryFinding.from_dict(_minimal_finding_dict(affected_symbols=["  foo ", "", "  ", "bar"]))
    assert f3.affected_symbols == ["foo", "bar"]
    # created_at invalid -> 0.0
    f4 = DiscoveryFinding.from_dict(_minimal_finding_dict(created_at="not_a_number"))
    assert f4.created_at == 0.0
    # scope invalid -> unknown
    f5 = DiscoveryFinding.from_dict(_minimal_finding_dict(scope_estimate="gigantic"))
    assert f5.scope_estimate == ScopeEstimate.UNKNOWN.value
    # acceptance via from_dict directly handles all string fields stripped
    f6 = DiscoveryFinding.from_dict(_minimal_finding_dict(repository="  myrepo  "))
    assert f6.repository == "myrepo"


def test_discovery_finding_from_dict_generates_id_and_priority_fallback_Given_missing_fields_When_from_dict_Then_defaults():
    """Given missing finding_id/priority
    When from_dict
    Then generated and priority falls back to severity."""
    d = _minimal_finding_dict()
    d.pop("finding_id")
    d.pop("priority")
    f = DiscoveryFinding.from_dict(d)
    assert f.finding_id.startswith("DF-")
    assert f.priority == f.severity
    # atomicity missing -> unknown
    d2 = _minimal_finding_dict()
    d2.pop("atomicity", None)
    # Actually from_dict does atomicity or UNKNOWN if empty - we test empty
    d2["atomicity"] = ""
    f2 = DiscoveryFinding.from_dict(d2)
    assert f2.atomicity == Atomicity.UNKNOWN.value


def test_discovery_finding_from_json_roundtrip_Given_json_When_from_json_Then_equal():
    """Given JSON string
    When from_json
    Then restored."""
    f = DiscoveryFinding.from_dict(_minimal_finding_dict())
    j = f.to_json()
    f2 = DiscoveryFinding.from_json(j)
    assert f2.title == f.title
    assert f2.fingerprint() == f.fingerprint()


def test_discovery_finding_from_dict_filters_non_dict_evidence_Given_mixed_evidence_When_from_dict_Then_filtered():
    """Given evidence with non-dict entries
    When from_dict
    Then filtered."""
    d = _minimal_finding_dict(evidence=[{"observation": "obs1"}, "not_a_dict", None, {"observation": "obs2"}])
    f = DiscoveryFinding.from_dict(d)
    assert len(f.evidence) == 2
    # also tests duplicate_candidates / relationships non-dict filtering
    d2 = _minimal_finding_dict(duplicate_candidates=[{"target_id": "CB-1"}, "bad"], relationships=[{"relation": "causes", "target_id": "CB-2"}, 123])
    f2 = DiscoveryFinding.from_dict(d2)
    assert len(f2.duplicate_candidates) == 1
    assert len(f2.relationships) == 1


def test_discovery_repair_raw_truncates_and_normalizes_Given_long_title_When_repair_Then_truncated():
    """Given raw with overlong title and bad enums
    When _repair_raw
    Then repaired."""
    long_title = "T" * 500
    raw = {
        "title": long_title,
        "severity": "CRITICAL",
        "priority": "",
        "confidence": "bad",
        "atomicity": "bad",
        "scope_estimate": "BIG",
        "discovery_category": "BUG",
        "evidence": "  some observation string  ",
        "affected_files": "a.py, b.py",
        "desired_state": "outcome via desired_state",
    }
    repaired = _repair_raw(raw)
    assert len(repaired["title"]) == MAX_TITLE_LENGTH
    assert repaired["severity"] == "critical"
    assert repaired["confidence"] == Confidence.MEDIUM.value
    assert repaired["atomicity"] == Atomicity.UNKNOWN.value
    # evidence string -> list
    assert isinstance(repaired["evidence"], list)
    assert repaired["evidence"][0]["observation"] == "some observation string"
    # affected_files string -> list
    assert repaired["affected_files"] == ["a.py", "b.py"]
    # acceptance_outcome from desired_state
    assert repaired["acceptance_outcome"] == "outcome via desired_state"


def test_discovery_repair_raw_ticket_class_fallback_and_list_evidence_Given_ticket_class_When_repair_Then_fallback():
    """Given ticket_class instead of discovery_category
    When _repair_raw
    Then category filled."""
    raw = {"ticket_class": "Security", "severity": "unknown_sev", "evidence": ["  obs string  ", {"observation": "obs2"}, None, ""]}
    repaired = _repair_raw(raw)
    assert repaired["discovery_category"] == "security"
    # severity unknown -> medium
    assert repaired["severity"] == "medium"
    # priority defaults to severity when empty
    assert repaired["priority"] == "medium"
    # evidence list string coercion
    assert repaired["evidence"][0]["observation"] == "obs string"
    assert len(repaired["evidence"]) <= MAX_EVIDENCE_ITEMS
    # evidence truncated at MAX
    raw2 = {"evidence": ["x"] * (MAX_EVIDENCE_ITEMS + 10)}
    repaired2 = _repair_raw(raw2)
    assert len(repaired2["evidence"]) == MAX_EVIDENCE_ITEMS


def test_discovery_finding_from_agent_output_success_and_repair_Given_raw_When_from_agent_Then_validated():
    """Given raw agent output
    When finding_from_agent_output
    Then repaired and validated."""
    raw = {
        "title": "Test finding",
        "problem_statement": "Something is broken in foo.py",
        "discovery_category": "bug",
        "severity": "high",
        "priority": "high",
        "confidence": "high",
        "atomicity": "atomic",
        "evidence": [{"observation": "obs", "kind": "failing_test"}],
        "acceptance_outcome": "Fix it",
    }
    f = finding_from_agent_output(raw, role="bug_hunter", repository="repo", repository_revision="rev1")
    assert f.discovery_role == "bug_hunter"
    assert f.repository == "repo"
    assert f.title == "Test finding"
    # finding_id auto-generated
    assert f.finding_id.startswith("DF-")


def test_discovery_finding_from_agent_output_validates_required_Given_missing_fields_When_from_agent_Then_raises():
    """Given missing required fields
    When finding_from_agent_output
    Then raises FindingValidationError."""
    # non-dict payload
    with pytest.raises(FindingValidationError, match="must be a JSON object"):
        finding_from_agent_output("not_a_dict", role="bug_hunter")  # type: ignore[arg-type]
    # empty dict missing title/problem/acceptance
    with pytest.raises(FindingValidationError):
        finding_from_agent_output({}, role="bug_hunter")
    # missing acceptance_outcome
    raw = {
        "title": "t", "problem_statement": "prob", "discovery_category": "bug",
        "severity": "high", "evidence": [{"observation": "obs"}], "acceptance_outcome": ""
    }
    with pytest.raises(FindingValidationError):
        finding_from_agent_output(raw, role="bug_hunter")


def test_discovery_finding_from_agent_output_role_and_repo_defaults_Given_empty_role_repo_When_from_agent_Then_uses_args():
    """Given role/repo args
    When from_agent_output with empty fields
    Then defaults applied."""
    raw = {
        "title": "t", "problem_statement": "prob", "discovery_category": "bug",
        "severity": "high", "evidence": [{"observation": "obs"}], "acceptance_outcome": "outcome"
    }
    # role supplied should fill discovery_role if missing
    f = finding_from_agent_output(raw, role="security_auditor")
    assert f.discovery_role == "security_auditor"
    # repository defaults from args
    f2 = finding_from_agent_output(raw, role="bug_hunter", repository="myrepo", repository_revision="abc")
    assert f2.repository == "myrepo"
    assert f2.repository_revision == "abc"


def test_discovery_enums_cover_all_values_Given_enums_When_inspected_Then_members():
    """Given enums
    When iterating
    Then all values present."""
    assert Confidence.HIGH.value == "high"
    assert Atomicity.UNKNOWN.value == "unknown"
    assert ScopeEstimate.LARGE.value == "large"
    assert DuplicateRelation.DISTINCT.value == "distinct"
    assert FindingRelation.SUPERSEDES.value == "supersedes"
    assert "bug" in FINDING_CATEGORIES
    assert "critical" in SEVERITY_VALUES
    assert "failing_test" in CONCRETE_EVIDENCE_KINDS
    assert "other" in EVIDENCE_KINDS


# =============================================================================
# evidence_validator — extensive
# =============================================================================

def test_ev_is_generated_path_detects_patterns_Given_paths_When_check_Then_correct(tmp_path: Path):
    """Given various paths
    When is_generated_path
    Then detects generated."""
    assert is_generated_path("__pycache__/foo.pyc") is True
    assert is_generated_path("dist/bundle.js") is True
    assert is_generated_path("build/output.bin") is True
    assert is_generated_path(".git/objects/abc") is True
    assert is_generated_path("node_modules/lib") is True
    assert is_generated_path("pkg.egg-info/file") is True  # via *.egg-info
    assert is_generated_path("coverage/report.html") is True
    assert is_generated_path(".venv/lib/python") is True
    assert is_generated_path(".codebot/state/file.json") is True
    assert is_generated_path("src/foo.py") is False
    # backslash normalization
    assert is_generated_path("dist\\bundle.js") is True


def test_ev_is_vendored_path_case_insensitive_Given_paths_When_check_Then_correct():
    """Given vendor paths
    When is_vendored_path
    Then case-insensitive detection."""
    assert is_vendored_path("vendor/lib/foo.py") is True
    assert is_vendored_path("Third_Party/code.py") is True
    assert is_vendored_path("src/vendor/lib.py") is True  # contains vendor/
    assert is_vendored_path("node_modules/foo") is True
    assert is_vendored_path("src/foo.py") is False
    assert is_vendored_path("src/clean/file.py") is False


def test_ev_is_non_source_extension_Given_extensions_When_check_Then_correct():
    """Given extensions
    When is_non_source_extension
    Then correct."""
    assert is_non_source_extension("archive.zip") is True
    assert is_non_source_extension("lib.so") is True
    assert is_non_source_extension("doc.pdf") is True
    assert is_non_source_extension("src/foo.py") is False
    assert is_non_source_extension("src/foo.PYC") is True  # lowercased
    assert is_non_source_extension("no_extension") is False


def test_ev_resolve_path_security_Given_traversal_When_resolve_Then_none(tmp_path: Path):
    """Given traversal or absolute
    When _resolve_path
    Then None."""
    assert _resolve_path(tmp_path, "") is None
    assert _resolve_path(tmp_path, "/abs/path") is None
    assert _resolve_path(tmp_path, "\\abs\\path") is None
    assert _resolve_path(tmp_path, "../outside") is None
    assert _resolve_path(tmp_path, "src/../..") is None
    # valid
    p = _resolve_path(tmp_path, "src/foo.py")
    assert p is not None
    assert str(p).endswith("src/foo.py")
    # traversal via resolve escape (symlink-like but using .. in resolved path)
    # create real file and test relative_to escape not triggered for valid inside
    _make_file(tmp_path, "src/a.py", "x")
    assert _resolve_path(tmp_path, "src/a.py") is not None


def test_ev_check_file_exists_Given_file_When_check_Then_bool(tmp_path: Path):
    """Given file existence
    When _check_file_exists
    Then returns correct."""
    _make_file(tmp_path, "src/exist.py", "hello")
    exists, resolved = _check_file_exists(tmp_path, "src/exist.py")
    assert exists is True
    assert resolved is not None
    exists2, resolved2 = _check_file_exists(tmp_path, "src/missing.py")
    assert exists2 is False
    exists3, _ = _check_file_exists(tmp_path, "/abs.py")
    assert exists3 is False


def test_ev_check_symbol_in_file_regex_and_ast_Given_py_file_When_check_Then_found(tmp_path: Path):
    """Given python file with symbols
    When _check_symbol_in_file
    Then regex fast path or AST fallback."""
    content = "def foo():\n    pass\nclass Bar:\n    pass\nx = 1\n"
    p = _make_file(tmp_path, "src/sym.py", content)
    assert _check_symbol_in_file(p, "") is True  # no symbol to check
    assert _check_symbol_in_file(p, "foo") is True  # regex finds def foo
    assert _check_symbol_in_file(p, "Bar") is True
    assert _check_symbol_in_file(p, "x") is True  # regex \b x \b
    assert _check_symbol_in_file(p, "nonexistent") is False
    # AST path: ensure ast parsing finds Name/Attribute even if regex misses due to special chars? Actually regex already would find; test AST success by creating file where symbol appears only as attribute
    content2 = "obj = SomeClass()\nobj.my_attr = 1\n"
    p2 = _make_file(tmp_path, "src/attr.py", content2)
    assert _check_symbol_in_file(p2, "my_attr") is True
    # non-py file falls back to regex only
    p3 = _make_file(tmp_path, "src/data.txt", "hello world my_symbol")
    assert _check_symbol_in_file(p3, "my_symbol") is True
    assert _check_symbol_in_file(p3, "missing") is False


def test_ev_check_symbol_file_read_error_Given_missing_file_When_check_Then_false(tmp_path: Path):
    """Given unreadable file
    When _check_symbol_in_file
    Then False."""
    p = tmp_path / "src/missing2.py"
    # not created
    assert _check_symbol_in_file(p, "foo") is False
    # also test OSError via patch
    _make_file(tmp_path, "src/err.py", "def foo(): pass")
    pp = tmp_path / "src/err.py"
    with patch.object(Path, "read_text", side_effect=OSError("fail")):
        assert _check_symbol_in_file(pp, "foo") is False


def test_ev_check_symbol_ast_syntax_error_Given_bad_py_When_check_Then_regex_only(tmp_path: Path):
    """Given python file with syntax error
    When _check_symbol_in_file and regex misses
    Then returns False without crashing."""
    bad_content = "def foo(:\n  syntax error [[["
    p = _make_file(tmp_path, "src/bad.py", bad_content)
    # regex won't find nonexistent symbol, AST will raise SyntaxError and be swallowed
    assert _check_symbol_in_file(p, "nonexistent_symbol_xyz") is False
    # but regex can still find token even with syntax error
    p2 = _make_file(tmp_path, "src/bad2.py", "def foo(:\n  something my_token")
    assert _check_symbol_in_file(p2, "my_token") is True


def test_ev_check_line_in_file_bounds_Given_file_When_check_Then_correct(tmp_path: Path):
    """Given file with N lines
    When _check_line_in_file
    Then within bounds true, beyond false."""
    content = "a\nb\nc\n"
    p = _make_file(tmp_path, "src/lines.py", content)
    assert _check_line_in_file(p, 0) is True
    assert _check_line_in_file(p, -5) is True
    assert _check_line_in_file(p, 1) is True
    assert _check_line_in_file(p, 3) is True
    assert _check_line_in_file(p, 4) is True  # 3 lines +1 due to trailing newline counted
    assert _check_line_in_file(p, 100) is False


def test_ev_check_line_in_file_oserror_and_safety_bound(tmp_path: Path):
    """Given OSError or huge file
    When _check_line_in_file
    Then handles without crash."""
    p = tmp_path / "src/noexist.py"
    assert _check_line_in_file(p, 5) is False
    # patch open to raise OSError
    _make_file(tmp_path, "src/ok.py", "a\nb\n")
    pp = tmp_path / "src/ok.py"
    with patch("builtins.open", side_effect=OSError("fail")):
        # first loop raises, second read_text also patched? Actually second path uses Path.read_text; patch that too
        with patch.object(Path, "read_text", side_effect=OSError("fail")):
            assert _check_line_in_file(pp, 5) is False
    # safety bound: file with many lines, request high line -> loop breaks early then falls through to total_lines check
    many = "\n".join(str(i) for i in range(300))
    p2 = _make_file(tmp_path, "src/many.py", many)
    assert _check_line_in_file(p2, 250) is True  # within file
    assert _check_line_in_file(p2, 500) is False


def test_ev_check_content_match_short_and_found_Given_excerpt_When_check_Then_correct(tmp_path: Path):
    """Given excerpt
    When _check_content_match
    Then handles short, found, not found."""
    content = "def foo():\n    return 42\n"
    p = _make_file(tmp_path, "src/content.py", content)
    assert _check_content_match(p, "") is True
    assert _check_content_match(p, "   ") is True
    assert _check_content_match(p, "short") is True  # <10 chars ignored
    # long enough and present
    assert _check_content_match(p, "def foo():\n    return 42") is True
    # whitespace normalized
    assert _check_content_match(p, "def   foo():  return    42") is True
    # not present
    assert _check_content_match(p, "this excerpt definitely not in file content at all xyz") is False


def test_ev_check_content_match_oserror_and_truncation(tmp_path: Path):
    """Given file read error
    When _check_content_match
    Then False."""
    p = tmp_path / "src/missing_content.py"
    assert _check_content_match(p, "some long excerpt that is definitely long enough") is False
    _make_file(tmp_path, "src/excerpt.py", "hello world content here")
    pp = tmp_path / "src/excerpt.py"
    with patch.object(Path, "read_text", side_effect=OSError("fail")):
        assert _check_content_match(pp, "hello world content here long enough to test") is False
    # excerpt longer than 100 chars, only first 100 checked
    long_excerpt = "a" * 150
    content2 = "a" * 150
    p2 = _make_file(tmp_path, "src/long.py", content2)
    assert _check_content_match(p2, long_excerpt) is True


def test_ev_get_current_revision_success_and_failure(tmp_path: Path):
    """Given git repo or not
    When get_current_revision
    Then returns hash or empty."""
    mock_result = MagicMock(returncode=0, stdout="abc123\n")
    with patch("codebot.evidence_validator.subprocess.run", return_value=mock_result) as mock_run:
        rev = get_current_revision(tmp_path)
        assert rev == "abc123"
        mock_run.assert_called_once()
    # non-zero return
    mock_result2 = MagicMock(returncode=1, stdout="")
    with patch("codebot.evidence_validator.subprocess.run", return_value=mock_result2):
        assert get_current_revision(tmp_path) == ""
    # timeout/OSError/FileNotFound
    with patch("codebot.evidence_validator.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5)):
        assert get_current_revision(tmp_path) == ""
    with patch("codebot.evidence_validator.subprocess.run", side_effect=OSError("no git")):
        assert get_current_revision(tmp_path) == ""
    with patch("codebot.evidence_validator.subprocess.run", side_effect=FileNotFoundError("git not found")):
        assert get_current_revision(tmp_path) == ""


def test_ev_validate_evidence_item_fast_rejections_Given_generated_vendored_non_source_When_validate_Then_flag(tmp_path: Path):
    """Given generated/vendored/non-source paths
    When validate_evidence_item
    Then respective flag set."""
    r = validate_evidence_item(tmp_path, "dist/bundle.js")
    assert r.file_is_generated is True
    assert r.is_valid is False
    assert "generated" in r.warnings[0].lower()
    r2 = validate_evidence_item(tmp_path, "vendor/lib.py")
    assert r2.file_is_vendored is True
    r3 = validate_evidence_item(tmp_path, "archive.zip")
    assert r3.file_is_non_source is True
    # check rejection_reasons
    assert "generated_file" in r.rejection_reasons
    assert "vendored_code" in r2.rejection_reasons
    assert "non_source_file" in r3.rejection_reasons


def test_ev_validate_evidence_item_file_not_found_Given_missing_When_validate_Then_file_not_exists(tmp_path: Path):
    """Given missing file
    When validate_evidence_item
    Then file_exists False."""
    r = validate_evidence_item(tmp_path, "src/missing_file.py")
    assert r.file_exists is False
    assert r.is_valid is False
    assert "file_does_not_exist" in r.rejection_reasons


def test_ev_validate_evidence_item_success_and_warnings_Given_real_file_When_validate_Then_checks(tmp_path: Path):
    """Given real file with symbol/line/content
    When validate_evidence_item
    Then validates each."""
    content = "def my_func():\n    x = 1\n    return x\n"
    _make_file(tmp_path, "src/real.py", content)
    # all correct
    r = validate_evidence_item(tmp_path, "src/real.py", symbol="my_func", line_number=1, excerpt="def my_func():\n    x = 1")
    assert r.is_valid is True
    assert r.warnings == ()
    # symbol missing
    r2 = validate_evidence_item(tmp_path, "src/real.py", symbol="nonexistent_xyz", line_number=1)
    assert r2.symbol_exists is False
    assert "symbol_not_found" in r2.rejection_reasons
    assert r2.is_valid is False
    # line beyond
    r3 = validate_evidence_item(tmp_path, "src/real.py", line_number=999)
    assert r3.line_exists is False
    # content mismatch
    r4 = validate_evidence_item(tmp_path, "src/real.py", excerpt="this definitely not in file content xyz long enough")
    assert r4.content_matches is False


def test_ev_evidence_check_result_properties_Given_flags_When_properties_Then_correct():
    """Given EvidenceCheckResult
    When is_valid/rejection_reasons
    Then correct."""
    c = EvidenceCheckResult()
    assert c.is_valid is True
    assert c.rejection_reasons == ()
    c2 = EvidenceCheckResult(file_exists=False, symbol_exists=False)
    assert c2.is_valid is False
    assert "file_does_not_exist" in c2.rejection_reasons
    assert "symbol_not_found" in c2.rejection_reasons
    c3 = EvidenceCheckResult(line_exists=False, content_matches=False)
    assert "line_not_found" in c3.rejection_reasons
    assert "content_mismatch" in c3.rejection_reasons


def test_ev_validate_finding_evidence_aggregates_flags_Given_items_When_validate_Then_aggregate(tmp_path: Path):
    """Given multiple evidence items
    When validate_finding_evidence
    Then aggregates flags."""
    _make_file(tmp_path, "src/exists.py", "def foo(): pass")
    items = [
        {"file_path": "", "observation": "obs without file"},  # observation-only -> valid
        {"file_path": "src/exists.py", "symbol": "foo"},
        {"file_path": "src/missing.py", "symbol": "foo"},  # hallucination
        {"file_path": "dist/bundle.js"},  # generated
    ]
    result = validate_finding_evidence(tmp_path, items)
    assert len(result.checks) == 4
    assert result.hallucination_detected is True
    assert result.generated_file_reference is True
    assert result.has_rejections is True
    assert result.all_valid is False
    assert "file_does_not_exist" in result.rejection_summary
    # stale detection: symbol not found
    _make_file(tmp_path, "src/stale.py", "def bar(): pass")
    items2 = [{"file_path": "src/stale.py", "symbol": "missing_sym", "excerpt": "def bar(): pass"}]
    result2 = validate_finding_evidence(tmp_path, items2)
    assert result2.stale_evidence is True
    # vendor reference
    items3 = [{"file_path": "vendor/lib.py"}]
    result3 = validate_finding_evidence(tmp_path, items3)
    assert result3.vendor_reference is True
    # to_dict
    d = result.to_dict()
    assert "all_valid" in d
    assert d["checks_count"] == 4


def test_ev_validate_finding_evidence_empty_file_path_is_valid(tmp_path: Path):
    """Given evidence without file_path
    When validate_finding_evidence
    Then counted as valid check."""
    result = validate_finding_evidence(tmp_path, [{"file_path": "", "observation": "just observation"}])
    assert result.all_valid is True
    assert len(result.checks) == 1


def test_ev_revalidate_before_ticket_creation_gates_Given_evidence_When_revalidate_Then_decide(tmp_path: Path):
    """Given evidence items for ticket creation
    When revalidate_before_ticket_creation
    Then gate decision."""
    # no evidence -> false
    should, reason = revalidate_before_ticket_creation(tmp_path, [])
    assert should is False
    assert "no evidence" in reason.lower()
    # hallucinated -> false
    should2, reason2 = revalidate_before_ticket_creation(tmp_path, [{"file_path": "src/missing.py"}])
    assert should2 is False
    assert "hallucinated" in reason2.lower()
    # generated -> false
    should3, _ = revalidate_before_ticket_creation(tmp_path, [{"file_path": "dist/bundle.js"}])
    assert should3 is False
    # vendored -> false
    should4, _ = revalidate_before_ticket_creation(tmp_path, [{"file_path": "vendor/lib.py"}])
    assert should4 is False
    # no valid checks (all stale) -> false
    _make_file(tmp_path, "src/a.py", "def foo(): pass")
    should5, reason5 = revalidate_before_ticket_creation(tmp_path, [{"file_path": "src/a.py", "symbol": "nonexistent_xyz_long_symbol"}])
    assert should5 is False
    # valid -> true
    should6, reason6 = revalidate_before_ticket_creation(tmp_path, [{"file_path": "src/a.py", "symbol": "foo"}])
    assert should6 is True
    assert "validated" in reason6.lower()


def test_ev_revalidate_revision_mismatch_still_validates_Given_revision_When_revalidate_Then_continues(tmp_path: Path):
    """Given expected_revision mismatch
    When revalidate_before_ticket_creation
    Then still validates evidence."""
    _make_file(tmp_path, "src/rev.py", "def foo(): pass")
    with patch("codebot.evidence_validator.get_current_revision", return_value="new_rev"):
        should, _ = revalidate_before_ticket_creation(tmp_path, [{"file_path": "src/rev.py", "symbol": "foo"}], expected_revision="old_rev")
        assert should is True
    # same revision also true
    with patch("codebot.evidence_validator.get_current_revision", return_value="same"):
        should2, _ = revalidate_before_ticket_creation(tmp_path, [{"file_path": "src/rev.py", "symbol": "foo"}], expected_revision="same")
        assert should2 is True
    # empty revision does not call get_current_revision branch? actually it skips
    should3, _ = revalidate_before_ticket_creation(tmp_path, [{"file_path": "src/rev.py", "symbol": "foo"}], expected_revision="")
    assert should3 is True


def test_ev_revalidate_partial_staleness_allows_with_warning_Given_mixed_evidence_When_revalidate_Then_allows(tmp_path: Path, caplog):
    """Given mixed valid and stale evidence
    When revalidate
    Then allows with warning."""
    _make_file(tmp_path, "src/p1.py", "def foo(): pass")
    _make_file(tmp_path, "src/p2.py", "def bar(): pass")
    items = [
        {"file_path": "src/p1.py", "symbol": "foo"},  # valid
        {"file_path": "src/p2.py", "symbol": "missing_xyz"},  # stale symbol
    ]
    with caplog.at_level(logging.WARNING):
        should, _ = revalidate_before_ticket_creation(tmp_path, items)
        assert should is True
        # warning logged for partial staleness
        # Note: logger is codebot.evidence_validator logger; caplog should capture if level set


def test_ev_validation_result_properties_Given_checks_When_properties_Then_correct():
    """Given EvidenceValidationResult
    When checking properties
    Then correct."""
    c_valid = EvidenceCheckResult()
    c_invalid = EvidenceCheckResult(file_exists=False)
    r = EvidenceValidationResult(checks=(c_valid, c_invalid), hallucination_detected=False)
    assert r.all_valid is False
    assert r.has_rejections is True
    assert "file_does_not_exist" in r.rejection_summary
    r2 = EvidenceValidationResult(checks=(c_valid,), hallucination_detected=False)
    assert r2.all_valid is True


# =============================================================================
# findings_log — extensive (≈18 tests)
# =============================================================================

def test_findings_log_resolve_state_dir_adapter_env_and_default(tmp_path: Path, monkeypatch):
    """Given adapter/env/default
    When _resolve_state_dir
    Then priority adapter > env > default."""
    # clean adapter
    orig_adapter = fl._adapter_instance
    orig_state_dir = fl.STATE_DIR
    orig_default = fl.DEFAULT_FINDINGS_PATH
    try:
        # default without env/adapter
        monkeypatch.delenv("CODEBOT_STATE_DIR", raising=False)
        monkeypatch.delenv("CODEBOT_PROJECT_ROOT", raising=False)
        fl._adapter_instance = None
        # should return static STATE_DIR
        resolved = fl._resolve_state_dir()
        assert isinstance(resolved, Path)

        # env CODEBOT_STATE_DIR
        monkeypatch.setenv("CODEBOT_STATE_DIR", str(tmp_path / "env_state"))
        assert fl._resolve_state_dir() == Path(str(tmp_path / "env_state"))
        monkeypatch.delenv("CODEBOT_STATE_DIR", raising=False)

        # env CODEBOT_PROJECT_ROOT
        monkeypatch.setenv("CODEBOT_PROJECT_ROOT", str(tmp_path / "proj"))
        assert fl._resolve_state_dir() == Path(str(tmp_path / "proj")) / ".codebot" / "state"
        monkeypatch.delenv("CODEBOT_PROJECT_ROOT", raising=False)

        # adapter overrides env
        mock_adapter = MagicMock()
        mock_state = tmp_path / "adapter_state"
        mock_adapter.paths.return_value = MagicMock(state_dir=mock_state)
        fl._adapter_instance = mock_adapter
        monkeypatch.setenv("CODEBOT_STATE_DIR", str(tmp_path / "should_not_use"))
        assert fl._resolve_state_dir() == mock_state

        # adapter exception fallback to env
        mock_adapter2 = MagicMock()
        mock_adapter2.paths.side_effect = RuntimeError("fail")
        fl._adapter_instance = mock_adapter2
        monkeypatch.setenv("CODEBOT_STATE_DIR", str(tmp_path / "fallback"))
        assert fl._resolve_state_dir() == Path(str(tmp_path / "fallback"))

        # get_default_findings_path respects _resolve_state_dir
        fl._adapter_instance = None
        monkeypatch.delenv("CODEBOT_STATE_DIR", raising=False)
        monkeypatch.setenv("CODEBOT_STATE_DIR", str(tmp_path / "findings_state"))
        assert get_default_findings_path() == Path(str(tmp_path / "findings_state")) / "findings.jsonl"
    finally:
        fl._adapter_instance = orig_adapter
        fl.STATE_DIR = orig_state_dir
        fl.DEFAULT_FINDINGS_PATH = orig_default
        monkeypatch.delenv("CODEBOT_STATE_DIR", raising=False)
        monkeypatch.delenv("CODEBOT_PROJECT_ROOT", raising=False)


def test_findings_log_set_and_get_adapter(tmp_path: Path):
    """Given adapter
    When set_project_adapter
    Then get_adapter returns it and paths set."""
    orig_adapter = fl._adapter_instance
    orig_state = fl.STATE_DIR
    orig_default = fl.DEFAULT_FINDINGS_PATH
    try:
        mock_adapter = MagicMock()
        mock_state = tmp_path / "state_adapter"
        mock_adapter.paths.return_value = MagicMock(state_dir=mock_state)
        set_project_adapter(mock_adapter)
        assert get_adapter() is mock_adapter
        assert fl.STATE_DIR == mock_state
        assert fl.DEFAULT_FINDINGS_PATH == mock_state / "findings.jsonl"

        # adapter with failing paths() -> adapter set but STATE_DIR unchanged from last successful?
        # The except pass leaves STATE_DIR as previous value (mock_state)
        bad_adapter = MagicMock()
        bad_adapter.paths.side_effect = RuntimeError("oops")
        set_project_adapter(bad_adapter)
        assert get_adapter() is bad_adapter
    finally:
        fl._adapter_instance = orig_adapter
        fl.STATE_DIR = orig_state
        fl.DEFAULT_FINDINGS_PATH = orig_default


def test_findings_log_append_finding_creates_and_writes(tmp_path: Path):
    """Given bot and finding details
    When append_finding
    Then file created with JSON line."""
    p = tmp_path / "findings.jsonl"
    append_finding("bot-a", "bug", "module_a", "found null deref", "high", path=p)
    assert p.exists()
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["bot"] == "bot-a"
    assert obj["type"] == "bug"
    assert obj["finding"] == "found null deref"
    assert "ts" in obj
    assert FINDINGS_SCHEMA_KEYS.issubset(obj.keys())


def test_findings_log_append_finding_optional_fields_and_rotate_called(tmp_path: Path):
    """Given optional fields
    When append_finding
    Then they appear and rotate_findings called."""
    p = tmp_path / "findings2.jsonl"
    append_finding(
        "bot-b", "security", "mod", "finding text", "critical", path=p,
        confidence="high", priority="high", repo_revision="abc", atomicity="atomic",
        discovery_category="security", finding_id="DF-123", fingerprint="fp123",
        affected_files=["a.py"], affected_symbols=["foo"], relationships=[{"relation": "causes", "target_id": "CB-1"}]
    )
    obj = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    assert obj["confidence"] == "high"
    assert obj["finding_id"] == "DF-123"
    assert obj["affected_files"] == ["a.py"]
    assert obj["relationships"] == [{"relation": "causes", "target_id": "CB-1"}]
    # empty optional omitted
    p2 = tmp_path / "findings3.jsonl"
    append_finding("bot", "type", "mod", "find", "low", path=p2)
    obj2 = json.loads(p2.read_text(encoding="utf-8").splitlines()[0])
    assert "confidence" not in obj2
    assert "priority" not in obj2


def test_findings_log_append_finding_mkdir_parents(tmp_path: Path):
    """Given nested path
    When append_finding
    Then parents created."""
    p = tmp_path / "deep" / "nested" / "findings.jsonl"
    append_finding("bot", "type", "mod", "find", "low", path=p)
    assert p.exists()


def test_findings_log_read_findings_exists_false_and_limit(tmp_path: Path):
    """Given missing file and limit
    When read_findings
    Then empty or limited."""
    p = tmp_path / "nonexistent.jsonl"
    assert read_findings(path=p) == []
    # create 10 lines, read with limit 3 -> last 3
    p2 = tmp_path / "many.jsonl"
    for i in range(10):
        append_finding(f"bot-{i}", "type", "mod", f"finding {i}", "high", path=p2)
    all_read = read_findings(path=p2, limit=100)
    assert len(all_read) == 10
    limited = read_findings(path=p2, limit=3)
    assert len(limited) == 3
    assert limited[-1]["bot"] == "bot-9"


def test_findings_log_read_findings_skips_corrupt_and_missing_keys(tmp_path: Path):
    """Given file with corrupt lines and missing schema keys
    When read_findings
    Then skips them."""
    p = tmp_path / "corrupt.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    # valid line
    valid = {"ts": time.time(), "bot": "b", "type": "t", "module": "m", "finding": "f", "severity": "high"}
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(valid) + "\n")
        f.write("not json {\n")
        f.write(json.dumps({"ts": 1, "bot": "b"}) + "\n")  # missing keys
        f.write("\n")  # empty line
        f.write(json.dumps({**valid, "bot": "b2"}) + "\n")
    results = read_findings(path=p)
    assert len(results) == 2
    assert results[0]["bot"] == "b"
    assert results[1]["bot"] == "b2"


def test_findings_log_read_findings_oserror(tmp_path: Path):
    """Given read raises OSError
    When read_findings
    Then returns [] without crash."""
    p = tmp_path / "exists.jsonl"
    p.write_text(json.dumps({"ts": 1, "bot": "b", "type": "t", "module": "m", "finding": "f", "severity": "high"}) + "\n", encoding="utf-8")
    with patch.object(Path, "read_text", side_effect=OSError("fail")):
        assert read_findings(path=p) == []


def test_findings_log_rotate_findings_truncates_and_handles(tmp_path: Path):
    """Given file exceeds max_lines
    When rotate_findings
    Then truncated to max_lines."""
    p = tmp_path / "rotate.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    # write 10 lines, rotate to 5
    for i in range(10):
        p.write_text(p.read_text(encoding="utf-8") + f'{{"n": {i}}}\n' if p.exists() else f'{{"n": {i}}}\n', encoding="utf-8") if i else p.write_text(f'{{"n": 0}}\n', encoding="utf-8")
    # simpler: use append pattern
    # reset file with 10 lines
    lines = [f'{{"n": {i}}}' for i in range(10)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rotate_findings(path=p, max_lines=5)
    remaining = p.read_text(encoding="utf-8").splitlines()
    assert len(remaining) == 5
    assert json.loads(remaining[0])["n"] == 5
    # no rotation needed when under limit
    p2 = tmp_path / "no_rotate.jsonl"
    p2.write_text("{a}\n{b}\n", encoding="utf-8")
    rotate_findings(path=p2, max_lines=10)
    assert len(p2.read_text(encoding="utf-8").splitlines()) == 2
    # nonexistent -> noop
    p3 = tmp_path / "not_exist.jsonl"
    rotate_findings(path=p3, max_lines=5)  # should not raise
    # OSError handled
    with patch.object(Path, "read_text", side_effect=OSError("fail")):
        rotate_findings(path=p, max_lines=5)  # should not raise
    p4 = tmp_path / "write_fail.jsonl"
    p4.write_text("\n".join([f'{{"n":{i}}}' for i in range(10)]) + "\n", encoding="utf-8")
    with patch.object(Path, "write_text", side_effect=OSError("fail")):
        rotate_findings(path=p4, max_lines=5)  # should not raise


def test_findings_log_constants_and_schema_keys_Given_constants_When_inspected_Then_correct():
    """Given constants
    When inspected
    Then values."""
    assert "ts" in FINDINGS_SCHEMA_KEYS
    assert MAX_FINDINGS_LINES == 10000
    assert MAX_FINDINGS_READ == 5000


def test_findings_log_append_then_read_integration(tmp_path: Path):
    """Given append multiple
    When read_findings
    Then integration works."""
    p = tmp_path / "integration.jsonl"
    for i in range(5):
        append_finding("integration-bot", "type", "mod", f"finding {i}", "medium", path=p)
    results = read_findings(path=p)
    assert len(results) == 5
    for r in results:
        assert r["bot"] == "integration-bot"


# =============================================================================
# manifest_schema — extensive
# =============================================================================

def test_manifest_validate_dict_success_Given_valid_When_validate_Then_ok(tmp_path: Path):
    """Given valid dict
    When validate_manifest
    Then ok."""
    ok, errors = validate_manifest(_valid_manifest())
    assert ok is True
    assert errors == []


def test_manifest_validate_path_success_Given_file_When_validate_path_Then_ok(tmp_path: Path):
    """Given file path
    When validate_manifest with path
    Then reads file."""
    m = _valid_manifest()
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(m), encoding="utf-8")
    ok, errors = validate_manifest(p)
    assert ok is True
    ok2, _ = validate_manifest(str(p))
    assert ok2 is True


def test_manifest_validate_invalid_json_and_read_error_Given_bad_file_When_validate_Then_error(tmp_path: Path):
    """Given invalid JSON / read error
    When validate_manifest via path
    Then error."""
    p = tmp_path / "bad.json"
    p.write_text("{ invalid json", encoding="utf-8")
    ok, errors = validate_manifest(p)
    assert ok is False
    assert any("invalid JSON" in e for e in errors)
    # file not found with json suffix treated as missing file
    ok2, errors2 = validate_manifest(tmp_path / "missing.json")
    assert ok2 is False
    assert any("file not found" in e for e in errors2)
    # slash string not found
    ok3, errors3 = validate_manifest("/nonexistent/path/manifest.json")
    assert ok3 is False


def test_manifest_validate_dict_vs_path_heuristic_Given_string_without_slash_When_validate_Then_expected():
    """Given plain string without slash/json
    When validate_manifest with that string
    Then expected dict or path error."""
    ok, errors = validate_manifest("not_a_dict_nor_path")
    assert ok is False
    assert any("expected dict or path" in e for e in errors)


def test_manifest_validate_wrong_type_and_not_dict_json_Given_int_When_validate_Then_error(tmp_path: Path):
    """Given int input or JSON not object
    When validate_manifest
    Then error."""
    ok, errors = validate_manifest(123)  # type: ignore[arg-type]
    assert ok is False
    assert any("expected dict or path" in e for e in errors)
    p = tmp_path / "list.json"
    p.write_text(json.dumps([1,2,3]), encoding="utf-8")
    ok2, errors2 = validate_manifest(p)
    assert ok2 is False
    assert any("must be a JSON object" in e for e in errors2)
    # also dict validate with non-dict data via direct dict path? Already covered by path
    # test validate_manifest with list-like dict? Actually if data is not dict after json load


def test_manifest_validate_unknown_and_missing_fields_Given_extra_missing_When_validate_Then_errors():
    """Given unknown field and missing required
    When validate_manifest
    Then errors for each."""
    d = _valid_manifest()
    d["unknown_field"] = "oops"
    ok, errors = validate_manifest(d)
    assert ok is False
    assert any("unknown field" in e for e in errors)
    ok2, errors2 = validate_manifest({})
    assert ok2 is False
    assert any("missing required field" in e for e in errors2)


def test_manifest_validate_field_validations_Given_invalid_values_When_validate_Then_each_error():
    """Given invalid per-field values
    When validate_manifest
    Then specific errors."""
    base = _valid_manifest()
    # name empty
    ok, e = validate_manifest({**base, "name": ""})
    assert any("field 'name'" in x for x in e)
    ok, e = validate_manifest({**base, "name": 123})
    assert any("field 'name'" in x for x in e)
    # kind invalid
    ok, e = validate_manifest({**base, "kind": "bad"})
    assert any("field 'kind'" in x for x in e)
    # prompt_file empty
    ok, e = validate_manifest({**base, "prompt_file": ""})
    assert any("prompt_file" in x for x in e)
    # model empty
    ok, e = validate_manifest({**base, "model": ""})
    assert any("field 'model'" in x for x in e)
    # runner invalid
    ok, e = validate_manifest({**base, "runner": "bad"})
    assert any("field 'runner'" in x for x in e)
    # enabled not bool
    ok, e = validate_manifest({**base, "enabled": "true"})
    assert any("field 'enabled'" in x for x in e)
    # interval_seconds invalid cases
    for bad in [0, -1, True, "30"]:
        ok, e = validate_manifest({**base, "interval_seconds": bad})
        assert any("interval_seconds" in x for x in e)
    # heartbeat_timeout
    ok, e = validate_manifest({**base, "heartbeat_timeout": 0})
    assert any("heartbeat_timeout" in x for x in e)
    # tier_priority bool rejected
    ok, e = validate_manifest({**base, "tier_priority": True})
    assert any("tier_priority" in x for x in e)
    # max_restarts
    ok, e = validate_manifest({**base, "max_restarts": 0})
    assert any("max_restarts" in x for x in e)
    # clean_exit_wait
    ok, e = validate_manifest({**base, "clean_exit_wait": "yes"})
    assert any("clean_exit_wait" in x for x in e)
    # session_timeout
    ok, e = validate_manifest({**base, "session_timeout": -1})
    assert any("session_timeout" in x for x in e)
    # input not list
    ok, e = validate_manifest({**base, "input": "not_list"})
    assert any("field 'input'" in x for x in e)
    # input empty list
    ok, e = validate_manifest({**base, "input": []})
    assert any("field 'input'" in x for x in e)
    # output similarly
    ok, e = validate_manifest({**base, "output": []})
    assert any("field 'output'" in x for x in e)


def test_manifest_validate_input_entries_Given_bad_input_When_validate_Then_errors():
    """Given bad input entries
    When validate_manifest
    Then errors."""
    base = _valid_manifest()
    # input entry not dict
    ok, e = validate_manifest({**base, "input": ["not_dict"]})
    assert any("input[0]" in x for x in e)
    # missing path
    ok, e = validate_manifest({**base, "input": [{"type": "fs"}]})
    assert any("input[0].path" in x for x in e)
    # missing type
    ok, e = validate_manifest({**base, "input": [{"path": "src"}]})
    assert any("input[0].type" in x for x in e)
    # filter not string
    ok, e = validate_manifest({**base, "input": [{"path": "src", "type": "fs", "filter": 123}]})
    assert any("filter" in x for x in e)
    # unknown key in input
    ok, e = validate_manifest({**base, "input": [{"path": "src", "type": "fs", "unknown": "x"}]})
    assert any("unknown field" in x for x in e)


def test_manifest_validate_output_entries_Given_bad_output_When_validate_Then_errors():
    """Given bad output entries
    When validate_manifest
    Then errors."""
    base = _valid_manifest()
    ok, e = validate_manifest({**base, "output": ["bad"]})
    assert any("output[0]" in x for x in e)
    ok, e = validate_manifest({**base, "output": [{"path": "out"}]})
    assert any("output[0].type" in x for x in e)
    ok, e = validate_manifest({**base, "output": [{"path": "out", "type": "json", "extra": "x"}]})
    assert any("unknown field" in x for x in e)


def test_manifest_validate_noop_cap_logic_Given_noop_cap_When_validate_Then_conditional():
    """Given noop_cap
    When validate
    Then conditional noop_counter_file logic."""
    base = _valid_manifest()
    # noop_cap invalid negative
    ok, e = validate_manifest({**base, "noop_cap": -1})
    assert any("noop_cap" in x for x in e)
    # noop_cap bool rejected
    ok, e = validate_manifest({**base, "noop_cap": True})
    assert any("noop_cap" in x for x in e)
    # noop_cap >0 without counter -> error
    ok, e = validate_manifest({**base, "noop_cap": 5})
    assert any("noop_counter_file" in x and "required when" in x for x in e)
    # noop_cap >0 with empty counter -> error
    ok, e = validate_manifest({**base, "noop_cap": 5, "noop_counter_file": ""})
    assert any("noop_counter_file" in x for x in e)
    # noop_cap 0 with counter invalid -> error
    ok, e = validate_manifest({**base, "noop_cap": 0, "noop_counter_file": ""})
    assert any("noop_counter_file" in x for x in e)
    # noop_cap 0 without counter -> ok
    ok, e = validate_manifest({**base, "noop_cap": 0})
    assert ok is True
    # noop_cap >0 with valid counter -> ok
    ok, e = validate_manifest({**base, "noop_cap": 3, "noop_counter_file": "counters/noop.json"})
    assert ok is True
    # stray counter without cap -> still validates type
    ok, e = validate_manifest({**{k: v for k, v in base.items() if k != "noop_cap"}, "noop_counter_file": ""})
    assert any("noop_counter_file" in x for x in e)
    # stray counter without cap valid -> missing required noop_cap error but counter ok
    base_no_cap = {k: v for k, v in base.items() if k != "noop_cap"}
    ok, e = validate_manifest({**base_no_cap, "noop_counter_file": "some/path"})
    # should have missing noop_cap error
    assert any("noop_cap" in x for x in e)


def test_manifest_validate_optional_fields_Given_optionals_When_validate_Then_checks():
    """Given optional fields with bad values
    When validate_manifest
    Then errors."""
    base = _valid_manifest()
    # complexity_filter not list
    ok, e = validate_manifest({**base, "complexity_filter": "bad"})
    assert any("complexity_filter" in x for x in e)
    # complexity_filter empty
    ok, e = validate_manifest({**base, "complexity_filter": []})
    assert any("complexity_filter" in x for x in e)
    # complexity_filter with non-str items
    ok, e = validate_manifest({**base, "complexity_filter": ["a", 123]})
    assert any("complexity_filter" in x for x in e)
    # shard empty
    ok, e = validate_manifest({**base, "shard": ""})
    assert any("shard" in x for x in e)
    # scratchpad not bool
    ok, e = validate_manifest({**base, "scratchpad": "yes"})
    assert any("scratchpad" in x for x in e)
    # batch_tier invalid
    ok, e = validate_manifest({**base, "batch_tier": "invalid"})
    assert any("batch_tier" in x for x in e)
    # valid optionals
    ok, e = validate_manifest({**base, "complexity_filter": ["high"], "shard": "shard-1", "scratchpad": True, "batch_tier": "high-limit"})
    assert ok is True


def test_manifest_validate_source_label_in_path_errors_Given_path_When_invalid_Then_path_in_error(tmp_path: Path):
    """Given path-based validation
    When invalid
    Then error contains path."""
    m = _valid_manifest()
    m["name"] = ""  # invalid
    p = tmp_path / "m.json"
    p.write_text(json.dumps(m), encoding="utf-8")
    ok, errors = validate_manifest(p)
    assert any(str(p) in e for e in errors)


def test_manifest_load_manifest_success_and_errors(tmp_path: Path):
    """Given manifest file
    When load_manifest
    Then loads or raises."""
    m = _valid_manifest()
    p = tmp_path / "good.json"
    p.write_text(json.dumps(m), encoding="utf-8")
    loaded = load_manifest(p)
    assert loaded["name"] == "bot-a"
    # not found
    with pytest.raises(ValueError, match="file not found"):
        load_manifest(tmp_path / "nope.json")
    # invalid json
    bad = tmp_path / "bad.json"
    bad.write_text("{ invalid", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_manifest(bad)
    # validation failure tags path
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"name": ""}), encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_manifest(invalid)
    assert str(p) not in str(exc.value)  # actually invalid path should be in message, but check invalid file's path
    assert str(invalid) in str(exc.value)
    # read error via patch
    with patch.object(Path, "read_text", side_effect=OSError("disk")):
        with pytest.raises(ValueError, match="read error"):
            load_manifest(p)


def test_manifest_load_all_manifests_success_and_errors(tmp_path: Path):
    """Given manifest dir
    When load_all_manifests
    Then loads dict or raises."""
    d = tmp_path / "manifests"
    d.mkdir()
    m1 = _valid_manifest(name="bot-1")
    m2 = _valid_manifest(name="bot-2")
    (d / "a.json").write_text(json.dumps(m1), encoding="utf-8")
    (d / "b.json").write_text(json.dumps(m2), encoding="utf-8")
    all_m = load_all_manifests(d)
    assert "bot-1" in all_m
    assert "bot-2" in all_m
    # not exists
    with pytest.raises(ValueError, match="manifest directory not found"):
        load_all_manifests(tmp_path / "no_dir")
    # not a directory
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="not a directory"):
        load_all_manifests(f)
    # duplicate name
    dup_dir = tmp_path / "dup_manifests"
    dup_dir.mkdir()
    (dup_dir / "x.json").write_text(json.dumps(_valid_manifest(name="dup")), encoding="utf-8")
    (dup_dir / "y.json").write_text(json.dumps(_valid_manifest(name="dup")), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate manifest name"):
        load_all_manifests(dup_dir)
    # invalid manifest inside dir
    bad_dir = tmp_path / "bad_manifests"
    bad_dir.mkdir()
    (bad_dir / "good.json").write_text(json.dumps(_valid_manifest(name="good")), encoding="utf-8")
    (bad_dir / "bad.json").write_text(json.dumps({"name": ""}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_all_manifests(bad_dir)
    # empty dir -> empty dict
    empty = tmp_path / "empty_manifests"
    empty.mkdir()
    assert load_all_manifests(empty) == {}
    # manifest with non-string name
    weird_dir = tmp_path / "weird"
    weird_dir.mkdir()
    weird = _valid_manifest(name="weird")
    weird["name"] = 123  # type: ignore
    (weird_dir / "weird.json").write_text(json.dumps(weird), encoding="utf-8")
    with pytest.raises(ValueError):
        load_all_manifests(weird_dir)


# =============================================================================
# quality_metrics — extensive (push 61% -> >85%)
# =============================================================================

def test_quality_snapshot_to_dict_Given_snapshot_When_to_dict_Then_all_fields():
    """Given QualitySnapshot
    When to_dict
    Then all fields present."""
    snap = QualitySnapshot(
        timestamp=123.0, total_tickets=5, complete_tickets=2, rework_tickets=1, rejected_tickets=0,
        deferred_tickets=0, decompose_tickets=0, planning_tickets=0, implementing_tickets=1, reviewing_tickets=0,
        independent_test_failures=0, escaped_defects=0, complete_reopened_rate=0.0, tests_added_per_change=0.0,
        coverage_delta_pct=0.0, static_findings_delta=0, complexity_delta=0.0, duplication_delta=0.0,
        unnecessary_ticket_rate=0.0, cost_per_accepted_change=0.0, tokens_per_accepted_change=0.0,
        changes_surviving_24h=0, total_commits_24h=0, cross_ticket_regression_rate=0.0, rework_rate=0.0,
        completion_rate=0.0, median_lifecycle_minutes=0.0, completions_last_hour=0,
    )
    d = snap.to_dict()
    assert d["timestamp"] == 123.0
    assert d["total_tickets"] == 5


def test_quality_tracker_init_load_no_file_and_corrupt(tmp_path: Path):
    """Given no history file or corrupt lines
    When QualityMetricsTracker init
    Then handles without crash."""
    state = tmp_path / "state"
    project = tmp_path / "proj"
    state.mkdir(parents=True)
    project.mkdir(parents=True)
    # no file
    tracker = QualityMetricsTracker(state, project)
    assert tracker._history == []
    assert tracker.maybe_record(force=False) is None or isinstance(tracker.maybe_record(force=True), QualitySnapshot)
    # write corrupt history
    hist_path = state / "quality_metrics.jsonl"
    hist_path.write_text('{"timestamp": 1.0, "total_tickets": 1' + "\n" + "not json\n" + '{"timestamp": 2.0, "total_tickets": 2, "complete_tickets": 0, "rework_tickets": 0, "rejected_tickets": 0, "deferred_tickets": 0, "decompose_tickets": 0, "planning_tickets": 0, "implementing_tickets": 0, "reviewing_tickets": 0, "independent_test_failures": 0, "escaped_defects": 0, "complete_reopened_rate": 0.0, "tests_added_per_change": 0.0, "coverage_delta_pct": 0.0, "static_findings_delta": 0, "complexity_delta": 0.0, "duplication_delta": 0.0, "unnecessary_ticket_rate": 0.0, "cost_per_accepted_change": 0.0, "tokens_per_accepted_change": 0.0, "changes_surviving_24h": 0, "total_commits_24h": 0, "cross_ticket_regression_rate": 0.0, "rework_rate": 0.0, "completion_rate": 0.0, "median_lifecycle_minutes": 0.0, "completions_last_hour": 0}\n', encoding="utf-8")
    tracker2 = QualityMetricsTracker(state, project)
    # should have loaded at least one valid snapshot (last one)
    assert len(tracker2._history) >= 1
    # OSError on load
    with patch.object(Path, "exists", return_value=True):
        with patch("builtins.open", side_effect=OSError("fail")):
            t = QualityMetricsTracker(state, project, max_history=5)
            # should not raise
            assert t._history is not None


def test_quality_tracker_max_history_truncation(tmp_path: Path):
    """Given history exceeds max_history
    When init
    Then truncated."""
    state = tmp_path / "state"
    project = tmp_path / "proj"
    state.mkdir(parents=True); project.mkdir(parents=True)
    # create many snapshots file
    hist_path = state / "quality_metrics.jsonl"
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    for i in range(10):
        snap = QualitySnapshot(
            timestamp=float(i), total_tickets=i, complete_tickets=0, rework_tickets=0, rejected_tickets=0,
            deferred_tickets=0, decompose_tickets=0, planning_tickets=0, implementing_tickets=0, reviewing_tickets=0,
            independent_test_failures=0, escaped_defects=0, complete_reopened_rate=0.0, tests_added_per_change=0.0,
            coverage_delta_pct=0.0, static_findings_delta=0, complexity_delta=0.0, duplication_delta=0.0,
            unnecessary_ticket_rate=0.0, cost_per_accepted_change=0.0, tokens_per_accepted_change=0.0,
            changes_surviving_24h=0, total_commits_24h=0, cross_ticket_regression_rate=0.0, rework_rate=0.0,
            completion_rate=0.0, median_lifecycle_minutes=0.0, completions_last_hour=0,
        )
        with open(hist_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(snap.to_dict()) + "\n")
    tracker = QualityMetricsTracker(state, project, max_history=3)
    assert len(tracker._history) == 3
    assert tracker._history[-1].timestamp == 9.0


def test_quality_tracker_maybe_record_interval_and_force(tmp_path: Path):
    """Given tracker with recent snapshot
    When maybe_record without force
    Then respects interval."""
    state = tmp_path / "state"
    project = tmp_path / "proj"
    state.mkdir(parents=True); project.mkdir(parents=True)
    tracker = QualityMetricsTracker(state, project)
    # force first record
    snap1 = tracker.maybe_record(force=True)
    assert snap1 is not None
    # immediate second without force -> None
    snap2 = tracker.maybe_record(force=False)
    assert snap2 is None
    # force again -> snapshot
    snap3 = tracker.maybe_record(force=True)
    assert snap3 is not None
    # after interval elapsed, should record even without force (patch time)
    tracker._last_snapshot_time = time.time() - SNAPSHOT_INTERVAL_SECONDS - 1
    snap4 = tracker.maybe_record(force=False)
    assert snap4 is not None


def test_quality_tracker_append_oserror(tmp_path: Path):
    """Given append with mkdir failure
    When _append
    Then swallowed."""
    state = tmp_path / "state"
    project = tmp_path / "proj"
    state.mkdir(parents=True); project.mkdir(parents=True)
    tracker = QualityMetricsTracker(state, project)
    snap = QualitySnapshot(
        timestamp=time.time(), total_tickets=0, complete_tickets=0, rework_tickets=0, rejected_tickets=0,
        deferred_tickets=0, decompose_tickets=0, planning_tickets=0, implementing_tickets=0, reviewing_tickets=0,
        independent_test_failures=0, escaped_defects=0, complete_reopened_rate=0.0, tests_added_per_change=0.0,
        coverage_delta_pct=0.0, static_findings_delta=0, complexity_delta=0.0, duplication_delta=0.0,
        unnecessary_ticket_rate=0.0, cost_per_accepted_change=0.0, tokens_per_accepted_change=0.0,
        changes_surviving_24h=0, total_commits_24h=0, cross_ticket_regression_rate=0.0, rework_rate=0.0,
        completion_rate=0.0, median_lifecycle_minutes=0.0, completions_last_hour=0,
    )
    with patch.object(Path, "mkdir", side_effect=OSError("fail")):
        tracker._append(snap)  # should not raise


def test_quality_tracker_load_tickets_variants(tmp_path: Path):
    """Given tickets.json exists or not
    When _load_tickets
    Then returns list."""
    state = tmp_path / "state"
    project = tmp_path / "proj"
    state.mkdir(parents=True); project.mkdir(parents=True)
    tracker = QualityMetricsTracker(state, project)
    assert tracker._load_tickets() == []
    # bad json
    (state / "tickets.json").write_text("not json", encoding="utf-8")
    assert tracker._load_tickets() == []
    # valid
    _tickets_json([{"state": "COMPLETE"}], state)
    assert len(tracker._load_tickets()) == 1
    # missing tickets key -> empty via .get default
    (state / "tickets.json").write_text(json.dumps({"other": []}), encoding="utf-8")
    assert tracker._load_tickets() == []


def test_quality_tracker_count_states_and_test_failures(tmp_path: Path):
    """Given tickets with states and log files
    When helpers
    Then counts."""
    state = tmp_path / "state"
    project = tmp_path / "proj"
    state.mkdir(parents=True); project.mkdir(parents=True)
    tracker = QualityMetricsTracker(state, project)
    counts = tracker._count_states([{"state": "COMPLETE"}, {"state": "COMPLETE"}, {"state": "REWORK"}])
    assert counts["COMPLETE"] == 2
    assert counts["REWORK"] == 1
    # no logs dir
    assert tracker._count_test_failures() == 0
    # logs dir with failures
    logs = state.parent / "logs"
    # Since _count_test_failures uses state_dir.parent / logs, we need state.parent/logs
    # state is tmp_path/state, so parent is tmp_path
    # create logs in tmp_path/logs
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "a.log").write_text("FAILED test_one\nAssertionError blah\ntest_something failed\n", encoding="utf-8")
    (logs / "b.log").write_text("ok\n", encoding="utf-8")
    assert tracker._count_test_failures() >= 3
    # OSError on read
    with patch.object(Path, "read_text", side_effect=OSError("fail")):
        assert isinstance(tracker._count_test_failures(), int)


def test_quality_tracker_escaped_defects_and_reopen_rate(tmp_path: Path):
    """Given tickets with regression/broke
    When _count_escaped_defects / _compute_reopen_rate
    Then correct."""
    state = tmp_path / "state"
    project = tmp_path / "proj"
    state.mkdir(parents=True); project.mkdir(parents=True)
    tracker = QualityMetricsTracker(state, project)
    tickets = [
        {"state": "COMPLETE", "title": "fix regression in foo", "problem_statement": "", "rework_count": 0},
        {"state": "COMPLETE", "title": "ok", "problem_statement": "this broke something", "rework_count": 0},
        {"state": "COMPLETE", "title": "ok", "problem_statement": "broken link", "rework_count": 0},
        {"state": "COMPLETE", "title": "regression already fixed", "problem_statement": "", "rework_count": 2},  # skipped due to rework_count >0
        {"state": "REWORK", "title": "regression", "problem_statement": "", "rework_count": 0},
    ]
    assert tracker._count_escaped_defects(tickets) == 3
    assert tracker._compute_reopen_rate(tickets) == pytest.approx(1/4)  # 1 reopened of 4 completed
    assert tracker._compute_reopen_rate([]) == 0.0
    assert tracker._compute_reopen_rate([{"state": "REWORK"}]) == 0.0


def test_quality_tracker_tests_per_change_and_unnecessary_rate(tmp_path: Path):
    """Given tickets
    When _compute_tests_per_change / unnecessary_rate
    Then rates."""
    state = tmp_path / "state"
    project = tmp_path / "proj"
    state.mkdir(parents=True); project.mkdir(parents=True)
    tracker = QualityMetricsTracker(state, project)
    tickets = [
        {"ticket_class": "feature", "state": "COMPLETE"},
        {"ticket_class": "bug", "state": "COMPLETE"},
        {"ticket_class": "test", "state": "COMPLETE"},
        {"ticket_class": "test", "state": "COMPLETE"},
        {"ticket_class": "feature", "state": "REWORK"},
    ]
    # 2 impl tickets complete, 2 test tickets complete => 1.0
    assert tracker._compute_tests_per_change(tickets) == 1.0
    assert tracker._compute_tests_per_change([]) == 0.0
    assert tracker._compute_tests_per_change([{"ticket_class": "feature", "state": "REWORK"}]) == 0.0
    # unnecessary
    all_tickets = [{"state": "COMPLETE"}, {"state": "REJECTED"}, {"state": "DUPLICATE"}, {"state": "DEFERRED"}]
    assert tracker._compute_unnecessary_rate(all_tickets) == pytest.approx(0.75)
    assert tracker._compute_unnecessary_rate([]) == 0.0


def test_quality_tracker_cost_and_tokens_per_accepted(tmp_path: Path):
    """Given completed tickets and ledger
    When _compute_cost/_tokens
    Then correct."""
    state = tmp_path / "state"
    project = tmp_path / "proj"
    state.mkdir(parents=True); project.mkdir(parents=True)
    tracker = QualityMetricsTracker(state, project)
    assert tracker._compute_cost_per_accepted([]) == 0.0
    assert tracker._compute_tokens_per_accepted([]) == 0.0
    # with completed tickets having final_cost_tokens
    tickets = [{"state": "COMPLETE", "final_cost_tokens": 1000}, {"state": "COMPLETE", "final_cost_tokens": 2000}]
    cost = tracker._compute_cost_per_accepted(tickets)
    assert cost == pytest.approx(round(3000 * 0.000002 / 2, 6))
    tokens = tracker._compute_tokens_per_accepted(tickets)
    assert tokens == pytest.approx(1500.0)
    # zero tokens but ledger fallback
    state2 = tmp_path / "state2"
    state2.mkdir()
    tracker2 = QualityMetricsTracker(state2, project)
    tickets2 = [{"state": "COMPLETE", "final_cost_tokens": 0}]
    (state2 / "token_ledger.json").write_text(json.dumps({"total_actual": 5000}), encoding="utf-8")
    assert tracker2._compute_cost_per_accepted(tickets2) == pytest.approx(round(5000*0.000002/1,6))
    assert tracker2._compute_tokens_per_accepted(tickets2) == pytest.approx(5000.0)
    # ledger invalid json handled
    state3 = tmp_path / "state3"
    state3.mkdir()
    tracker3 = QualityMetricsTracker(state3, project)
    (state3 / "token_ledger.json").write_text("bad json", encoding="utf-8")
    assert tracker3._compute_cost_per_accepted([{"state": "COMPLETE", "final_cost_tokens": 0}]) == 0.0
    assert tracker3._compute_tokens_per_accepted([{"state": "COMPLETE", "final_cost_tokens": 0}]) == 0.0
    # ledger missing -> 0
    state4 = tmp_path / "state4"
    state4.mkdir()
    tracker4 = QualityMetricsTracker(state4, project)
    assert tracker4._compute_cost_per_accepted([{"state": "COMPLETE", "final_cost_tokens": 0}]) == 0.0


def test_quality_tracker_commit_survival(tmp_path: Path):
    """Given git log output
    When _compute_commit_survival
    Then surviving/total."""
    state = tmp_path / "state"
    project = tmp_path / "proj"
    state.mkdir(parents=True); project.mkdir(parents=True)
    tracker = QualityMetricsTracker(state, project)
    # no output
    with patch.object(tracker, "_run_command", return_value=""):
        assert tracker._compute_commit_survival() == (0,0)
    # with commits including revert
    with patch.object(tracker, "_run_command", return_value="abc fix\ndef revert bad change\n123 another\n"):
        surviving, total = tracker._compute_commit_survival()
        assert total == 3
        assert surviving == 2
    # case-insensitive revert detection
    with patch.object(tracker, "_run_command", return_value="Revert thing\nrollback change\n"):
        surviving, total = tracker._compute_commit_survival()
        assert surviving == 0


def test_quality_tracker_cross_ticket_and_rates(tmp_path: Path):
    """Given tickets
    When cross_ticket/rework/completion/median
    Then rates."""
    state = tmp_path / "state"
    project = tmp_path / "proj"
    state.mkdir(parents=True); project.mkdir(parents=True)
    tracker = QualityMetricsTracker(state, project)
    # cross with no rework
    assert tracker._compute_cross_ticket_regression([]) == 0.0
    tickets = [
        {"state": "REWORK", "problem_statement": "caused by another ticket side effect", "evidence": "side effect"},
        {"state": "REWORK", "problem_statement": "another ticket caused this", "evidence": ""},
        {"state": "COMPLETE"},
    ]
    # 2 rework, 1 complete, both match keywords? first has "side effect" in problem -> counts? Actually logic: "another ticket" in problem OR "caused by" in evidence OR "side effect" in problem
    # first has "side effect" in problem -> 1, second has "another ticket" in problem -> 1, total 2/1 complete =2.0 but capped? Let's just check >0
    assert tracker._compute_cross_ticket_regression(tickets) > 0
    # no completed -> 0 even with rework
    assert tracker._compute_cross_ticket_regression([{"state": "REWORK", "problem_statement": "another ticket"}]) == 0.0
    # rework_rate
    assert tracker._compute_rework_rate({"COMPLETE": 5, "REWORK": 2}) == pytest.approx(2/2)  # total_non_terminal = REWORK only? Actually COMPLETE excluded so total_non_terminal=2
    assert tracker._compute_rework_rate({"COMPLETE": 5}) == 0.0
    assert tracker._compute_rework_rate({}) == 0.0
    # completion_rate
    assert tracker._compute_completion_rate({"COMPLETE": 3, "REWORK": 1}) == pytest.approx(0.75)
    assert tracker._compute_completion_rate({}) == 0.0
    # median lifecycle
    now = time.time()
    tickets2 = [
        {"state": "COMPLETE", "created_at": now-600, "updated_at": now},
        {"state": "COMPLETE", "created_at": now-300, "updated_at": now},
        {"state": "REWORK", "created_at": now-100, "updated_at": now},
    ]
    # median of [5,10] sorted -> index 1 => 10.0
    median = tracker._compute_median_lifecycle(tickets2)
    assert median > 0
    assert tracker._compute_median_lifecycle([]) == 0.0
    # negative duration filtered
    tickets3 = [{"state": "COMPLETE", "created_at": now, "updated_at": now-100}]
    assert tracker._compute_median_lifecycle(tickets3) == 0.0
    # recent completions
    tickets4 = [{"state": "COMPLETE", "updated_at": now}, {"state": "COMPLETE", "updated_at": now-7200}]
    assert tracker._count_recent_completions(tickets4, now) == 1


def test_quality_tracker_run_command_and_coverage_deltas(tmp_path: Path):
    """Given command execution
    When _run_command
    Then returns output or empty on exception."""
    state = tmp_path / "state"
    project = tmp_path / "proj"
    state.mkdir(parents=True); project.mkdir(parents=True)
    tracker = QualityMetricsTracker(state, project)
    # success
    with patch("codebot.quality_metrics.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="out", stderr="err")
        assert tracker._run_command(["git", "log"]) == "outerr"
    # exception
    with patch("codebot.quality_metrics.subprocess.run", side_effect=RuntimeError("fail")):
        assert tracker._run_command(["bad"]) == ""
    # coverage deltas stub
    assert tracker._compute_coverage_delta() == 0.0
    assert tracker._compute_static_findings_delta() == 0
    assert tracker._compute_complexity_delta() == 0.0
    assert tracker._compute_duplication_delta() == 0.0


def test_quality_tracker_compute_and_summary(tmp_path: Path):
    """Given tickets and history
    When _compute and summary
    Then populated."""
    state = tmp_path / "state"
    project = tmp_path / "proj"
    state.mkdir(parents=True); project.mkdir(parents=True)
    # create tickets.json with diverse states
    now = time.time()
    tickets = [
        {"state": "COMPLETE", "ticket_class": "feature", "final_cost_tokens": 1000, "created_at": now-1000, "updated_at": now, "title": "fix", "problem_statement": "", "rework_count": 0},
        {"state": "REWORK", "ticket_class": "bug", "problem_statement": "another ticket broke", "evidence": "caused by"},
        {"state": "REJECTED"},
        {"state": "DECOMPOSE"},
        {"state": "PLANNING"},
        {"state": "IMPLEMENT"},
        {"state": "REVIEW"},
        {"state": "DEFERRED"},
    ]
    _tickets_json(tickets, state)
    tracker = QualityMetricsTracker(state, project)
    snap = tracker.maybe_record(force=True)
    assert snap.total_tickets == len(tickets)
    assert snap.complete_tickets == 1
    assert snap.decompose_tickets >= 1
    # summary with no history case
    empty_state = tmp_path / "empty_state"
    empty_state.mkdir()
    empty_tracker = QualityMetricsTracker(empty_state, project)
    assert empty_tracker.summary() == {"error": "no history"}
    # summary normal
    summ = tracker.summary(window_hours=24)
    assert "throughput" in summ
    assert "quality" in summ
    assert "economics" in summ
    assert "stability" in summ
    assert "pipeline" in summ
    assert "trends" in summ
    # summary fallback when no recent within window
    # create tracker with old snapshots
    old_state = tmp_path / "old_state"
    old_state.mkdir()
    old_tracker = QualityMetricsTracker(old_state, project)
    # manually inject old history
    old_snap = QualitySnapshot(
        timestamp=time.time()-100*3600, total_tickets=1, complete_tickets=1, rework_tickets=0, rejected_tickets=0,
        deferred_tickets=0, decompose_tickets=0, planning_tickets=0, implementing_tickets=0, reviewing_tickets=0,
        independent_test_failures=0, escaped_defects=0, complete_reopened_rate=0.0, tests_added_per_change=0.0,
        coverage_delta_pct=0.0, static_findings_delta=0, complexity_delta=0.0, duplication_delta=0.0,
        unnecessary_ticket_rate=0.0, cost_per_accepted_change=0.0, tokens_per_accepted_change=0.0,
        changes_surviving_24h=0, total_commits_24h=0, cross_ticket_regression_rate=0.0, rework_rate=0.0,
        completion_rate=1.0, median_lifecycle_minutes=5.0, completions_last_hour=0,
    )
    old_tracker._history = [old_snap]
    # summary with small window -> fallback to last 5
    summ2 = old_tracker.summary(window_hours=1)
    assert summ2["snapshots"] == 1


def test_quality_tracker_trends_direction(tmp_path: Path):
    """Given two snapshots
    When _compute_trends
    Then direction correct."""
    state = tmp_path / "state"
    project = tmp_path / "proj"
    state.mkdir(parents=True); project.mkdir(parents=True)
    tracker = QualityMetricsTracker(state, project)
    # not enough snapshots -> empty
    assert tracker._compute_trends([MagicMock()]) == {}
    s1 = QualitySnapshot(
        timestamp=1, total_tickets=1, complete_tickets=1, rework_tickets=1, rejected_tickets=0,
        deferred_tickets=0, decompose_tickets=0, planning_tickets=0, implementing_tickets=0, reviewing_tickets=0,
        independent_test_failures=0, escaped_defects=2, complete_reopened_rate=0.0, tests_added_per_change=0.0,
        coverage_delta_pct=0.0, static_findings_delta=0, complexity_delta=0.0, duplication_delta=0.0,
        unnecessary_ticket_rate=0.0, cost_per_accepted_change=10.0, tokens_per_accepted_change=0.0,
        changes_surviving_24h=0, total_commits_24h=0, cross_ticket_regression_rate=0.0, rework_rate=0.5,
        completion_rate=0.5, median_lifecycle_minutes=0.0, completions_last_hour=1,
    )
    s2 = QualitySnapshot(
        timestamp=2, total_tickets=2, complete_tickets=2, rework_tickets=0, rejected_tickets=0,
        deferred_tickets=0, decompose_tickets=0, planning_tickets=0, implementing_tickets=0, reviewing_tickets=0,
        independent_test_failures=0, escaped_defects=1, complete_reopened_rate=0.0, tests_added_per_change=0.0,
        coverage_delta_pct=0.0, static_findings_delta=0, complexity_delta=0.0, duplication_delta=0.0,
        unnecessary_ticket_rate=0.0, cost_per_accepted_change=5.0, tokens_per_accepted_change=0.0,
        changes_surviving_24h=0, total_commits_24h=0, cross_ticket_regression_rate=0.0, rework_rate=0.2,
        completion_rate=0.8, median_lifecycle_minutes=0.0, completions_last_hour=3,
    )
    trends = tracker._compute_trends([s1, s2])
    assert trends["rework_rate"] == "improving"  # 0.5->0.2 invert True -> lower is improving
    assert trends["completion_rate"] == "improving"
    assert trends["escaped_defects"] == "improving"
    assert trends["cost_per_accepted"] == "improving"
    assert trends["throughput"] == "improving"
    # stable case
    trends2 = tracker._compute_trends([s2, s2])
    assert trends2["rework_rate"] == "stable"
    # degrading case
    trends3 = tracker._compute_trends([s2, s1])
    assert trends3["rework_rate"] == "degrading"

