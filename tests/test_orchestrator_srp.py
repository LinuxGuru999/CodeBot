"""Tests for orchestrator SRP refactoring.

Verifies that the orchestrator has been properly decomposed into focused modules
and that each extracted module exposes the expected public API.
"""
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def _count_effective_loc(filepath: Path) -> int:
    """Count lines of code excluding blanks and comments."""
    if not filepath.exists():
        return 0
    count = 0
    in_multiline_string = False
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            stripped = line.strip()
            # Skip blank lines
            if not stripped:
                continue
            # Skip pure comment lines
            if stripped.startswith('#'):
                continue
            # Count the line
            count += 1
    return count


def test_orchestrator_loc_under_500():
    """Orchestrator should be under 500 effective lines of code after refactoring."""
    orchestrator_path = project_root / "codebot" / "orchestrator.py"
    assert orchestrator_path.exists(), "orchestrator.py not found"
    
    effective_loc = _count_effective_loc(orchestrator_path)
    print(f"orchestrator.py has {effective_loc} effective LOC (excluding blanks/comments)")
    assert effective_loc < 500, f"orchestrator.py has {effective_loc} effective LOC, expected < 500"


# ---------------------------------------------------------------------------
# Tests for actual extracted modules (replacing config_reloader/scheduler)
# ---------------------------------------------------------------------------

def test_process_manager_module_exists():
    """Process manager module should exist and expose bot lifecycle functions."""
    from codebot import process_manager
    
    # Core lifecycle functions
    assert hasattr(process_manager, 'start_bot'), "process_manager missing start_bot"
    assert hasattr(process_manager, 'stop_bot'), "process_manager missing stop_bot"
    assert hasattr(process_manager, 'restart_bot'), "process_manager missing restart_bot"
    
    # Heartbeat monitoring
    assert hasattr(process_manager, 'read_heartbeat'), "process_manager missing read_heartbeat"
    assert hasattr(process_manager, 'batch_read_heartbeats'), "process_manager missing batch_read_heartbeats"
    
    # Bot registry
    assert hasattr(process_manager, 'BOT_REGISTRY'), "process_manager missing BOT_REGISTRY"


def test_ticket_dispatcher_module_exists():
    """Ticket dispatcher module should exist and expose dispatch/claim functions."""
    from codebot import ticket_dispatcher
    
    # Role name constants
    assert hasattr(ticket_dispatcher, 'IMPLEMENTER_ROLE_NAMES'), "ticket_dispatcher missing IMPLEMENTER_ROLE_NAMES"
    assert hasattr(ticket_dispatcher, 'REVIEWER_ROLE_NAMES'), "ticket_dispatcher missing REVIEWER_ROLE_NAMES"
    assert hasattr(ticket_dispatcher, 'DISCOVERY_ROLE_NAMES'), "ticket_dispatcher missing DISCOVERY_ROLE_NAMES"
    
    # Dispatch functions
    assert hasattr(ticket_dispatcher, 'reviewer_roles_for_ticket'), "ticket_dispatcher missing reviewer_roles_for_ticket"
    assert hasattr(ticket_dispatcher, 'write_review_packet'), "ticket_dispatcher missing write_review_packet"
    assert hasattr(ticket_dispatcher, 'write_implementation_packet'), "ticket_dispatcher missing write_implementation_packet"
    
    # Claim management
    assert hasattr(ticket_dispatcher, 'CLAIM_TTL_SECONDS'), "ticket_dispatcher missing CLAIM_TTL_SECONDS"


def test_dispatch_service_module_exists():
    """Dispatch service module should exist and expose pipeline state functions."""
    from codebot import dispatch_service
    
    # Pipeline state
    assert hasattr(dispatch_service, 'get_pipeline_state'), "dispatch_service missing get_pipeline_state"
    assert hasattr(dispatch_service, 'is_needed_bot'), "dispatch_service missing is_needed_bot"
    
    # Agent availability
    assert hasattr(dispatch_service, 'apply_agent_availability'), "dispatch_service missing apply_agent_availability"
    
    # Role name constants
    assert hasattr(dispatch_service, 'IMPLEMENTER_ROLE_NAMES'), "dispatch_service missing IMPLEMENTER_ROLE_NAMES"
    assert hasattr(dispatch_service, 'REVIEWER_ROLE_NAMES'), "dispatch_service missing REVIEWER_ROLE_NAMES"


def test_state_manager_module_exists():
    """State manager module should exist and expose path/drain control functions."""
    from codebot import state_manager
    
    # Path configuration
    assert hasattr(state_manager, 'get_paths'), "state_manager missing get_paths"
    assert hasattr(state_manager, 'PathConfig'), "state_manager missing PathConfig"
    
    # Drain control
    assert hasattr(state_manager, 'is_draining'), "state_manager missing is_draining"
    assert hasattr(state_manager, 'set_drain'), "state_manager missing set_drain"
    assert hasattr(state_manager, 'clear_drain'), "state_manager missing clear_drain"
    assert hasattr(state_manager, 'drain_status'), "state_manager missing drain_status"
    
    # Backup/restore
    assert hasattr(state_manager, 'backup_botnet'), "state_manager missing backup_botnet"
    assert hasattr(state_manager, 'restore_botnet'), "state_manager missing restore_botnet"


def test_worker_scaler_module_exists():
    """Worker scaler module should exist and expose scaling/registry functions."""
    from codebot import worker_scaler
    
    # Scaling functions
    assert hasattr(worker_scaler, 'rotating_slots'), "worker_scaler missing rotating_slots"
    assert hasattr(worker_scaler, 'worker_reserved_slots'), "worker_scaler missing worker_reserved_slots"
    
    # Registry functions
    assert hasattr(worker_scaler, 'load_bot_registry'), "worker_scaler missing load_bot_registry"
    assert hasattr(worker_scaler, 'build_bots'), "worker_scaler missing build_bots"
    
    # Health/budget checks
    assert hasattr(worker_scaler, 'is_manifest_error_disabled'), "worker_scaler missing is_manifest_error_disabled"
    assert hasattr(worker_scaler, 'is_manifest_restart_budget_exceeded'), "worker_scaler missing is_manifest_restart_budget_exceeded"


def test_extracted_modules_are_independent():
    """Verify extracted modules can be imported without circular dependencies."""
    # Each module should be importable independently
    import codebot.process_manager
    import codebot.ticket_dispatcher
    import codebot.dispatch_service
    import codebot.state_manager
    import codebot.worker_scaler
    
    # All imports succeeded if we reach here
    assert True


def test_state_manager_get_paths_returns_valid_config():
    """get_paths() should return a PathConfig with valid directories."""
    from codebot.state_manager import get_paths, PathConfig
    
    paths = get_paths()
    assert isinstance(paths, PathConfig), "get_paths() should return PathConfig instance"
    
    # Verify required attributes exist
    assert hasattr(paths, 'state_dir'), "PathConfig missing state_dir"
    assert hasattr(paths, 'logs_dir'), "PathConfig missing logs_dir"
    assert hasattr(paths, 'bots_dir'), "PathConfig missing bots_dir"
    assert hasattr(paths, 'drain_file'), "PathConfig missing drain_file"
    assert hasattr(paths, 'update_lock'), "PathConfig missing update_lock"
