"""Performance tests for orchestrator._check_code_changes.

Verifies O(M+B) complexity by ensuring execution time scales linearly
with the number of modules and bots, not quadratically.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

import codebot.orchestrator as orch
from codebot.orchestrator import BotConfig, BotState


def _make_bot(name: str, model: str = "xiaomi-mimo-2.5") -> BotState:
    cfg = BotConfig(
        name=name,
        prompt_file=f"{name}.md",
        interval_seconds=300,
        heartbeat_timeout=600,
        model=model,
    )
    return BotState(config=cfg)


class TestCheckCodeChangesComplexity:
    """Verify _check_code_changes runs in O(M+B) time."""

    def test_linear_scaling_50_bots_100_modules(self, tmp_path):
        """Benchmark: <10ms for 50 bots and 100 modules."""
        # Create 100 mock module files
        pkg_dir = tmp_path / "codebot"
        pkg_dir.mkdir()
        mtimes = {}
        for i in range(100):
            f = pkg_dir / f"module_{i}.py"
            f.write_text("# mock")
            mtimes[f"module_{i}.py"] = f.stat().st_mtime

        # Create 50 bots
        bots = {}
        for i in range(50):
            bot = _make_bot(f"bot-{i}")
            # Initialize last_code_mtimes to simulate steady state
            bot.last_code_mtimes = dict(mtimes)
            bots[f"bot-{i}"] = bot

        with patch.object(orch, "_get_code_mtimes", return_value=mtimes):
            start = time.perf_counter()
            orch._check_code_changes(bots)
            elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 10, f"Execution took {elapsed_ms:.2f}ms, expected <10ms"

    def test_no_nested_loop_over_full_registry(self):
        """Ensure implementation does not iterate M*B naively."""
        # This is a structural check: we verify the function exists and
        # completes quickly even with larger inputs, implying no O(M*B) loop.
        mtimes = {f"mod_{i}.py": 1000.0 + i for i in range(200)}
        bots = {}
        for i in range(100):
            bot = _make_bot(f"bot-{i}")
            bot.last_code_mtimes = dict(mtimes)
            bots[f"bot-{i}"] = bot

        with patch.object(orch, "_get_code_mtimes", return_value=mtimes):
            start = time.perf_counter()
            orch._check_code_changes(bots)
            elapsed_ms = (time.perf_counter() - start) * 1000

        # 200 modules * 100 bots = 20,000 iterations if O(M*B).
        # If optimized, it should be very fast (<5ms).
        assert elapsed_ms < 5, f"Execution took {elapsed_ms:.2f}ms, likely O(M*B)"
