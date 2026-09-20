"""Tests for CB-5190476-CBC6: Orchestrator decoupled from direct TicketStore/QUEUE.md access.

Acceptance Criteria:
  - no direct TicketStore imports in orchestrator.py
  - no direct file reads of QUEUE.md in orchestrator.py
  - queue depth provided via adapter
"""
import ast
import re
from pathlib import Path


def test_no_direct_ticketstore_imports_in_orchestrator():
    """Verify orchestrator.py does not directly import TicketStore or related functions."""
    orchestrator_path = Path(__file__).parent.parent / "codebot" / "orchestrator.py"
    assert orchestrator_path.exists(), "orchestrator.py not found"

    source = orchestrator_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Check for direct imports from ticket_engine
    direct_ticket_engine_imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "codebot.ticket_engine":
                for alias in node.names:
                    if alias.name in ("TicketStore", "get_ticket_store", "clear_ticket_store_cache"):
                        direct_ticket_engine_imports.append(alias.name)
            elif node.module == "codebot.ticket_dispatcher":
                for alias in node.names:
                    if alias.name in ("TicketStore", "get_ticket_store", "clear_ticket_store_cache"):
                        direct_ticket_engine_imports.append(alias.name)

    assert not direct_ticket_engine_imports, (
        f"orchestrator.py has direct TicketStore imports: {direct_ticket_engine_imports}. "
        "Should use QueueManager or adapter interface instead."
    )


def test_no_queue_md_reads_in_orchestrator():
    """Verify orchestrator.py does not directly read QUEUE.md files."""
    orchestrator_path = Path(__file__).parent.parent / "codebot" / "orchestrator.py"
    assert orchestrator_path.exists(), "orchestrator.py not found"

    source = orchestrator_path.read_text(encoding="utf-8")

    # Check for QUEUE.md string literals or path constructions
    queue_md_patterns = [
        r'["\']QUEUE\.md["\']',
        r"QUEUE\.md",
        r"queue_file.*QUEUE",
        r"docs.*triage.*QUEUE",
    ]

    violations = []
    for pattern in queue_md_patterns:
        matches = re.findall(pattern, source, re.IGNORECASE)
        if matches:
            violations.extend(matches)

    assert not violations, (
        f"orchestrator.py contains direct QUEUE.md references: {violations}. "
        "Should use adapter interface for queue access."
    )


def test_queue_depth_via_adapter_in_init_tick():
    """Verify _init_tick() uses adapter interface for queue depth."""
    orchestrator_path = Path(__file__).parent.parent / "codebot" / "orchestrator.py"
    assert orchestrator_path.exists(), "orchestrator.py not found"

    source = orchestrator_path.read_text(encoding="utf-8")

    # Check that _init_tick uses adapter or QueueManager (which wraps adapter)
    init_tick_match = re.search(
        r"def _init_tick\(\).*?^(?=def |$)",
        source,
        re.MULTILINE | re.DOTALL
    )
    assert init_tick_match, "_init_tick() function not found"

    init_tick_body = init_tick_match.group(0)

    # Should use adapter.queue_depth() OR QueueManager (which provides adapter interface)
    has_adapter_queue_depth = "adapter.queue_depth()" in init_tick_body
    has_queuemanager = "QueueManager" in init_tick_body
    # Should NOT directly instantiate TicketStore
    has_direct_ticketstore = "TicketStore(" in init_tick_body
    # Should NOT call clear_ticket_store_cache directly
    has_direct_cache_clear = "clear_ticket_store_cache()" in init_tick_body

    assert has_adapter_queue_depth or has_queuemanager, (
        "_init_tick() should use adapter.queue_depth() or QueueManager for queue depth."
    )
    assert not has_direct_ticketstore, (
        "_init_tick() should not directly instantiate TicketStore."
    )
    assert not has_direct_cache_clear, (
        "_init_tick() should not call clear_ticket_store_cache() directly."
    )


def test_queuemanager_exists_and_provides_adapter_interface():
    """Verify QueueManager class exists and provides required methods."""
    from codebot.ticket_engine import QueueManager

    # Verify class exists
    assert QueueManager is not None, "QueueManager class not found"

    # Verify required methods exist
    required_methods = [
        "actionable_queue_depth",
        "ticket_classes",
        "summary",
        "clear_cache",
        "get_store",
    ]

    for method in required_methods:
        assert hasattr(QueueManager, method), f"QueueManager missing method: {method}"


