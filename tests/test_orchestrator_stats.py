"""Tests for orchestrator stats integration."""
import tempfile
import pytest
from pathlib import Path
from codebot.orchestrator import Orchestrator


def test_orchestrator_get_model_stats():
    """Test that orchestrator can query model stats."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create minimal project structure
        project_dir = Path(tmpdir) / "test_project"
        project_dir.mkdir()
        
        # Create .codebot/state directory
        state_dir = project_dir / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        
        # Create orchestrator
        orchestrator = Orchestrator(project_root=str(project_dir))
        
        # Initially should have no stats
        stats = orchestrator.get_model_stats()
        assert len(stats) == 0
        
        # Filtered queries should also work
        filtered = orchestrator.get_model_stats(model_name="gpt-4")
        assert len(filtered) == 0


def test_orchestrator_stats_persist_across_instances():
    """Test that stats persist across orchestrator restarts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir) / "test_project"
        project_dir.mkdir()
        
        state_dir = project_dir / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        
        # First orchestrator instance
        orchestrator1 = Orchestrator(project_root=str(project_dir))
        
        # Manually record some stats via the stats_collector
        orchestrator1.stats_collector.record_call(
            model_name="gpt-4",
            task_type="code_generation",
            success=True,
            cost=0.01,
            tokens_in=100,
            tokens_out=50
        )
        
        # Second orchestrator instance (simulates restart)
        orchestrator2 = Orchestrator(project_root=str(project_dir))
        
        # Stats should persist
        stats = orchestrator2.get_model_stats()
        key = "gpt-4:code_generation"
        assert key in stats
        assert stats[key]["total_calls"] == 1
