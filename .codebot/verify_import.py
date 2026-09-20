"""Verify acceptance criteria for CB-6947264-A7B0."""
import sys
sys.path.insert(0, '/home/kozuka/Work/CodeBot')

# Check 1: file_lock.py imports from locks.py
import codebot.locks as L
import codebot.file_lock as F

assert F.flock is L.flock, f"flock identity mismatch: {F.flock} is not {L.flock}"
assert F.LOCK_SH == L.LOCK_SH, f"LOCK_SH mismatch: {F.LOCK_SH} != {L.LOCK_SH}"
assert F.LOCK_EX == L.LOCK_EX, f"LOCK_EX mismatch: {F.LOCK_EX} != {L.LOCK_EX}"
assert F.LOCK_UN == L.LOCK_UN, f"LOCK_UN mismatch: {F.LOCK_UN} != {L.LOCK_UN}"
assert F.LOCK_NB == L.LOCK_NB, f"LOCK_NB mismatch: {F.LOCK_NB} != {L.LOCK_NB}"
print("PASS: file_lock re-exports from locks correctly (identity check)")

# Check 2: No circular imports (already proven by successful import above)
print("PASS: no circular imports")

# Check 3: Caller modules import fine
import codebot.lease_state
import codebot.token_budget
import codebot.ticket_engine
import codebot.api_runner
print("PASS: all caller modules import without error")

print("\nAll acceptance criteria verified!")
