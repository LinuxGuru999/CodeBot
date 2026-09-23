#!/usr/bin/env python3
"""Pre-ticket evidence validation for discovery findings.

Purpose
-------
Validates that evidence referenced in a discovery finding actually exists
in the current repository state BEFORE a ticket is created. This prevents
stale findings, hallucinated file references, and phantom symbols from
entering the downstream pipeline.

Why
---
Discovery agents operate on a snapshot of the repository. Between the
moment an agent observes a problem and the moment the ticket is created,
another agent may have already fixed the issue, the file may have been
deleted, or the agent may have hallucinated a reference entirely. This
module performs a lightweight recheck immediately before ticket creation
(§7: Repository Revision Awareness, §37: Hallucinated Evidence).

Invariants
----------
- stdlib-only (pathlib, re, ast, subprocess, logging)
- Read-only: never modifies files
- Bounded: each validation reads at most MAX_FILE_BYTES per file
- Fast: symbol lookup uses regex first, falls back to AST only for .py
- Fail-safe: I/O errors produce warnings, never crash discovery
"""

from __future__ import annotations

import ast
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 500_000  # 500KB read cap per evidence file
MAX_SYMBOLS_PER_FILE = 200  # cap on AST symbols extracted

# Directories/patterns that are generated, vendored, or non-source
GENERATED_PATTERNS: tuple[str, ...] = (
    "__pycache__",
    ".pyc",
    "node_modules",
    ".git/",
    "dist/",
    "build/",
    ".tox/",
    ".mypy_cache/",
    ".pytest_cache/",
    "*.egg-info",
    "coverage/",
    ".venv/",
    "venv/",
    ".codebot/state/",
    ".omo/",
    ".opencode/",
    "logs/",
)

# File extensions that are never editable source
NON_SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe",
    ".lock", ".log", ".jsonl", ".wal.jsonl",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".eot",
    ".zip", ".tar", ".gz", ".bz2", ".xz",
    ".pdf", ".doc", ".docx",
})

# Directories that indicate vendored/third-party code
VENDOR_INDICATORS: frozenset[str] = frozenset({
    "vendor/", "third_party/", "third-party/", "external/",
    "node_modules/", ".bundle/", "gems/",
})

