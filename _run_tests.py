#!/usr/bin/env python3
"""Run tests for test_orchestrator.py"""
import subprocess, sys
result = subprocess.run(
    [sys.executable, '-m', 'pytest', 'tests/test_orchestrator.py', '-q', '--tb=short', '--no-header'],
    capture_output=True, text=True, timeout=120
)
print(result.stdout[-5000:] if len(result.stdout) > 5000 else result.stdout)
if result.stderr:
    print("STDERR:", result.stderr[-2000:])
print("RC:", result.returncode)
