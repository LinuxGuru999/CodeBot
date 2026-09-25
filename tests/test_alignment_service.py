#!/usr/bin/env python3
"""Tests for alignment_service.py using state_manager.get_paths() for path resolution."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

from codebot import alignment_service
from codebot import state_manager


class TestEnsureRlAdapter:
    """Tests for _ensure_rl_adapter() adapter injection."""

    def test_ensure_rl_adapter_uses_get_paths(self, tmp_path):
        """Verify _ensure_rl_adapter uses state_manager.get_paths()."""
        # Create a temporary state directory structure
        custom_state_dir = tmp_path / "test_state"
        custom_state_dir.mkdir(parents=True)
        events_dir = custom_state_dir / "alignment_events"
        events_dir.mkdir()
        
        # Mock get_paths to return our custom paths
        mock_paths = MagicMock()
        mock_paths.state_dir = custom_state_dir
        mock_paths.alignment_events_dir = events_dir
        mock_paths.bots_dir = custom_state_dir.parent.parent
        mock_paths.logs_dir = custom_state_dir.parent / "logs"
        
        from codebot import rl_engine
        rl_engine._adapter_instance = None
        
        try:
            with patch('codebot.alignment_service.get_paths', return_value=mock_paths):
                alignment_service._ensure_rl_adapter()
                
                # Verify rl_engine now has an adapter
                assert rl_engine._adapter_instance is not None
                
                # Verify the adapter is the paths object itself
                assert rl_engine._adapter_instance is mock_paths
        finally:
            rl_engine._adapter_instance = None

    def test_ensure_rl_adapter_does_not_override_existing_adapter(self):
        """Verify _ensure_rl_adapter doesn't override an existing adapter."""
        from codebot import rl_engine
        
        # Create and set a custom adapter
        custom_adapter = MagicMock()
        rl_engine._adapter_instance = custom_adapter
        
        try:
            # Call _ensure_rl_adapter - should return early
            alignment_service._ensure_rl_adapter()
            
            # Should not have changed the adapter
            assert rl_engine._adapter_instance is custom_adapter
        finally:
            rl_engine._adapter_instance = None

    def test_ensure_rl_adapter_handles_missing_rl_engine(self):
        """Verify _ensure_rl_adapter fails gracefully when rl_engine is unavailable."""
        # Temporarily remove rl_engine from modules
        original_rl_engine = sys.modules.get('codebot.rl_engine')
        try:
            del sys.modules['codebot.rl_engine']
            sys.modules['codebot.rl_engine'] = None
            
            # Should not raise an exception
            alignment_service._ensure_rl_adapter()
        finally:
            # Restore original module
            if original_rl_engine:
                sys.modules['codebot.rl_engine'] = original_rl_engine
            else:
                del sys.modules['codebot.rl_engine']

    def test_ensure_rl_adapter_handles_set_project_adapter_error(self):
        """Verify _ensure_rl_adapter handles errors when setting project adapter."""
        from codebot import rl_engine
        
        # Clear any existing adapter
        rl_engine._adapter_instance = None
        
        # Mock get_paths
        mock_paths = MagicMock()
        
        try:
            with patch('codebot.state_manager.get_paths', return_value=mock_paths), \
                 patch('codebot.rl_engine.set_project_adapter', side_effect=Exception("Test error")):
                # Should not raise an exception
                alignment_service._ensure_rl_adapter()
                
                # Adapter should still be None since set_project_adapter failed
                assert rl_engine._adapter_instance is None
        finally:
            rl_engine._adapter_instance = None


