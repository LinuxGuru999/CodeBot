"""Tests for drain status text labels in botop.py — ticket CB-5100583-4313.

Acceptance criteria:
- Drain status readable with NO_COLOR=1
- Distinct text markers for active/clear states ([DRAIN] vs [OK])
- Existing functionality preserved
"""

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOTOP = [sys.executable, "-m", "codebot.botop"]


def _run_botop(args: list[str], env_override: dict | None = None) -> subprocess.CompletedProcess:
    """Run botop with optional environment overrides."""
    import os
    env = os.environ.copy()
    if env_override:
        env.update(env_override)
    return subprocess.run(
        [*BOTOP, *args],
        capture_output=True,
        timeout=10,
        cwd=str(PROJECT_ROOT),
        env=env,
    )


class TestDrainLabelsNoColor:
    """Verify drain status is readable without color (NO_COLOR=1)."""

    def test_status_clear_drain_label_no_color(self):
        """With NO_COLOR=1 and no drain active, status shows [OK] marker."""
        r = _run_botop(["status"], env_override={"NO_COLOR": "1"})
        assert r.returncode == 0, f"botop status failed: {r.stderr[:300]}"
        out = r.stdout.decode(errors="ignore") + r.stderr.decode(errors="ignore")
        # When no drain is active, should show [OK] marker
        assert "[OK]" in out, (
            f"drain clear status missing [OK] marker with NO_COLOR=1:\n{out[:500]}"
        )

    def test_live_once_drain_label_no_color(self):
        """With NO_COLOR=1, live --once shows drain markers as plain text."""
        r = _run_botop(["live", "--once"], env_override={"NO_COLOR": "1"})
        assert r.returncode == 0, f"botop live --once failed: {r.stderr[:300]}"
        out = r.stdout.decode(errors="ignore")
        # Should contain one of the drain markers as plain text
        has_marker = "[DRAIN]" in out or "[OK]" in out
        assert has_marker, (
            f"live output missing drain text markers with NO_COLOR=1:\n{out[:500]}"
        )

    def test_health_drain_label_no_color(self):
        """With NO_COLOR=1, health shows drain markers as plain text."""
        r = _run_botop(["health"], env_override={"NO_COLOR": "1"})
        assert r.returncode == 0, f"botop health failed: {r.stderr[:300]}"
        out = r.stdout.decode(errors="ignore")
        has_marker = "[DRAIN]" in out or "[OK]" in out
        assert has_marker, (
            f"health output missing drain text markers with NO_COLOR=1:\n{out[:500]}"
        )


class TestDrainLabelsWithColor:
    """Verify drain status text markers exist even with color enabled."""

    def test_status_has_drain_ok_markers(self):
        """Status output includes [DRAIN] or [OK] text markers."""
        r = _run_botop(["status"])
        assert r.returncode == 0, f"botop status failed: {r.stderr[:300]}"
        out = r.stdout.decode(errors="ignore")
        has_marker = "[DRAIN]" in out or "[OK]" in out
        assert has_marker, (
            f"status output missing drain text markers:\n{out[:500]}"
        )

    def test_live_once_has_drain_markers(self):
        """Live --once output includes [DRAIN] or [OK] text markers."""
        r = _run_botop(["live", "--once"])
        assert r.returncode == 0, f"botop live --once failed: {r.stderr[:300]}"
        out = r.stdout.decode(errors="ignore")
        has_marker = "[DRAIN]" in out or "[OK]" in out
        assert has_marker, (
            f"live output missing drain text markers:\n{out[:500]}"
        )

    def test_health_has_drain_markers(self):
        """Health output includes [DRAIN] or [OK] text markers."""
        r = _run_botop(["health"])
        assert r.returncode == 0, f"botop health failed: {r.stderr[:300]}"
        out = r.stdout.decode(errors="ignore")
        has_marker = "[DRAIN]" in out or "[OK]" in out
        assert has_marker, (
            f"health output missing drain text markers:\n{out[:500]}"
        )


class TestDrainLabelsUnit:
    """Unit tests: verify _c() helper preserves text markers."""

    def test_c_returns_text_when_color_disabled(self):
        """_c() with enabled=False returns text unchanged."""
        from codebot.botop import _c
        result = _c("[DRAIN] ACTIVE", "red", False)
        assert result == "[DRAIN] ACTIVE"

    def test_c_returns_text_when_color_enabled(self):
        """_c() with enabled=True wraps text in ANSI but preserves content."""
        from codebot.botop import _c
        result = _c("[OK] clear", "green", True)
        assert "[OK] clear" in result
        # Should contain ANSI codes when enabled
        assert "\033[" in result

    def test_c_ok_marker_preserved(self):
        """[OK] marker is preserved in both color modes."""
        from codebot.botop import _c
        off = _c("[OK] clear", "green", False)
        on = _c("[OK] clear", "green", True)
        assert off == "[OK] clear"
        assert "[OK]" in on

    def test_c_drain_marker_preserved(self):
        """[DRAIN] marker is preserved in both color modes."""
        from codebot.botop import _c
        off = _c("[DRAIN] ACTIVE", "red", False)
        on = _c("[DRAIN] ACTIVE", "red", True)
        assert off == "[DRAIN] ACTIVE"
        assert "[DRAIN]" in on
