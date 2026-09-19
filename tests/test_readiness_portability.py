"""Regression test for CB-2971810-B3A4: readiness.py must import on all platforms.

Ensures no Unix-only modules (e.g., fcntl) are imported at module level
without platform guards, which would break Windows imports.
"""

import sys
from unittest.mock import patch


def test_readiness_imports_without_fcntl() -> None:
    """readiness.py must import successfully even when fcntl is unavailable."""
    # Remove cached module if present to force re-import
    mods_to_remove = [k for k in sys.modules if k == "codebot.readiness" or k.startswith("codebot.readiness.")]
    saved = {k: sys.modules.pop(k) for k in mods_to_remove}

    original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__  # type: ignore[attr-defined]

    def _mock_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "fcntl":
            raise ImportError("No module named 'fcntl' (simulated Windows)")
        return original_import(name, *args, **kwargs)

    try:
        with patch("builtins.__import__", side_effect=_mock_import):
            # This must NOT raise ImportError
            import codebot.readiness  # noqa: F401
    finally:
        # Restore cached modules
        sys.modules.update(saved)
