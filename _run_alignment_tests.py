#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import pytest
    sys.exit(pytest.main(['tests/test_alignment_service.py', '-v', '--tb=short']))
except ImportError:
    print("pytest not available")
    sys.exit(1)
