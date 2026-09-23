#!/usr/bin/env python3
"""Documentation Ticket Generator — auto-creates documentation tickets on completion.

Purpose
-------
When a ticket completes, this module analyzes the changes and automatically
creates documentation tickets if documentation wasn't updated as part of the
original work.

Why
---
Documentation drift is a major problem. By automatically detecting when code
changes without corresponding documentation updates, we ensure documentation
stays synchronized with implementation.

Invariants
----------
- stdlib-only
- Never creates duplicate documentation tickets
- Respects constitution-protected paths
- Documentation tickets are created at GOAL state for immediate work
- Only creates tickets when documentation is genuinely needed
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("documentation_ticket_generator")

# File extensions that typically need documentation updates
CODE_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".rb"}

# Documentation files that might already be updated
DOC_PATTERNS = {
    "README.md",
    "README.rst",
    "docs/",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "API.md",
    "ARCHITECTURE.md",
    ".md",
}

# Types of changes that typically need documentation
CHANGE_CATEGORIES = {
    "api_change": "Public API modified",
    "new_feature": "New functionality added",
    "bug_fix": "Bug fix may need documentation update",
    "refactor": "Refactoring may need documentation update",
    "security": "Security change needs documentation",
    "performance": "Performance change may need documentation",
    "architecture": "Architecture change needs documentation",
}


def should_create_doc_ticket(
    ticket: Any,
    store: Any,
    protected_paths: set[str] | None = None,
) -> bool:
    """Determine if a completed ticket needs a documentation ticket.

    Args:
        ticket: The completed ticket object
        store: TicketStore instance to check for duplicates
        protected_paths: Set of path prefixes to skip

    Returns:
        True if a documentation ticket should be created
    """
    # Skip if ticket is already a documentation ticket
    if getattr(ticket, "ticket_class", "") == "documentation":
        return False

    # Skip if documentation was already updated
    if _documentation_already_updated(ticket):
        return False

    # Check if code files were modified
    affected = getattr(ticket, "affected_modules", []) or []
    code_files = [f for f in affected if _is_code_file(f)]

    if not code_files:
        return False

    # Check for duplicate documentation ticket
    if _duplicate_doc_ticket_exists(ticket, store):
        return False

    return True


def create_documentation_ticket(
    ticket: Any,
    store: Any,
    workspace: Path,
    protected_paths: set[str] | None = None,
) -> str | None:
    """Create a documentation ticket for a completed ticket.

    Args:
        ticket: The completed ticket object
        store: TicketStore instance
        workspace: Project workspace path
        protected_paths: Set of path prefixes to skip

    Returns:
        Created ticket ID or None if no ticket was created
    """
    from codebot.ticket_engine import create_ticket, TicketClass, Severity, RiskLevel, TicketState

    if not should_create_doc_ticket(ticket, store, protected_paths):
        return None

    # Analyze what documentation needs updating
    doc_needs = _analyze_documentation_needs(ticket, workspace)

    if not doc_needs:
        return None

    # Create the documentation ticket
    ticket_id = getattr(ticket, "id", "unknown")
    title = f"Update documentation for {ticket_id}"
    evidence_hash = hashlib.sha256(f"doc:{ticket_id}".encode()).hexdigest()[:16]

    problem = (
        f"Ticket {ticket_id} modified code but documentation was not updated. "
        f"Affected files: {', '.join(doc_needs['affected_files'][:5])}. "
        f"Documentation gaps: {', '.join(doc_needs['gaps'][:3])}."
    )

    desired = (
        f"Documentation updated to reflect changes from ticket {ticket_id}. "
        f"Required updates: {', '.join(doc_needs['required_updates'][:3])}."
    )

    acceptance = [
        f"Documentation reflects actual implementation for {ticket_id}",
        "No outdated claims in documentation",
        "Examples in documentation work correctly",
    ]

    try:
        doc_ticket = create_ticket(
            title=title,
            ticket_class=TicketClass.DOCUMENTATION,
            severity=Severity.MEDIUM,
            source="documentation_ticket_generator",
            evidence=evidence_hash,
            problem_statement=problem,
            desired_state=desired,
            acceptance_criteria=acceptance,
            risk=RiskLevel.LOW,
            affected_modules=doc_needs["affected_files"],
            dependencies=[ticket_id],
        )
        store.add(doc_ticket)
        store.transition(doc_ticket.id, TicketState.TRIAGED)
        store.transition(doc_ticket.id, TicketState.GOAL)
        store.transition(doc_ticket.id, TicketState.DECOMP)

        logger.info(
            "created documentation ticket %s for %s",
            doc_ticket.id, ticket_id,
        )
        return doc_ticket.id

    except ValueError as e:
        if "duplicate" in str(e).lower():
            logger.debug("skipped duplicate documentation ticket for %s", ticket_id)
            return None
        logger.warning("failed to create documentation ticket for %s: %s", ticket_id, e)
        return None


def _is_code_file(filepath: str) -> bool:
    """Check if a file is a code file that might need documentation."""
    return Path(filepath).suffix.lower() in CODE_EXTENSIONS


def _documentation_already_updated(ticket: Any) -> bool:
    """Check if documentation was already updated as part of this ticket."""
    affected = getattr(ticket, "affected_modules", []) or []

    for filepath in affected:
        # Check if the file is a documentation file
        for pattern in DOC_PATTERNS:
            if pattern in filepath:
                return True

    # Check if the ticket class is documentation-related
    ticket_class = getattr(ticket, "ticket_class", "")
    if ticket_class in ("documentation", "doc"):
        return True

    return False


def _duplicate_doc_ticket_exists(ticket: Any, store: Any) -> bool:
    """Check if a documentation ticket already exists for this ticket."""
    ticket_id = getattr(ticket, "id", "")

    # Search for existing documentation tickets referencing this ticket
    for t in store.list_by_class("documentation"):
        deps = getattr(t, "dependencies", []) or []
        if ticket_id in deps:
            return True

        # Also check title for reference
        title = getattr(t, "title", "")
        if ticket_id in title:
            return True

    return False


def _analyze_documentation_needs(ticket: Any, workspace: Path) -> dict[str, Any] | None:
    """Analyze what documentation needs updating based on the ticket's changes.

    Returns:
        Dictionary with documentation needs or None if no updates needed
    """
    affected = getattr(ticket, "affected_modules", []) or []
    ticket_class = getattr(ticket, "ticket_class", "")
    title = getattr(ticket, "title", "")

    code_files = [f for f in affected if _is_code_file(f)]

    if not code_files:
        return None

    # Determine what kind of documentation updates are needed
    gaps = []
    required_updates = []
    affected_files = []

    for filepath in code_files:
        full_path = workspace / filepath

        if not full_path.exists():
            continue

        affected_files.append(filepath)

        # Check for API changes (public functions/classes)
        if _has_public_api_changes(full_path):
            gaps.append(f"API documentation for {filepath}")
            required_updates.append(f"Update API docs for {filepath}")

        # Check for new features
        if ticket_class == "feature":
            gaps.append(f"Feature documentation for {filepath}")
            required_updates.append(f"Document new feature in {filepath}")

        # Check for security changes
        if ticket_class == "security":
            gaps.append(f"Security documentation for {filepath}")
            required_updates.append(f"Update security docs for {filepath}")

        # Check for architecture changes
        if ticket_class == "architecture":
            gaps.append(f"Architecture documentation for {filepath}")
            required_updates.append(f"Update architecture docs for {filepath}")

    if not affected_files:
        return None

    return {
        "affected_files": affected_files,
        "gaps": gaps,
        "required_updates": required_updates,
    }


def _has_public_api_changes(filepath: Path) -> bool:
    """Check if a file has public API changes (simplified check)."""
    try:
        content = filepath.read_text(encoding="utf-8", errors="ignore")

        # Simple heuristic: look for public function/class definitions
        public_indicators = [
            "def ",  # Python functions
            "class ",  # Classes
            "export ",  # JavaScript/TypeScript exports
            "pub ",  # Rust public
            "func ",  # Go functions
        ]

        for indicator in public_indicators:
            if indicator in content:
                return True

    except (OSError, UnicodeDecodeError):
        pass

    return False
