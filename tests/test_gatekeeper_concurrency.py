"""Concurrency tests for gatekeeper._log_decision atomicity."""
import json
import os
import threading
import time
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from codebot.gatekeeper import Gatekeeper


def test_log_decision_concurrent_atomicity(tmp_path):
    """Verify that concurrent calls to _log_decision produce exactly N valid JSON lines.

    This test spawns multiple threads, each calling _log_decision with a unique payload.
    After all threads complete, it reads the log file and asserts:
    1. Exactly N lines exist.
    2. Every line is valid JSON.
    3. Every expected payload is present exactly once.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    gk = Gatekeeper(state_dir)

    num_threads = 10
    results = []
    errors = []

    def worker(i):
        try:
            result = {
                "ticket_id": f"CB-CONC-{i}",
                "decision": "COMPLETE",
                "timestamp": time.time(),
                "thread_id": i,
            }
            gk._log_decision(result)
            results.append(result)
        except Exception as e:
            errors.append(e)

    threads = []
    for i in range(num_threads):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    assert not errors, f"Errors occurred in worker threads: {errors}"
    assert len(results) == num_threads, f"Expected {num_threads} results, got {len(results)}"

    # Read and verify log file
    log_path = state_dir / "gate_results.jsonl"
    assert log_path.exists(), "Log file was not created"

    content = log_path.read_text(encoding="utf-8")
    lines = [line.strip() for line in content.splitlines() if line.strip()]

    assert len(lines) == num_threads, f"Expected {num_threads} lines in log, got {len(lines)}"

    found_ids = set()
    for line in lines:
        try:
            entry = json.loads(line)
            tid = entry.get("ticket_id")
            assert tid is not None, "Missing ticket_id in log entry"
            assert tid not in found_ids, f"Duplicate ticket_id found: {tid}"
            found_ids.add(tid)
        except json.JSONDecodeError as e:
            pytest.fail(f"Invalid JSON in log line: {line!r}. Error: {e}")

    expected_ids = {f"CB-CONC-{i}" for i in range(num_threads)}
    assert found_ids == expected_ids, f"Missing or extra IDs. Expected: {expected_ids}, Found: {found_ids}"
