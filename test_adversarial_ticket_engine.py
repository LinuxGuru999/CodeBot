#!/usr/bin/env python3
"""Adversarial challenge tests for ticket_engine.py (CB-3227216-42CA)"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from codebot.ticket_engine import (
    TicketStore, Ticket, TicketState, TicketClass, Severity, RiskLevel,
    create_ticket, generate_ticket_id
)

def test_invalid_transition():
    """Attack: Attempt invalid state transition DISCOVERED -> COMPLETE"""
    t = create_ticket(
        title="Test",
        ticket_class=TicketClass.BUG,
        severity=Severity.LOW,
        source="test",
        evidence="evidence",
        problem_statement="problem",
        desired_state="desired",
        acceptance_criteria=["ac1"]
    )
    try:
        t.transition(TicketState.COMPLETE)
        return False, "Invalid transition allowed"
    except ValueError as e:
        if "invalid transition" in str(e):
            return True, "Invalid transition correctly rejected"
        return False, f"Wrong error: {e}"

def test_empty_acceptance_criteria():
    """Attack: Create ticket with empty acceptance criteria"""
    try:
        create_ticket(
            title="Test",
            ticket_class=TicketClass.BUG,
            severity=Severity.LOW,
            source="test",
            evidence="evidence",
            problem_statement="problem",
            desired_state="desired",
            acceptance_criteria=[]
        )
        return False, "Empty acceptance criteria allowed"
    except ValueError as e:
        if "acceptance criterion" in str(e):
            return True, "Empty acceptance criteria correctly rejected"
        return False, f"Wrong error: {e}"

def test_duplicate_evidence():
    """Attack: Create two tickets with identical evidence hash"""
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = Path(tmpdir) / "tickets.json"
        store = TicketStore(store_path)
        
        t1 = create_ticket(
            title="Test 1",
            ticket_class=TicketClass.BUG,
            severity=Severity.LOW,
            source="test",
            evidence="same evidence",
            problem_statement="same problem",
            desired_state="desired",
            acceptance_criteria=["ac1"]
        )
        store.add(t1)
        
        t2 = create_ticket(
            title="Test 2",
            ticket_class=TicketClass.BUG,
            severity=Severity.LOW,
            source="test",
            evidence="same evidence",
            problem_statement="same problem",
            desired_state="desired",
            acceptance_criteria=["ac1"]
        )
        try:
            store.add(t2)
            return False, "Duplicate evidence allowed"
        except ValueError as e:
            if "duplicate" in str(e).lower():
                return True, "Duplicate evidence correctly rejected"
            return False, f"Wrong error: {e}"

def test_gatekeeper_bypass():
    """Attack: Transition VERIFYING -> COMPLETE without gate approval"""
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = Path(tmpdir) / "tickets.json"
        store = TicketStore(store_path)
        
        t = create_ticket(
            title="Test",
            ticket_class=TicketClass.BUG,
            severity=Severity.LOW,
            source="test",
            evidence="evidence",
            problem_statement="problem",
            desired_state="desired",
            acceptance_criteria=["ac1"]
        )
        store.add(t)
        
        # Move to VERIFYING
        for state in [TicketState.VALIDATING, TicketState.TRIAGED, TicketState.READY, 
                      TicketState.PLANNING, TicketState.IMPLEMENTING, TicketState.REVIEWING, 
                      TicketState.VERIFYING]:
            t = store.transition(t.id, state)
            
        try:
            store.transition(t.id, TicketState.COMPLETE)
            return False, "Gatekeeper bypass allowed"
        except ValueError as e:
            if "gatekeeper" in str(e).lower():
                return True, "Gatekeeper bypass correctly blocked"
            return False, f"Wrong error: {e}"

def test_planning_bypass():
    """Attack: Transition READY -> IMPLEMENTING without plan for high risk"""
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = Path(tmpdir) / "tickets.json"
        store = TicketStore(store_path)
        
        t = create_ticket(
            title="Test",
            ticket_class=TicketClass.SECURITY,
            severity=Severity.HIGH,
            risk=RiskLevel.CRITICAL,
            source="test",
            evidence="evidence",
            problem_statement="problem",
            desired_state="desired",
            acceptance_criteria=["ac1"]
        )
        store.add(t)
        
        # Move to READY
        for state in [TicketState.VALIDATING, TicketState.TRIAGED, TicketState.READY]:
            t = store.transition(t.id, state)
            
        try:
            store.transition(t.id, TicketState.IMPLEMENTING)
            return False, "Planning bypass allowed"
        except ValueError as e:
            if "plan" in str(e).lower():
                return True, "Planning bypass correctly blocked"
            return False, f"Wrong error: {e}"

def test_concurrent_access():
    """Attack: Concurrent add/transition to corrupt state indices"""
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = Path(tmpdir) / "tickets.json"
        store = TicketStore(store_path)
        
        errors = []
        
        def add_tickets(start_idx, count):
            try:
                for i in range(start_idx, start_idx + count):
                    t = create_ticket(
                        title=f"Concurrent {i}",
                        ticket_class=TicketClass.BUG,
                        severity=Severity.LOW,
                        source="test",
                        evidence=f"evidence {i}",
                        problem_statement=f"problem {i}",
                        desired_state="desired",
                        acceptance_criteria=["ac1"]
                    )
                    store.add(t)
            except Exception as e:
                errors.append(str(e))
                
        threads = [threading.Thread(target=add_tickets, args=(i*10, 10)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
            
        if errors:
            return False, f"Concurrent access errors: {errors}"
            
        # Verify consistency
        with store._lock:
            total_tickets = len(store._tickets)
            total_in_state_index = sum(len(ids) for ids in store._state_index.values())
            
        if total_tickets != total_in_state_index:
            return False, f"State index inconsistency: {total_tickets} tickets vs {total_in_state_index} in index"
            
        return True, "Concurrent access handled correctly"

def test_atomic_write_crash_recovery():
    """Attack: Verify atomic write protects against corruption"""
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = Path(tmpdir) / "tickets.json"
        store = TicketStore(store_path)
        
        t = create_ticket(
            title="Test",
            ticket_class=TicketClass.BUG,
            severity=Severity.LOW,
            source="test",
            evidence="evidence",
            problem_statement="problem",
            desired_state="desired",
            acceptance_criteria=["ac1"]
        )
        store.add(t)
        store.flush()
        
        # Verify file exists and is valid JSON
        if not store_path.exists():
            return False, "Ticket file not created"
            
        try:
            data = json.loads(store_path.read_text())
            if "tickets" not in data:
                return False, "Invalid JSON structure"
        except json.JSONDecodeError:
            return False, "Corrupted JSON file"
            
        return True, "Atomic write produced valid file"

def run_tests():
    tests = [
        ("Invalid Transition", test_invalid_transition),
        ("Empty Acceptance Criteria", test_empty_acceptance_criteria),
        ("Duplicate Evidence", test_duplicate_evidence),
        ("Gatekeeper Bypass", test_gatekeeper_bypass),
        ("Planning Bypass", test_planning_bypass),
        ("Concurrent Access", test_concurrent_access),
        ("Atomic Write", test_atomic_write_crash_recovery),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            passed, msg = test_func()
            results.append((name, passed, msg))
            print(f"{'PASS' if passed else 'FAIL'}: {name} - {msg}")
        except Exception as e:
            results.append((name, False, f"Exception: {e}"))
            print(f"FAIL: {name} - Exception: {e}")
            
    return results

if __name__ == "__main__":
    results = run_tests()
    failed = [r for r in results if not r[1]]
    if failed:
        print(f"\n{len(failed)} tests FAILED")
        sys.exit(1)
    else:
        print(f"\nAll {len(results)} tests PASSED")
        sys.exit(0)
