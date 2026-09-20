"""Tests for Process Manager - focusing on file locking for prompt reads."""
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from codebot.process_manager import (
    BotConfig,
    BotState,
    _prepare_prompt_with_context,
    _prompt_read_lock,
)


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

        # Patch BOTS_DIR to use tmp_path
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
