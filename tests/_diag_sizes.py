#!/usr/bin/env python3
"""Diagnostic: measure ScratchpadState serialized sizes."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codebot.scratchpad import ScratchpadState, MAX_SCRATCHPAD_BYTES

print(f"MAX_SCRATCHPAD_BYTES = {MAX_SCRATCHPAD_BYTES}")

# 1. Just 200 steps, no context
s1 = ScratchpadState(completed_steps=[f"step_{i}_with_long_desc" for i in range(200)])
print(f"200 steps, no ctx: {len(s1.to_json().encode('utf-8'))} bytes")

# 2. 50 steps + ctx 50k
s2 = ScratchpadState(completed_steps=[f"step_{i}_xxx" for i in range(50)], context_summary="x" * 50000)
print(f"50 steps + ctx50k: {len(s2.to_json().encode('utf-8'))} bytes")

# 3. 50 steps + ctx 100 (after halving would get here)
s3 = ScratchpadState(completed_steps=[f"step_{i}_xxx" for i in range(50)], context_summary="x" * 100)
print(f"50 steps + ctx100: {len(s3.to_json().encode('utf-8'))} bytes")

# 4. 500 steps, no context
s4 = ScratchpadState(completed_steps=[f"step_{i}_xxx" for i in range(500)])
print(f"500 steps, no ctx: {len(s4.to_json().encode('utf-8'))} bytes")

# 5. 50 steps, no context
s5 = ScratchpadState(completed_steps=[f"step_{i}_xxx" for i in range(50)])
print(f"50 steps, no ctx: {len(s5.to_json().encode('utf-8'))} bytes")

# 6. 100 steps, no context
s6 = ScratchpadState(completed_steps=[f"step_{i}_xxx" for i in range(100)])
print(f"100 steps, no ctx: {len(s6.to_json().encode('utf-8'))} bytes")
