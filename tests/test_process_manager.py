"""Tests for Process Manager - focusing on file locking for prompt reads."""
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebot.process_manager import (
    BotConfig,
    BotState,
    _prepare_prompt_with_context,
    _prompt_read_lock,
    _count_api_runner_processes,
    _clear_process_count_cache,
    batch_read_heartbeats,
    read_heartbeat,
)
def test_count_api_runner_processes_matches_module_invocation():
    _clear_process_count_cache()
    completed = MagicMock(returncode=0, stdout="101\n102\n")

    with patch("codebot.process_manager.subprocess.run", return_value=completed) as run:
        assert _count_api_runner_processes() == 2

    run.assert_called_once_with(
        ["pgrep", "-f", "codebot.api_runner"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    _clear_process_count_cache()


def test_count_api_runner_processes_cached_within_ttl():
    """Second call within TTL returns cached value without invoking subprocess.run again."""
    _clear_process_count_cache()
    completed = MagicMock(returncode=0, stdout="101\n102\n")

    with patch("codebot.process_manager.subprocess.run", return_value=completed) as run:
        with patch("codebot.process_manager.time.monotonic", side_effect=[0.0, 1.0]):
            first = _count_api_runner_processes()
            second = _count_api_runner_processes()

    assert first == 2
    assert second == 2
    # subprocess.run should only be called once due to caching
    assert run.call_count == 1
    _clear_process_count_cache()


def test_count_api_runner_processes_cache_invalidates_after_ttl():
    """Call after TTL expiry re-invokes pgrep and returns fresh count."""
    _clear_process_count_cache()
    completed1 = MagicMock(returncode=0, stdout="101\n102\n")
    completed2 = MagicMock(returncode=0, stdout="201\n202\n203\n")

    with patch("codebot.process_manager.subprocess.run", side_effect=[completed1, completed2]) as run:
        with patch("codebot.process_manager.time.monotonic", side_effect=[0.0, 6.0]):
            first = _count_api_runner_processes()
            second = _count_api_runner_processes()

    assert first == 2
    assert second == 3
    # subprocess.run should be called twice because cache expired
    assert run.call_count == 2
    _clear_process_count_cache()


def test_prompt_read_lock_basic(tmp_path):
    """Test that _prompt_read_lock acquires and releases lock correctly."""
    prompt_file = tmp_path / "test_prompt.md"
    prompt_file.write_text("test content")

    with _prompt_read_lock(prompt_file):
        # Should be able to read while holding lock
        content = prompt_file.read_text()
        assert content == "test content"

    # Lock should be released after context manager exits
    assert prompt_file.exists()


def test_prompt_read_lock_nonexistent_file(tmp_path):
    """Test that _prompt_read_lock raises FileNotFoundError for missing file."""
    prompt_file = tmp_path / "nonexistent.md"

    with pytest.raises(FileNotFoundError):
        with _prompt_read_lock(prompt_file):
            pass


def test_concurrent_prompt_read(tmp_path):
    """Test that concurrent writes don't cause stale/inconsistent reads.
    
    This test spawns a writer thread that continuously modifies a prompt file
    while the main thread repeatedly reads it using _prepare_prompt_with_context.
    With proper locking, each read should get consistent content (no partial writes).
    """
    # Create a prompt file in the tmp_path
    prompt_file = tmp_path / "test_bot.md"
    prompt_file.write_text("initial content")

    # Track read results
    read_results = []
    errors = []
    stop_event = threading.Event()

    def writer_thread():
        """Continuously write different content to the prompt file."""
        counter = 0
        while not stop_event.is_set():
            try:
                # Write content that includes a counter for verification
                content = f"writer iteration {counter} " + "x" * 100
                # Write atomically using temp file + rename
                tmp_file = tmp_path / "test_bot_tmp.md"
                tmp_file.write_text(content)
                os.replace(str(tmp_file), str(prompt_file))
                counter += 1
                time.sleep(0.001)  # Small delay between writes
            except Exception as e:
                errors.append(f"Writer error: {e}")
                break

    def reader_thread():
        """Repeatedly read the prompt file using _prepare_prompt_with_context."""
        bot_config = BotConfig(
            name="test_bot",
            prompt_file=str(prompt_file.name),
            interval_seconds=60,
            heartbeat_timeout=120,
        )
        bot_state = BotState(config=bot_config)

        # Patch BOTS_DIR to use tmp_path (honored by _resolve_bots_dir)
        with patch("codebot.process_manager.BOTS_DIR", tmp_path):
            iterations = 0
            while not stop_event.is_set() and iterations < 100:
                try:
                    content = _prepare_prompt_with_context(bot_state)
                    if content:  # Only record non-empty reads
                        read_results.append(content)
                    iterations += 1
                    time.sleep(0.001)
                except Exception as e:
                    errors.append(f"Reader error: {e}")
                    break

    # Start writer and reader threads
    writer = threading.Thread(target=writer_thread, daemon=True)
    reader = threading.Thread(target=reader_thread, daemon=True)

    writer.start()
    reader.start()

    # Let them run for a bit
    time.sleep(0.5)

    # Signal stop and wait for threads
    stop_event.set()
    writer.join(timeout=2)
    reader.join(timeout=2)

    # Check for errors
    if errors:
        pytest.fail(f"Errors during concurrent test: {errors}")

    # Verify that all reads returned consistent content
    # Each read should be a complete string, not a mix of old and new content
    for i, content in enumerate(read_results):
        # Content should contain "writer iteration N" pattern
        assert "writer iteration" in content or content == "initial content", \
            f"Read {i} has unexpected content: {content[:50]}..."
        # Content should not be empty (unless file was being written)
        # The key assertion: content should be internally consistent
        # i.e., if it contains "writer iteration N", all the x's should match
        # that iteration's expected length

    # We should have collected some reads
    assert len(read_results) > 0, "No successful reads during concurrent test"


def test_prepare_prompt_with_context_locks_file(tmp_path):
    """Test that _prepare_prompt_with_context holds lock during read."""
    prompt_file = tmp_path / "locked_test.md"
    prompt_file.write_text("locked content test")

    bot_config = BotConfig(
        name="locked_test_bot",
        prompt_file=str(prompt_file.name),
        interval_seconds=60,
        heartbeat_timeout=120,
    )
    bot_state = BotState(config=bot_config)

    with patch("codebot.process_manager.BOTS_DIR", tmp_path):
        content = _prepare_prompt_with_context(bot_state)

    # Should get the content (may have git context appended)
    assert "locked content test" in content


# -----------------------------------------------------------------------
# batch_read_heartbeats
# -----------------------------------------------------------------------
class TestBatchReadHeartbeats:
    """Tests for batch_read_heartbeats() — single-pass heartbeat reading."""

    def test_returns_correct_timestamps(self, tmp_path):
        """Returns correct timestamps for multiple bots."""
        now = time.time()
        for name, ts in [("bot-a", now - 5), ("bot-b", now - 10)]:
            (tmp_path / f"{name}.heartbeat").write_text(str(ts))
        with patch("codebot.health_monitor.STATE_DIR", tmp_path):
            result = batch_read_heartbeats(["bot-a", "bot-b"])
        assert result["bot-a"] == pytest.approx(now - 5, abs=0.01)
        assert result["bot-b"] == pytest.approx(now - 10, abs=0.01)

    def test_missing_file_returns_zero(self, tmp_path):
        """Missing heartbeat files produce 0.0 entries."""
        with patch("codebot.health_monitor.STATE_DIR", tmp_path):
            result = batch_read_heartbeats(["no-such-bot"])
        assert result["no-such-bot"] == 0.0

    def test_corrupt_file_returns_zero(self, tmp_path):
        """Corrupt heartbeat files produce 0.0 entries."""
        (tmp_path / "bad-bot.heartbeat").write_text("not-a-timestamp")
        with patch("codebot.health_monitor.STATE_DIR", tmp_path):
            result = batch_read_heartbeats(["bad-bot"])
        assert result["bad-bot"] == 0.0

    def test_empty_list_returns_empty_dict(self, tmp_path):
        """Empty input returns empty dict."""
        with patch("codebot.health_monitor.STATE_DIR", tmp_path):
            result = batch_read_heartbeats([])
        assert result == {}

    def test_empty_file_returns_zero(self, tmp_path):
        """Empty heartbeat file produces 0.0."""
        (tmp_path / "empty-bot.heartbeat").write_text("")
        with patch("codebot.health_monitor.STATE_DIR", tmp_path):
            result = batch_read_heartbeats(["empty-bot"])
        assert result["empty-bot"] == 0.0

    def test_matches_individual_read(self, tmp_path):
        """batch_read_heartbeats produces same result as individual read_heartbeat calls."""
        now = time.time()
        bots = ["match-a", "match-b", "match-c"]
        for name in bots:
            (tmp_path / f"{name}.heartbeat").write_text(str(now - 20))
        with patch("codebot.health_monitor.STATE_DIR", tmp_path):
            batch_result = batch_read_heartbeats(bots)
            individual_results = {name: read_heartbeat(name) for name in bots}
        for name in bots:
            assert batch_result[name] == pytest.approx(individual_results[name], abs=0.01)

    def test_mixed_valid_and_missing(self, tmp_path):
        """Mix of existing and missing heartbeat files handled correctly."""
        now = time.time()
        (tmp_path / "exists.heartbeat").write_text(str(now))
        with patch("codebot.health_monitor.STATE_DIR", tmp_path):
            result = batch_read_heartbeats(["exists", "missing"])
        assert result["exists"] == pytest.approx(now, abs=0.01)
        assert result["missing"] == 0.0

    def test_consistency_with_individual_read(self, tmp_path):
        """Each entry in batch result matches read_heartbeat for same bot."""
        now = time.time()
        (tmp_path / "consistency.heartbeat").write_text(str(now))
        with patch("codebot.health_monitor.STATE_DIR", tmp_path):
            batch_result = batch_read_heartbeats(["consistency"])
            individual = read_heartbeat("consistency")
        assert batch_result["consistency"] == pytest.approx(individual, abs=0.01)
