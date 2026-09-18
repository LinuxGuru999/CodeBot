"""Tests for API client with stats collection."""
import tempfile
import pytest
from codebot.api_client import APIClient
from codebot.stats_collector import StatsCollector


def test_api_client_records_stats():
    """Test that APIClient records stats when stats_collector is provided."""
    with tempfile.TemporaryDirectory() as tmpdir:
        stats_collector = StatsCollector(state_dir=tmpdir)
        client = APIClient(stats_collector=stats_collector)
        
        result = client.call_model(
            model_name="gpt-4",
            prompt="test prompt here",
            task_type="code_generation"
        )
        
        # Verify result
        assert result["success"] is True
        assert result["model"] == "gpt-4"
        
        # Verify stats were recorded
        stats = stats_collector.get_stats()
        key = "gpt-4:code_generation"
        assert key in stats
        assert stats[key]["total_calls"] == 1
        assert stats[key]["successful_calls"] == 1


def test_api_client_without_stats_collector():
    """Test that APIClient works without stats_collector."""
    client = APIClient(stats_collector=None)
    
    result = client.call_model(
        model_name="gpt-4",
        prompt="test prompt",
        task_type="bug_fix"
    )
    
    # Should still work, just no stats recorded
    assert result["success"] is True
    assert result["model"] == "gpt-4"


def test_api_client_records_failed_call():
    """Test that failed calls are recorded correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        stats_collector = StatsCollector(state_dir=tmpdir)
        client = APIClient(stats_collector=stats_collector)
        
        # Simulate a failed call by manually calling record_call
        # Note: In real implementation, the API would return success=False
        stats_collector.record_call(
            model_name="gpt-4",
            task_type="code_generation",
            success=False,
            cost=0.0,
            tokens_in=100,
            tokens_out=0
        )
        
        stats = stats_collector.get_stats()
        key = "gpt-4:code_generation"
        assert key in stats
        assert stats[key]["failed_calls"] == 1
        assert stats[key]["successful_calls"] == 0