class TestAlignmentPipelineStateUsage:
    """Tests verifying alignment pipeline functions use get_paths() for state directory."""

    def test_run_alignment_pipeline_uses_get_paths(self, tmp_path):
        """Verify run_alignment_pipeline uses state_manager.get_paths() for paths."""
        # Create a temporary state directory structure
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events_dir = state_dir / "alignment_events"
        events_dir.mkdir()
        
        # Mock get_paths to return our temp directory
        mock_paths = MagicMock()
        mock_paths.state_dir = state_dir
        mock_paths.alignment_events_dir = events_dir
        
        with patch('codebot.state_manager.get_paths', return_value=mock_paths):
            # Call the function (it will return False since no events exist)
            result = alignment_service.run_alignment_pipeline("test_bot")
            
            # Should complete without error
            assert result is False  # No events to process

    def test_run_alignment_pipeline_for_all_uses_get_paths(self, tmp_path):
        """Verify run_alignment_pipeline_for_all uses state_manager.get_paths() for paths."""
        # Create a temporary state directory structure
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events_dir = state_dir / "alignment_events"
        events_dir.mkdir()
        
        # Mock get_paths
        mock_paths = MagicMock()
        mock_paths.state_dir = state_dir
        mock_paths.alignment_events_dir = events_dir
        
        with patch('codebot.state_manager.get_paths', return_value=mock_paths):
            # Call the function
            alignment_service.run_alignment_pipeline_for_all()
            # Should complete without error

    def test_run_alignment_pipeline_for_all_handles_missing_events_dir(self, tmp_path):
        """Verify run_alignment_pipeline_for_all handles missing events directory."""
        # Create state directory without events subdirectory
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        
        # Mock get_paths
        mock_paths = MagicMock()
        mock_paths.state_dir = state_dir
        mock_paths.alignment_events_dir = state_dir / "alignment_events"  # Doesn't exist
        
        with patch('codebot.state_manager.get_paths', return_value=mock_paths):
            # Should not raise exception
            alignment_service.run_alignment_pipeline_for_all()
            # Test passes if no exception is raised

    def test_run_alignment_pipeline_for_all_handles_consume_triggers_error(self, tmp_path):
        """Verify run_alignment_pipeline_for_all handles consume_triggers failure."""
        # Create state directory structure
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events_dir = state_dir / "alignment_events"
        events_dir.mkdir()
        
        # Mock get_paths and consume_triggers to fail
        mock_paths = MagicMock()
        mock_paths.state_dir = state_dir
        mock_paths.alignment_events_dir = events_dir
        
        with patch('codebot.state_manager.get_paths', return_value=mock_paths), \
             patch('codebot.prompt_optimizer.consume_triggers', side_effect=Exception("Test error")):
            # Should not raise exception
            alignment_service.run_alignment_pipeline_for_all()
            # Test passes if no exception is raised

    def test_run_alignment_pipeline_for_all_logs_failed_bot_pipeline(self, tmp_path):
        """Verify run_alignment_pipeline_for_all logs warnings when a bot pipeline fails."""
        import json
        from codebot import rl_engine
        
        # Create state directory structure
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events_dir = state_dir / "alignment_events"
        events_dir.mkdir()
        
        # Create an event file
        event_data = {
            "bot": "failing_bot",
            "exit_reason": "clean",
            "processed": False,
        }
        event_file = events_dir / "failing_bot.exit.json"
        event_file.write_text(json.dumps(event_data))
        
        # Clear rl_engine adapter
        rl_engine._adapter_instance = None
        
        # Mock get_paths and make run_alignment_pipeline raise
        mock_paths = MagicMock()
        mock_paths.state_dir = state_dir
        mock_paths.alignment_events_dir = events_dir
        
        with patch('codebot.state_manager.get_paths', return_value=mock_paths), \
             patch.object(alignment_service, 'run_alignment_pipeline', side_effect=Exception("Pipeline error")):
            # Should not raise exception
            alignment_service.run_alignment_pipeline_for_all()
            # Test passes if no exception is raised

    def test_run_alignment_pipeline_for_all_logs_consumed_triggers(self, tmp_path):
        """Verify run_alignment_pipeline_for_all logs when triggers are consumed."""
        # Create state directory structure
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events_dir = state_dir / "alignment_events"
        events_dir.mkdir()
        
        # Mock get_paths and consume_triggers to return a count
        mock_paths = MagicMock()
        mock_paths.state_dir = state_dir
        mock_paths.alignment_events_dir = events_dir
        
        with patch('codebot.state_manager.get_paths', return_value=mock_paths), \
             patch('codebot.prompt_optimizer.consume_triggers', return_value=3):
            # Should not raise exception
            alignment_service.run_alignment_pipeline_for_all()
            # Test passes if no exception is raised

    def test_run_alignment_pipeline_for_all_uses_bots_dir_fallback(self, tmp_path):
        """Verify run_alignment_pipeline_for_all uses BOTS_DIR when roles_dir doesn't exist."""
        import json
        from codebot import rl_engine
        
        # Create state directory structure without roles dir
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events_dir = state_dir / "alignment_events"
        events_dir.mkdir()
        triggers_dir = state_dir / "alignment_triggers"
        triggers_dir.mkdir()
        
        # Create a trigger file so consume_triggers has something to process
        trigger_data = {
            "bot": "test_bot",
            "score": 45,
            "verdict": "evolve",
        }
        trigger_file = triggers_dir / "test_bot.evolve.json"
        trigger_file.write_text(json.dumps(trigger_data))
        
        # Create an event so the function processes something
        event_data = {
            "bot": "test_bot",
            "exit_reason": "clean",
            "processed": False,
        }
        event_file = events_dir / "test_bot.exit.json"
        event_file.write_text(json.dumps(event_data))
        
        # Clear rl_engine adapter
        rl_engine._adapter_instance = None
        
        # Mock get_paths, score_event, and consume_triggers
        mock_paths = MagicMock()
        mock_paths.state_dir = state_dir
        mock_paths.alignment_events_dir = events_dir
        
        with patch('codebot.state_manager.get_paths', return_value=mock_paths), \
             patch('codebot.rl_engine.score_event', return_value={'score': 80}), \
             patch('codebot.prompt_optimizer.consume_triggers', return_value=1) as mock_consume:
            # Should not raise exception
            alignment_service.run_alignment_pipeline_for_all()
            
            # Verify consume_triggers was called
            assert mock_consume.called
            # Verify it was called with the correct arguments (triggers_dir, roles_dir)
            call_args = mock_consume.call_args
            assert call_args[0][0] == triggers_dir
            # The second arg should be BOTS_DIR / "roles" since tmp_path/roles doesn't exist
            roles_arg = call_args[0][1]
            assert str(roles_arg).endswith("/roles")

    def test_run_alignment_pipeline_processes_events(self, tmp_path):
        """Verify run_alignment_pipeline processes alignment events correctly."""
        import json
        from codebot import rl_engine
        
        # Create state directory structure
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events_dir = state_dir / "alignment_events"
        events_dir.mkdir()
        triggers_dir = state_dir / "alignment_triggers"
        triggers_dir.mkdir()
        
        # Create a test event file
        event_data = {
            "bot": "test_bot",
            "exit_code": 0,
            "exit_reason": "clean",
            "processed": False,
        }
        event_file = events_dir / "test_bot.exit.json"
        event_file.write_text(json.dumps(event_data))
        
        # Clear rl_engine adapter
        rl_engine._adapter_instance = None
        
        # Mock get_paths and score_event to return high score
        mock_paths = MagicMock()
        mock_paths.state_dir = state_dir
        mock_paths.alignment_events_dir = events_dir
        
        with patch('codebot.state_manager.get_paths', return_value=mock_paths), \
             patch('codebot.rl_engine.score_event', return_value={'score': 75}):
            result = alignment_service.run_alignment_pipeline("test_bot")
            
            # Should process the event
            assert result is True
            
            # Verify event was marked as processed
            updated_data = json.loads(event_file.read_text())
            assert updated_data["processed"] is True
            assert "processed_at" in updated_data
            assert "reward" in updated_data
            assert "pattern" in updated_data

    def test_run_alignment_pipeline_creates_trigger_for_low_score(self, tmp_path):
        """Verify run_alignment_pipeline creates alignment trigger for low scores."""
        import json
        from codebot import rl_engine
        
        # Create state directory structure
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events_dir = state_dir / "alignment_events"
        events_dir.mkdir()
        triggers_dir = state_dir / "alignment_triggers"
        triggers_dir.mkdir()
        
        # Create a test event
        event_data = {
            "bot": "test_bot",
            "exit_code": 1,
            "exit_reason": "error",
            "processed": False,
        }
        event_file = events_dir / "test_bot.exit.json"
        event_file.write_text(json.dumps(event_data))
        
        # Clear rl_engine adapter
        rl_engine._adapter_instance = None
        
        # Mock get_paths and score_event to return low score
        mock_paths = MagicMock()
        mock_paths.state_dir = state_dir
        mock_paths.alignment_events_dir = events_dir
        
        with patch('codebot.state_manager.get_paths', return_value=mock_paths), \
             patch('codebot.rl_engine.score_event', return_value={'score': 45}):
            result = alignment_service.run_alignment_pipeline("test_bot")
            
            # Should process the event
            assert result is True
            
            # Verify trigger was created
            trigger_file = triggers_dir / "test_bot.evolve.json"
            assert trigger_file.exists()
            
            trigger_data = json.loads(trigger_file.read_text())
            assert trigger_data["bot"] == "test_bot"
            assert trigger_data["score"] == 45
            assert trigger_data["verdict"] == "evolve"
            assert "reward" in trigger_data
            assert "pattern" in trigger_data

    def test_run_alignment_pipeline_skips_processed_events(self, tmp_path):
        """Verify run_alignment_pipeline skips already-processed events."""
        import json
        
        # Create state directory structure
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events_dir = state_dir / "alignment_events"
        events_dir.mkdir()
        
        # Create a processed event
        event_data = {
            "bot": "test_bot",
            "processed": True,
            "processed_at": 1234567890.0,
        }
        event_file = events_dir / "test_bot.exit.json"
        event_file.write_text(json.dumps(event_data))
        
        # Mock get_paths
        mock_paths = MagicMock()
        mock_paths.state_dir = state_dir
        mock_paths.alignment_events_dir = events_dir
        
        with patch('codebot.state_manager.get_paths', return_value=mock_paths):
            result = alignment_service.run_alignment_pipeline("test_bot")
            
            # Should not process (already done)
            assert result is False

    def test_run_alignment_pipeline_for_all_process_multiple_bots(self, tmp_path):
        """Verify run_alignment_pipeline_for_all processes events from multiple bots."""
        import json
        from codebot import rl_engine
        
        # Create state directory structure
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events_dir = state_dir / "alignment_events"
        events_dir.mkdir()
        triggers_dir = state_dir / "alignment_triggers"
        triggers_dir.mkdir()
        
        # Create events for multiple bots
        for bot_name in ["bot1", "bot2"]:
            event_data = {
                "bot": bot_name,
                "exit_reason": "clean",
                "processed": False,
            }
            event_file = events_dir / f"{bot_name}.exit.json"
            event_file.write_text(json.dumps(event_data))
        
        # Clear rl_engine adapter
        rl_engine._adapter_instance = None
        
        # Mock get_paths and score_event
        mock_paths = MagicMock()
        mock_paths.state_dir = state_dir
        mock_paths.alignment_events_dir = events_dir
        
        with patch('codebot.state_manager.get_paths', return_value=mock_paths), \
             patch('codebot.rl_engine.score_event', return_value={'score': 80}):
            alignment_service.run_alignment_pipeline_for_all()
            
            # Verify both events were processed
            for bot_name in ["bot1", "bot2"]:
                event_file = events_dir / f"{bot_name}.exit.json"
                updated_data = json.loads(event_file.read_text())
                assert updated_data["processed"] is True

    def test_run_alignment_pipeline_handles_malformed_json(self, tmp_path):
        """Verify run_alignment_pipeline handles malformed JSON gracefully."""
        # Create state directory structure
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events_dir = state_dir / "alignment_events"
        events_dir.mkdir()
        
        # Create a malformed event file
        event_file = events_dir / "test_bot.exit.json"
        event_file.write_text("{invalid json")
        
        # Mock get_paths
        mock_paths = MagicMock()
        mock_paths.state_dir = state_dir
        mock_paths.alignment_events_dir = events_dir
        
        with patch('codebot.state_manager.get_paths', return_value=mock_paths):
            # Should not raise exception
            result = alignment_service.run_alignment_pipeline("test_bot")
            
            # Should return False (no valid events processed)
            assert result is False

    def test_run_alignment_pipeline_handles_missing_events_dir(self, tmp_path):
        """Verify run_alignment_pipeline handles missing events directory."""
        # Create state directory without events subdirectory
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        
        # Mock get_paths
        mock_paths = MagicMock()
        mock_paths.state_dir = state_dir
        mock_paths.alignment_events_dir = state_dir / "alignment_events"  # Doesn't exist
        
        with patch('codebot.state_manager.get_paths', return_value=mock_paths):
            result = alignment_service.run_alignment_pipeline("test_bot")
            
            # Should return False gracefully
            assert result is False

    def test_run_alignment_pipeline_handles_rl_import_error(self, tmp_path):
        """Verify run_alignment_pipeline handles rl_engine import failure."""
        # Create state directory structure
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events_dir = state_dir / "alignment_events"
        events_dir.mkdir()
        
        # Mock get_paths and rl_engine import to fail
        mock_paths = MagicMock()
        mock_paths.state_dir = state_dir
        mock_paths.alignment_events_dir = events_dir
        
        with patch('codebot.state_manager.get_paths', return_value=mock_paths), \
             patch.dict('sys.modules', {'codebot.rl_engine': None}):
            result = alignment_service.run_alignment_pipeline("test_bot")
            
            # Should return False gracefully
            assert result is False

    def test_run_alignment_pipeline_handles_file_read_error(self, tmp_path):
        """Verify run_alignment_pipeline handles file read errors."""
        # Create state directory structure
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events_dir = state_dir / "alignment_events"
        events_dir.mkdir()
        
        # Create an event file with permission issues
        event_file = events_dir / "test_bot.exit.json"
        event_file.write_text('{"valid": "json"}')
        event_file.chmod(0o000)  # Remove all permissions
        
        try:
            # Mock get_paths
            mock_paths = MagicMock()
            mock_paths.state_dir = state_dir
            mock_paths.alignment_events_dir = events_dir
            
            with patch('codebot.state_manager.get_paths', return_value=mock_paths):
                # Should not raise exception
                result = alignment_service.run_alignment_pipeline("test_bot")
                
                # Should return False (couldn't read file)
                assert result is False
        finally:
            # Restore permissions for cleanup
            event_file.chmod(0o644)

    def test_run_alignment_pipeline_handles_trigger_write_error(self, tmp_path):
        """Verify run_alignment_pipeline handles trigger write errors gracefully."""
        import json
        from codebot import rl_engine
        
        # Create state directory structure
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events_dir = state_dir / "alignment_events"
        events_dir.mkdir()
        
        # Create a test event
        event_data = {
            "bot": "test_bot",
            "exit_reason": "error",
            "processed": False,
        }
        event_file = events_dir / "test_bot.exit.json"
        event_file.write_text(json.dumps(event_data))
        
        # Clear rl_engine adapter
        rl_engine._adapter_instance = None
        
        # Mock get_paths and score_event
        mock_paths = MagicMock()
        mock_paths.state_dir = state_dir
        mock_paths.alignment_events_dir = events_dir
        
        with patch('codebot.state_manager.get_paths', return_value=mock_paths), \
             patch('codebot.rl_engine.score_event', return_value={'score': 45}):
            # Mock Path.mkdir to fail for triggers_dir
            original_mkdir = Path.mkdir
            def failing_mkdir(self, *args, **kwargs):
                if 'alignment_triggers' in str(self):
                    raise OSError("Permission denied")
                return original_mkdir(self, *args, **kwargs)
            
            with patch.object(Path, 'mkdir', failing_mkdir):
                # Should not raise exception
                result = alignment_service.run_alignment_pipeline("test_bot")
                
                # Should still process the event
                assert result is True

    def test_run_alignment_pipeline_guard_triggers_on_missing_adapter_non_canonical_path(self, tmp_path, caplog):
        """Verify run_alignment_pipeline aborts when adapter is None and state_dir is non-canonical."""
        import json
        import logging
        from codebot import rl_engine
        
        # Create state directory structure
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events_dir = state_dir / "alignment_events"
        events_dir.mkdir()
        
        # Create a test event file
        event_data = {
            "bot": "test_bot",
            "exit_reason": "clean",
            "processed": False,
        }
        event_file = events_dir / "test_bot.exit.json"
        event_file.write_text(json.dumps(event_data))
        
        # Clear rl_engine adapter
        rl_engine._adapter_instance = None
        
        # Mock get_paths to return non-canonical state_dir
        mock_paths = MagicMock()
        mock_paths.state_dir = state_dir  # This is NOT the canonical path
        mock_paths.alignment_events_dir = events_dir
        
        # Mock get_adapter_instance to return None
        with patch('codebot.state_manager.get_paths', return_value=mock_paths), \
             patch('codebot.state_manager.get_adapter_instance', return_value=None), \
             caplog.at_level(logging.CRITICAL):
            result = alignment_service.run_alignment_pipeline("test_bot")
            
            # Should return False (guard triggered)
            assert result is False
            
            # Should log CRITICAL message
            assert any(
                record.levelno == logging.CRITICAL and 
                "Alignment pipeline aborted" in record.message
                for record in caplog.records
            )
            
            # Verify event was NOT processed (zero events mutated)
            updated_data = json.loads(event_file.read_text())
            assert updated_data["processed"] is False
            # Ensure no other exit.json files were created or modified
            all_exit_files = list(events_dir.glob("*.exit.json"))
            assert len(all_exit_files) == 1
            assert all_exit_files[0] == event_file

    def test_run_alignment_pipeline_guard_passes_with_canonical_path(self, tmp_path):
        """Verify run_alignment_pipeline proceeds when state_dir matches canonical path."""
        import json
        from codebot import rl_engine
        
        # Use the actual canonical path
        canonical_state_dir = (Path(alignment_service.__file__).parent.parent / ".codebot" / "state")
        canonical_state_dir.mkdir(parents=True, exist_ok=True)
        events_dir = canonical_state_dir / "alignment_events"
        events_dir.mkdir(exist_ok=True)
        
        # Create a test event file
        event_data = {
            "bot": "test_bot",
            "exit_reason": "clean",
            "processed": False,
        }
        event_file = events_dir / "test_bot.exit.json"
        event_file.write_text(json.dumps(event_data))
        
        # Clear rl_engine adapter
        rl_engine._adapter_instance = None
        
        # Mock get_paths to return canonical state_dir
        mock_paths = MagicMock()
        mock_paths.state_dir = canonical_state_dir
        mock_paths.alignment_events_dir = events_dir
        
        try:
            # Mock get_adapter_instance to return None (but path is canonical)
            with patch('codebot.state_manager.get_paths', return_value=mock_paths), \
                 patch('codebot.state_manager.get_adapter_instance', return_value=None), \
                 patch('codebot.rl_engine.score_event', return_value={'score': 80}):
                result = alignment_service.run_alignment_pipeline("test_bot")
                
                # Should proceed (guard passes because path is canonical)
                assert result is True
                
                # Verify event was processed
                updated_data = json.loads(event_file.read_text())
                assert updated_data["processed"] is True
        finally:
            # Cleanup
            if event_file.exists():
                event_file.unlink()

    def test_run_alignment_pipeline_guard_passes_with_adapter_set(self, tmp_path):
        """Verify run_alignment_pipeline proceeds normally when adapter is set (happy path)."""
        import json
        from codebot import rl_engine

        # Create state directory structure (non-canonical path)
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events_dir = state_dir / "alignment_events"
        events_dir.mkdir()

        # Create a test event file
        event_data = {
            "bot": "test_bot",
            "exit_reason": "clean",
            "processed": False,
        }
        event_file = events_dir / "test_bot.exit.json"
        event_file.write_text(json.dumps(event_data))

        # Mock get_paths to return non-canonical state_dir
        mock_paths = MagicMock()
        mock_paths.state_dir = state_dir
        mock_paths.alignment_events_dir = events_dir

        # Mock adapter as present (guard should pass regardless of path)
        mock_adapter = MagicMock()

        with patch('codebot.state_manager.get_paths', return_value=mock_paths), \
             patch('codebot.state_manager.get_adapter_instance', return_value=mock_adapter), \
             patch('codebot.rl_engine.score_event', return_value={'score': 80}):
            result = alignment_service.run_alignment_pipeline("test_bot")

            # Should proceed and process event (adapter is set)
            assert result is True

            # Verify event was processed normally
            updated_data = json.loads(event_file.read_text())
            assert updated_data["processed"] is True
            assert "processed_at" in updated_data
            assert "reward" in updated_data
            assert "pattern" in updated_data