def test_queuemanager_actionable_queue_depth():
    """Verify QueueManager.actionable_queue_depth() returns correct count."""
    import tempfile
    import json
    from pathlib import Path
    from codebot.ticket_engine import QueueManager, TicketStore, TicketState, TicketClass, Severity, RiskLevel, create_ticket

    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)
        tickets_path = state_dir / "tickets.json"

        # Create a minimal tickets.json
        tickets_data = {
            "schema_version": "2.0",
            "updated_at": 0.0,
            "tickets": [
                {
                    "id": "TEST-001",
                    "title": "Test bug",
                    "ticket_class": "bug",
                    "severity": "medium",
                    "state": "READY",
                    "source": "test",
                    "evidence": "test evidence",
                    "problem_statement": "test problem",
                    "desired_state": "test desired",
                    "acceptance_criteria": ["test passes"],
                    "affected_modules": [],
                    "dependencies": [],
                    "risk": "medium",
                    "blast_radius": "low",
                    "security_impact": "none",
                    "migration_impact": "none",
                    "required_reviewers": [],
                    "required_tests": [],
                    "documentation_requirements": [],
                    "rollback_strategy": "revert",
                    "estimated_cost_tokens": 0,
                    "created_at": 0.0,
                    "updated_at": 0.0,
                },
                {
                    "id": "TEST-002",
                    "title": "Test feature",
                    "ticket_class": "feature",
                    "severity": "low",
                    "state": "DECOMPOSE",
                    "source": "test",
                    "evidence": "test evidence",
                    "problem_statement": "test problem",
                    "desired_state": "test desired",
                    "acceptance_criteria": ["test passes"],
                    "affected_modules": [],
                    "dependencies": [],
                    "risk": "low",
                    "blast_radius": "low",
                    "security_impact": "none",
                    "migration_impact": "none",
                    "required_reviewers": [],
                    "required_tests": [],
                    "documentation_requirements": [],
                    "rollback_strategy": "revert",
                    "estimated_cost_tokens": 0,
                    "created_at": 0.0,
                    "updated_at": 0.0,
                },
                {
                    "id": "TEST-003",
                    "title": "Test rework",
                    "ticket_class": "refactor",
                    "severity": "medium",
                    "state": "REWORK",
                    "source": "test",
                    "evidence": "test evidence",
                    "problem_statement": "test problem",
                    "desired_state": "test desired",
                    "acceptance_criteria": ["test passes"],
                    "affected_modules": [],
                    "dependencies": [],
                    "risk": "medium",
                    "blast_radius": "low",
                    "security_impact": "none",
                    "migration_impact": "none",
                    "required_reviewers": [],
                    "required_tests": [],
                    "documentation_requirements": [],
                    "rollback_strategy": "revert",
                    "estimated_cost_tokens": 0,
                    "created_at": 0.0,
                    "updated_at": 0.0,
                },
                {
                    "id": "TEST-004",
                    "title": "Test implementing",
                    "ticket_class": "bug",
                    "severity": "high",
                    "state": "IMPLEMENTING",
                    "source": "test",
                    "evidence": "test evidence",
                    "problem_statement": "test problem",
                    "desired_state": "test desired",
                    "acceptance_criteria": ["test passes"],
                    "affected_modules": [],
                    "dependencies": [],
                    "risk": "high",
                    "blast_radius": "medium",
                    "security_impact": "none",
                    "migration_impact": "none",
                    "required_reviewers": [],
                    "required_tests": [],
                    "documentation_requirements": [],
                    "rollback_strategy": "revert",
                    "estimated_cost_tokens": 0,
                    "created_at": 0.0,
                    "updated_at": 0.0,
                },
            ],
        }

        tickets_path.write_text(json.dumps(tickets_data))

        qm = QueueManager.from_state_dir(state_dir)
        depth = qm.actionable_queue_depth()

        # Should count READY + DECOMPOSE + REWORK = 3 (not IMPLEMENTING)
        assert depth == 3, f"Expected actionable depth 3, got {depth}"


def test_queuemanager_ticket_classes():
    """Verify QueueManager.ticket_classes() returns classes for READY tickets."""
    import tempfile
    import json
    from pathlib import Path
    from codebot.ticket_engine import QueueManager

    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)
        tickets_path = state_dir / "tickets.json"

        tickets_data = {
            "schema_version": "2.0",
            "updated_at": 0.0,
            "tickets": [
                {
                    "id": "TEST-001",
                    "title": "Test bug",
                    "ticket_class": "bug",
                    "severity": "medium",
                    "state": "READY",
                    "source": "test",
                    "evidence": "test",
                    "problem_statement": "test",
                    "desired_state": "test",
                    "acceptance_criteria": ["test"],
                    "affected_modules": [],
                    "dependencies": [],
                    "risk": "medium",
                    "blast_radius": "low",
                    "security_impact": "none",
                    "migration_impact": "none",
                    "required_reviewers": [],
                    "required_tests": [],
                    "documentation_requirements": [],
                    "rollback_strategy": "revert",
                    "estimated_cost_tokens": 0,
                    "created_at": 0.0,
                    "updated_at": 0.0,
                },
                {
                    "id": "TEST-002",
                    "title": "Test feature",
                    "ticket_class": "feature",
                    "severity": "low",
                    "state": "READY",
                    "source": "test",
                    "evidence": "test",
                    "problem_statement": "test",
                    "desired_state": "test",
                    "acceptance_criteria": ["test"],
                    "affected_modules": [],
                    "dependencies": [],
                    "risk": "low",
                    "blast_radius": "low",
                    "security_impact": "none",
                    "migration_impact": "none",
                    "required_reviewers": [],
                    "required_tests": [],
                    "documentation_requirements": [],
                    "rollback_strategy": "revert",
                    "estimated_cost_tokens": 0,
                    "created_at": 0.0,
                    "updated_at": 0.0,
                },
                {
                    "id": "TEST-003",
                    "title": "Test not ready",
                    "ticket_class": "security",
                    "severity": "critical",
                    "state": "IMPLEMENTING",
                    "source": "test",
                    "evidence": "test",
                    "problem_statement": "test",
                    "desired_state": "test",
                    "acceptance_criteria": ["test"],
                    "affected_modules": [],
                    "dependencies": [],
                    "risk": "high",
                    "blast_radius": "medium",
                    "security_impact": "high",
                    "migration_impact": "none",
                    "required_reviewers": [],
                    "required_tests": [],
                    "documentation_requirements": [],
                    "rollback_strategy": "revert",
                    "estimated_cost_tokens": 0,
                    "created_at": 0.0,
                    "updated_at": 0.0,
                },
            ],
        }

        tickets_path.write_text(json.dumps(tickets_data))

        qm = QueueManager.from_state_dir(state_dir)
        classes = qm.ticket_classes()

        # Should only return classes for READY tickets (bug, feature), not IMPLEMENTING
        assert len(classes) == 2, f"Expected 2 ticket classes, got {len(classes)}"
        assert "bug" in classes, "Should include 'bug' class"
        assert "feature" in classes, "Should include 'feature' class"