_SYMBOL_RE = re.compile(
    r"^\s*(?:def|class|async\s+def)\s+([a-zA-Z_]\w*)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class EvidenceCheckResult:
    """Result of validating one evidence reference."""
    file_exists: bool = True
    file_is_generated: bool = False
    file_is_vendored: bool = False
    file_is_non_source: bool = False
    symbol_exists: bool = True
    line_exists: bool = True
    content_matches: bool = True
    warnings: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        return (
            self.file_exists
            and not self.file_is_generated
            and not self.file_is_vendored
            and not self.file_is_non_source
            and self.symbol_exists
            and self.line_exists
            and self.content_matches
        )

    @property
    def rejection_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.file_exists:
            reasons.append("file_does_not_exist")
        if self.file_is_generated:
            reasons.append("generated_file")
        if self.file_is_vendored:
            reasons.append("vendored_code")
        if self.file_is_non_source:
            reasons.append("non_source_file")
        if not self.symbol_exists:
            reasons.append("symbol_not_found")
        if not self.line_exists:
            reasons.append("line_not_found")
        if not self.content_matches:
            reasons.append("content_mismatch")
        return tuple(reasons)


@dataclass(frozen=True)
class EvidenceValidationResult:
    """Aggregate result of validating all evidence in a finding."""
    checks: tuple[EvidenceCheckResult, ...]
    hallucination_detected: bool = False
    stale_evidence: bool = False
    generated_file_reference: bool = False
    vendor_reference: bool = False

    @property
    def all_valid(self) -> bool:
        return all(c.is_valid for c in self.checks)

    @property
    def has_rejections(self) -> bool:
        return any(not c.is_valid for c in self.checks)

    @property
    def rejection_summary(self) -> list[str]:
        """All unique rejection reasons across all checks."""
        reasons: set[str] = set()
        for c in self.checks:
            reasons.update(c.rejection_reasons)
        return sorted(reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "all_valid": self.all_valid,
            "hallucination_detected": self.hallucination_detected,
            "stale_evidence": self.stale_evidence,
            "generated_file_reference": self.generated_file_reference,
            "vendor_reference": self.vendor_reference,
            "rejection_summary": self.rejection_summary,
            "checks_count": len(self.checks),
        }


def is_generated_path(path_str: str) -> bool:
    """Check if a path matches known generated/build output patterns."""
    normalized = path_str.replace("\\", "/")
    for pattern in GENERATED_PATTERNS:
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if suffix in normalized:
                return True
        elif pattern.endswith("/"):
            if f"/{pattern}" in f"/{normalized}" or normalized.startswith(pattern):
                return True
        else:
            if pattern in normalized:
                return True
    return False


def is_vendored_path(path_str: str) -> bool:
    """Check if a path appears to be vendored/third-party code."""
    normalized = path_str.replace("\\", "/").lower()
    for indicator in VENDOR_INDICATORS:
        if indicator in normalized:
            return True
    return False


def is_non_source_extension(path_str: str) -> bool:
    """Check if a file extension indicates non-source content."""
    return Path(path_str).suffix.lower() in NON_SOURCE_EXTENSIONS


def _resolve_path(project_root: Path, file_path: str) -> Path | None:
    """Resolve a relative evidence path against project root safely.

    Returns None if the path escapes the project root (traversal attack)
    or is absolute.
    """
    if not file_path or file_path.startswith("/") or file_path.startswith("\\"):
        return None
    if ".." in Path(file_path).parts:
        return None
    resolved = (project_root / file_path).resolve()
    try:
        resolved.relative_to(project_root.resolve())
    except ValueError:
        return None
    return resolved


def _check_file_exists(project_root: Path, file_path: str) -> tuple[bool, Path | None]:
    """Check if file exists. Returns (exists, resolved_path)."""
    resolved = _resolve_path(project_root, file_path)
    if resolved is None:
        return False, None
    return resolved.is_file(), resolved


def _check_symbol_in_file(
    resolved_path: Path, symbol: str
) -> bool:
    """Check if a symbol (function/class name) exists in a file.

    Uses regex first (fast), falls back to AST for Python files.
    """
    if not symbol:
        return True  # No symbol to check

    try:
        content = resolved_path.read_text(
            encoding="utf-8", errors="ignore"
        )[:MAX_FILE_BYTES]
    except OSError:
        return False

    # Fast path: regex search
    if re.search(rf"\b{re.escape(symbol)}\b", content):
        return True

    # For Python files, try AST parsing
    if resolved_path.suffix == ".py":
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name == symbol:
                        return True
                elif isinstance(node, ast.Name) and node.id == symbol:
                    return True
                elif isinstance(node, ast.Attribute) and node.attr == symbol:
                    return True
        except SyntaxError:
            pass

    return False


def _check_line_in_file(resolved_path: Path, line_number: int) -> bool:
    """Check if a line number is within the file's bounds."""
    if line_number <= 0:
        return True  # No line to check

    try:
        with open(resolved_path, "r", encoding="utf-8", errors="ignore") as f:
            for i, _ in enumerate(f, 1):
                if i >= line_number:
                    return True
                if i > line_number + 100:  # Safety bound
                    break
    except OSError:
        return False

    # If we exhausted the file without reaching line_number
    try:
        content = resolved_path.read_text(encoding="utf-8", errors="ignore")
        total_lines = content.count("\n") + 1
        return line_number <= total_lines
    except OSError:
        return False


def _check_content_match(
    resolved_path: Path, excerpt: str, line_number: int = 0
) -> bool:
    """Check if an evidence excerpt appears in the file.

    Only checks when excerpt is non-empty. Uses fuzzy matching:
    normalizes whitespace and checks for substring presence.
    """
    if not excerpt or len(excerpt.strip()) < 10:
        return True  # Too short to meaningfully check

    try:
        content = resolved_path.read_text(
            encoding="utf-8", errors="ignore"
        )[:MAX_FILE_BYTES]
    except OSError:
        return False

    # Normalize whitespace for comparison
    normalized_content = re.sub(r"\s+", " ", content)
    normalized_excerpt = re.sub(r"\s+", " ", excerpt.strip())

    # Check first 100 chars of excerpt (enough to confirm presence)
    check_str = normalized_excerpt[:100]
    return check_str in normalized_content


def get_current_revision(project_root: Path) -> str:
    """Get the current git HEAD revision of the repository.

    Returns empty string if git is unavailable or not a repo.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        pass
    return ""


def validate_evidence_item(
    project_root: Path,
    file_path: str,
    symbol: str = "",
    line_number: int = 0,
    excerpt: str = "",
) -> EvidenceCheckResult:
    """Validate a single evidence reference against the live repository.

    This is the core pre-ticket-creation check (§7, §37).
    """
    warnings: list[str] = []

    # Check generated/vendored/non-source first (fast rejection)
    if file_path:
        if is_generated_path(file_path):
            return EvidenceCheckResult(
                file_exists=True,
                file_is_generated=True,
                warnings=("references generated/build output",),
            )
        if is_vendored_path(file_path):
            return EvidenceCheckResult(
                file_exists=True,
                file_is_vendored=True,
                warnings=("references vendored/third-party code",),
            )
        if is_non_source_extension(file_path):
            return EvidenceCheckResult(
                file_exists=True,
                file_is_non_source=True,
                warnings=("references non-source file type",),
            )

    # Check file existence
    exists, resolved = _check_file_exists(project_root, file_path)
    if not exists:
        return EvidenceCheckResult(
            file_exists=False,
            warnings=(f"file not found: {file_path}",),
        )

    assert resolved is not None

    # Check symbol
    symbol_ok = _check_symbol_in_file(resolved, symbol)
    if not symbol_ok:
        warnings.append(f"symbol '{symbol}' not found in {file_path}")

    # Check line number
    line_ok = _check_line_in_file(resolved, line_number)
    if not line_ok:
        warnings.append(f"line {line_number} beyond file end: {file_path}")

    # Check content match
    content_ok = _check_content_match(resolved, excerpt, line_number)
    if not content_ok:
        warnings.append(f"excerpt not found in {file_path}")

    return EvidenceCheckResult(
        file_exists=True,
        symbol_exists=symbol_ok,
        line_exists=line_ok,
        content_matches=content_ok,
        warnings=tuple(warnings),
    )


def validate_finding_evidence(
    project_root: Path,
    evidence_items: list[dict[str, Any]],
) -> EvidenceValidationResult:
    """Validate all evidence items in a finding.

    Args:
        project_root: Repository root path.
        evidence_items: List of dicts with keys: file_path, symbol,
            line_number, excerpt, observation.

    Returns:
        EvidenceValidationResult with per-item checks and aggregate flags.
    """
    checks: list[EvidenceCheckResult] = []
    hallucination = False
    stale = False
    generated_ref = False
    vendor_ref = False

    for item in evidence_items:
        file_path = str(item.get("file_path", "") or "").strip()
        symbol = str(item.get("symbol", "") or "").strip()
        line_number = int(item.get("line_number", 0) or 0)
        excerpt = str(item.get("excerpt", "") or "").strip()

        if not file_path:
            # Evidence without a file reference — observation-only
            checks.append(EvidenceCheckResult())
            continue

        result = validate_evidence_item(
            project_root, file_path, symbol, line_number, excerpt
        )
        checks.append(result)

        if not result.file_exists:
            hallucination = True
        if result.file_is_generated:
            generated_ref = True
        if result.file_is_vendored:
            vendor_ref = True
        if not result.symbol_exists or not result.content_matches:
            stale = True

    return EvidenceValidationResult(
        checks=tuple(checks),
        hallucination_detected=hallucination,
        stale_evidence=stale,
        generated_file_reference=generated_ref,
        vendor_reference=vendor_ref,
    )


def revalidate_before_ticket_creation(
    project_root: Path,
    evidence_items: list[dict[str, Any]],
    expected_revision: str = "",
) -> tuple[bool, str]:
    """Final gate before ticket creation (§7: revalidate evidence).

    Returns:
        (should_create, reason) — if should_create is False, reason
        explains why the ticket should NOT be created.
    """
    if not evidence_items:
        return False, "no evidence provided"

    # Check repository revision if available
    if expected_revision:
        current_rev = get_current_revision(project_root)
        if current_rev and current_rev != expected_revision:
            # Repository changed since discovery — revalidate
            pass  # Continue with validation; revision mismatch is noted
            # but not automatically fatal (the evidence may still exist)

    result = validate_finding_evidence(project_root, evidence_items)

    if result.hallucination_detected:
        return False, (
            "hallucinated evidence: referenced file(s) do not exist. "
            f"Rejections: {result.rejection_summary}"
        )

    if result.generated_file_reference:
        return False, (
            "evidence references generated/build output files which "
            "should not receive engineering tickets"
        )

    if result.vendor_reference:
        return False, (
            "evidence references vendored/third-party code which "
            "should not receive normal editable-source tickets"
        )

    # Count valid checks
    valid_count = sum(1 for c in result.checks if c.is_valid)
    total_with_files = sum(1 for c in result.checks if True)  # all checks

    if valid_count == 0 and total_with_files > 0:
        return False, (
            "no evidence could be verified against current repository state. "
            f"Rejections: {result.rejection_summary}"
        )

    # If some evidence is stale but some is valid, allow with warning
    if result.stale_evidence and valid_count > 0:
        logger.warning(
            "Partial evidence staleness detected: %d/%d checks passed",
            valid_count, len(result.checks),
        )

    return True, "evidence validated"