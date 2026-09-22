"""Extensive tests for codebot.state_manager — P0 path/config, drain/lock, adapter, backup/restore.

Covers all public symbols in state_manager.py:
PathConfig, _paths, set_adapter_instance, get_adapter_instance,
set_project_adapter, get_paths, is_draining, set_drain, clear_drain,
drain_status, backup_botnet, restore_botnet, check_self_restart, safe_stop_all.

Uses tmp_path only — never touches live .codebot/state.
One When per test, Given/When/Then docstrings, isolated fixtures.
"""
from __future__ import annotations

import time
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import codebot.state_manager as sm
from codebot.state_manager import (
    PathConfig,
    backup_botnet,
    check_self_restart,
    clear_drain,
    drain_status,
    get_adapter_instance,
    get_paths,
    is_draining,
    restore_botnet,
    safe_stop_all,
    set_adapter_instance,
    set_drain,
    set_project_adapter,
)


# ---------------------------------------------------------------------------
# Isolation fixture — save/restore module globals after each test
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _restore_state_manager() -> Generator[None, None, None]:
    original_paths = getattr(sm, "_paths")
    original_adapter = getattr(sm, "_adapter_instance")
    yield
    setattr(sm, "_paths", original_paths)
    setattr(sm, "_adapter_instance", original_adapter)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _isolated_config(tmp_path: Path) -> PathConfig:
    """Create a PathConfig rooted at tmp_path and assign to sm._paths."""
    state_dir = tmp_path / "state"
    logs_dir = tmp_path / "logs"
    cfg = PathConfig(
        bots_dir=tmp_path / "bots",
        state_dir=state_dir,
        logs_dir=logs_dir,
        backup_dir=state_dir / "backup",
        drain_file=state_dir / ".drain",
        update_lock=state_dir / ".update_lock",
        restart_file=state_dir / ".restart",
        alignment_events_dir=state_dir / "alignment_events",
    )
    for d in (cfg.state_dir, cfg.logs_dir, cfg.backup_dir, cfg.alignment_events_dir, cfg.bots_dir):
        d.mkdir(parents=True, exist_ok=True)
    sm._paths = cfg
    return cfg


# ---------------------------------------------------------------------------
# PathConfig / path resolution
# ---------------------------------------------------------------------------
def test_path_config_fields_are_paths_Given_dataclass_When_inspected_Then_all_fields_are_Path(tmp_path: Path) -> None:
    """Given PathConfig dataclass
    When inspecting its fields
    Then all path fields exist and are Path-typed when instantiated."""
    cfg = _isolated_config(tmp_path)
    assert isinstance(cfg.bots_dir, Path)
    assert isinstance(cfg.state_dir, Path)
    assert isinstance(cfg.logs_dir, Path)
    assert isinstance(cfg.backup_dir, Path)
    assert isinstance(cfg.alignment_events_dir, Path)
    assert isinstance(cfg.drain_file, Path)
    assert isinstance(cfg.update_lock, Path)
    assert isinstance(cfg.restart_file, Path)


def test_get_paths_returns_path_config_Given_isolated_state_When_get_paths_Then_is_path_config(tmp_path: Path) -> None:
    """Given an isolated state dir
    When get_paths() is called
    Then it returns a PathConfig instance."""
    _isolated_config(tmp_path)
    result = get_paths()
    assert isinstance(result, PathConfig)


def test_get_paths_reflects_isolated_config_Given_custom_tmp_dirs_When_get_paths_Then_matches_isolated(tmp_path: Path) -> None:
    """Given sm._paths pointed at tmp_path
    When get_paths() is called
    Then it reflects the tmp_path layout, not the live project root."""
    cfg = _isolated_config(tmp_path)
    got = get_paths()
    assert got.state_dir == cfg.state_dir
    assert got.logs_dir == cfg.logs_dir
    assert got.bots_dir == cfg.bots_dir
    assert got.backup_dir == cfg.state_dir / "backup"
    assert got.drain_file == cfg.state_dir / ".drain"
    assert got.update_lock == cfg.state_dir / ".update_lock"
    assert got.restart_file == cfg.state_dir / ".restart"
    assert got.alignment_events_dir == cfg.state_dir / "alignment_events"


def test_default_paths_derived_from_project_root_Given_no_adapter_When_get_paths_Then_defaults_under_project_root(tmp_path: Path) -> None:
    """Given the module-imported defaults (no custom adapter)
    When checking the live _paths (restored after isolation)
    Then bots_dir is project root and state/logs are under .codebot."""
    # We check the live value by temporarily looking at the saved original
    # without mutating it — read the dataclass fields directly
    # Note: this test intentionally does NOT call _isolated_config
    # to verify default derivation logic is path-based.
    original = sm._paths
    # The live config should still point under project root
    project_root = sm._project_root
    assert original.bots_dir == project_root
    assert original.state_dir == project_root / ".codebot" / "state"
    assert original.logs_dir == project_root / ".codebot" / "logs"


# ---------------------------------------------------------------------------
# Adapter pattern
# ---------------------------------------------------------------------------
def test_set_and_get_adapter_instance_Given_adapter_When_set_then_get_Then_same_object(tmp_path: Path) -> None:
    """Given an adapter object
    When set_adapter_instance() is called
    Then get_adapter_instance() returns the same object."""
    _isolated_config(tmp_path)
    adapter = MagicMock()
    set_adapter_instance(adapter)
    assert get_adapter_instance() is adapter


def test_set_adapter_instance_overwrites_previous_Given_two_adapters_When_second_set_Then_second_returned(tmp_path: Path) -> None:
    """Given two adapters set sequentially
    When the second is set
    Then get_adapter_instance() returns the second."""
    _isolated_config(tmp_path)
    a1 = MagicMock()
    a2 = MagicMock()
    set_adapter_instance(a1)
    set_adapter_instance(a2)
    assert get_adapter_instance() is a2


def test_get_adapter_instance_initially_none_after_clear_Given_cleared_state_When_get_Then_none(tmp_path: Path) -> None:
    """Given adapter cleared to None
    When get_adapter_instance() is called
    Then it returns None."""
    _isolated_config(tmp_path)
    set_adapter_instance(None)
    assert get_adapter_instance() is None


def test_set_project_adapter_creates_dirs_and_returns_config_Given_valid_adapter_When_set_project_adapter_Then_dirs_created(tmp_path: Path) -> None:
    """Given a valid adapter with tmp paths
    When set_project_adapter() is called
    Then it returns a PathConfig with derived dirs created."""
    _isolated_config(tmp_path)
    new_state = tmp_path / "new_state"
    new_logs = tmp_path / "new_logs"
    new_repo = tmp_path / "new_repo"
    mock_paths = MagicMock()
    mock_paths.repository_root = new_repo
    mock_paths.state_dir = new_state
    mock_paths.logs_dir = new_logs
    adapter = MagicMock()
    adapter.paths.return_value = mock_paths

    result = set_project_adapter(adapter)

    assert isinstance(result, PathConfig)
    assert result.state_dir == new_state
    assert result.logs_dir == new_logs
    assert result.bots_dir == new_repo
    assert result.backup_dir == new_state / "backup"
    assert result.drain_file == new_state / ".drain"
    assert result.update_lock == new_state / ".update_lock"
    assert result.restart_file == new_state / ".restart"
    assert result.alignment_events_dir == new_state / "alignment_events"
    assert new_state.exists()
    assert new_logs.exists()
    assert (new_state / "backup").exists()
    assert (new_state / "alignment_events").exists()
    # get_paths() must now reflect adapter
    assert get_paths().state_dir == new_state
    # adapter instance must be stored
    assert get_adapter_instance() is adapter


def test_set_project_adapter_uses_defaults_for_missing_attrs_Given_partial_adapter_When_set_Then_fallback_to_current(tmp_path: Path) -> None:
    """Given an adapter whose paths() object lacks some attributes
    When set_project_adapter() is called
    Then missing fields fall back to current config values."""
    cfg = _isolated_config(tmp_path)

    class PartialPaths:
        def __init__(self) -> None:
            self.state_dir = tmp_path / "partial_state"
    partial_obj = PartialPaths()
    adapter = MagicMock()
    adapter.paths.return_value = partial_obj

    result = set_project_adapter(adapter)

    assert result.state_dir == tmp_path / "partial_state"
    # bots_dir should fallback to previous cfg.bots_dir
    assert result.bots_dir == cfg.bots_dir
    # logs_dir fallback to cfg.logs_dir
    assert result.logs_dir == cfg.logs_dir


def test_set_project_adapter_handles_exception_and_returns_current_Given_failing_adapter_When_set_Then_returns_current_and_logs(tmp_path: Path) -> None:
    """Given an adapter whose paths() raises
    When set_project_adapter() is called
    Then it logs a warning and returns the current PathConfig unchanged."""
    cfg = _isolated_config(tmp_path)
    bad_adapter = MagicMock()
    bad_adapter.paths.side_effect = RuntimeError("boom")

    result = set_project_adapter(bad_adapter)

    # Should return the same paths as before (not crash)
    assert result.state_dir == cfg.state_dir
    assert result.bots_dir == cfg.bots_dir
    assert get_paths().state_dir == cfg.state_dir
    # Adapter instance is still stored even when paths() failed
    assert get_adapter_instance() is bad_adapter


def test_set_project_adapter_stores_adapter_even_on_failure_Given_adapter_raises_When_set_Then_adapter_still_stored(tmp_path: Path) -> None:
    """Given a failing adapter
    When set_project_adapter() is invoked
    Then get_adapter_instance() still returns that adapter."""
    _isolated_config(tmp_path)
    bad = MagicMock()
    bad.paths.side_effect = ValueError("fail")
    set_project_adapter(bad)
    assert get_adapter_instance() is bad


# ---------------------------------------------------------------------------
# Drain / lock controls
# ---------------------------------------------------------------------------
def test_is_draining_false_when_no_file_Given_clean_state_When_is_draining_Then_false(tmp_path: Path) -> None:
    """Given a clean isolated state with no drain file
    When is_draining() is called
    Then it returns False."""
    _isolated_config(tmp_path)
    assert is_draining() is False


def test_set_drain_creates_file_and_is_draining_true_Given_reason_When_set_drain_Then_draining_true(tmp_path: Path) -> None:
    """Given an isolated state
    When set_drain('maintenance') is called
    Then is_draining() becomes True."""
    _isolated_config(tmp_path)
    set_drain("maintenance")
    assert is_draining() is True


def test_set_drain_writes_reason_and_timestamp_Given_fixed_time_When_set_drain_Then_file_contains_time_and_reason(tmp_path: Path) -> None:
    """Given a fixed clock
    When set_drain() is called with a reason
    Then the drain file contains the timestamp and reason."""
    cfg = _isolated_config(tmp_path)
    with patch.object(time, "time", return_value=1234567890.0):
        set_drain("deploy")
    content = cfg.drain_file.read_text()
    assert "1234567890.0" in content
    assert "deploy" in content


def test_set_drain_without_reason_still_creates_file_Given_no_reason_When_set_drain_Then_file_exists(tmp_path: Path) -> None:
    """Given no reason string
    When set_drain() is called with default
    Then drain file exists and is_draining is True."""
    cfg = _isolated_config(tmp_path)
    set_drain()
    assert cfg.drain_file.exists()
    assert is_draining() is True
    # Content should still have a timestamp line
    txt = cfg.drain_file.read_text()
    assert txt.strip() != ""


def test_clear_drain_removes_drain_and_lock_Given_both_files_When_clear_drain_Then_both_gone(tmp_path: Path) -> None:
    """Given drain file and update_lock both exist
    When clear_drain() is called
    Then both files are removed and is_draining is False."""
    cfg = _isolated_config(tmp_path)
    set_drain("test")
    # also create update_lock
    cfg.update_lock.write_text("locked")
    assert cfg.drain_file.exists()
    assert cfg.update_lock.exists()

    clear_drain()

    assert not cfg.drain_file.exists()
    assert not cfg.update_lock.exists()
    assert is_draining() is False


def test_clear_drain_idempotent_when_no_files_Given_no_files_When_clear_drain_Then_no_error(tmp_path: Path) -> None:
    """Given no drain or lock files
    When clear_drain() is called
    Then it does not raise and is_draining stays False."""
    _isolated_config(tmp_path)
    clear_drain()  # should not raise
    assert is_draining() is False
    # second call also idempotent
    clear_drain()
    assert is_draining() is False


def test_drain_status_not_draining_Given_clean_When_drain_status_Then_reports_not_draining(tmp_path: Path) -> None:
    """Given a clean state
    When drain_status() is called
    Then it reports draining=False and null paths."""
    _isolated_config(tmp_path)
    status = drain_status()
    assert status["draining"] is False
    assert status["drain_file"] is None
    assert status["drain_reason"] is None


def test_drain_status_draining_with_reason_Given_drain_set_When_drain_status_Then_reports_draining_true(tmp_path: Path) -> None:
    """Given drain has been set with a reason
    When drain_status() is called
    Then it reports draining=True with file path and reason."""
    cfg = _isolated_config(tmp_path)
    with patch.object(time, "time", return_value=1000.0):
        set_drain("my-reason")
    status = drain_status()
    assert status["draining"] is True
    assert status["drain_file"] == str(cfg.drain_file)
    assert status["drain_reason"] is not None
    assert "my-reason" in status["drain_reason"]
    assert "1000.0" in status["drain_reason"]


def test_drain_status_shows_update_lock_when_present_Given_lock_exists_When_drain_status_Then_lock_path_reported(tmp_path: Path) -> None:
    """Given an update_lock file exists
    When drain_status() is called
    Then update_lock is reported as a string path."""
    cfg = _isolated_config(tmp_path)
    cfg.update_lock.write_text("lock")
    status = drain_status()
    assert status["update_lock"] == str(cfg.update_lock)

    # after clear, it should be None
    cfg.update_lock.unlink()
    status2 = drain_status()
    assert status2["update_lock"] is None


def test_drain_status_update_lock_none_when_absent_Given_no_lock_When_drain_status_Then_update_lock_none(tmp_path: Path) -> None:
    """Given no update_lock file
    When drain_status() is called
    Then update_lock is None."""
    _isolated_config(tmp_path)
    status = drain_status()
    assert status["update_lock"] is None


# ---------------------------------------------------------------------------
# backup / restore
# ---------------------------------------------------------------------------
def test_backup_botnet_creates_backup_with_md_files_Given_bots_dir_with_md_When_backup_botnet_Then_files_copied(tmp_path: Path) -> None:
    """Given BOTS_DIR containing .md files and a tmp state dir
    When backup_botnet() is called
    Then a timestamped backup dir is created containing copies of the .md files."""
    state_cfg = _isolated_config(tmp_path)
    bots_dir = tmp_path / "bots_src"
    bots_dir.mkdir(parents=True, exist_ok=True)
    (bots_dir / "a.md").write_text("# a")
    (bots_dir / "b.md").write_text("# b")
    (bots_dir / "ignore.txt").write_text("not md")

    with patch("codebot.process_manager.BOTS_DIR", bots_dir):
        with patch.object(time, "strftime", return_value="20200101-120000"):
            dest = backup_botnet()

    assert dest.exists()
    assert dest.parent == state_cfg.backup_dir
    assert dest.name == "botnet-20200101-120000"
    assert (dest / "a.md").exists()
    assert (dest / "b.md").exists()
    assert not (dest / "ignore.txt").exists()
    assert (dest / "a.md").read_text() == "# a"


def test_backup_botnet_with_tag_includes_tag_Given_tag_When_backup_botnet_Then_dirname_contains_tag(tmp_path: Path) -> None:
    """Given a tag string
    When backup_botnet(tag='mytag') is called
    Then the backup dirname contains the tag prefix."""
    _isolated_config(tmp_path)
    bots_dir = tmp_path / "bots_tag"
    bots_dir.mkdir(parents=True, exist_ok=True)
    (bots_dir / "x.md").write_text("x")

    with patch("codebot.process_manager.BOTS_DIR", bots_dir):
        with patch.object(time, "strftime", return_value="20200101-120000"):
            dest = backup_botnet(tag="mytag")

    assert "mytag" in dest.name
    assert dest.name == "botnet-mytag-20200101-120000"


def test_backup_botnet_empty_bots_dir_Given_no_md_files_When_backup_botnet_Then_empty_backup(tmp_path: Path) -> None:
    """Given an empty BOTS_DIR
    When backup_botnet() is called
    Then an empty backup directory is still created."""
    _isolated_config(tmp_path)
    empty_bots = tmp_path / "empty_bots"
    empty_bots.mkdir(parents=True, exist_ok=True)

    with patch("codebot.process_manager.BOTS_DIR", empty_bots):
        with patch.object(time, "strftime", return_value="20200101-120000"):
            dest = backup_botnet()

    assert dest.exists()
    assert list(dest.glob("*.md")) == []


def test_restore_botnet_copies_files_back_Given_backup_with_md_When_restore_botnet_Then_files_restored(tmp_path: Path) -> None:
    """Given a backup dir with .md files
    When restore_botnet() is called
    Then files are copied back to BOTS_DIR."""
    _isolated_config(tmp_path)
    bots_dir = tmp_path / "bots_restore"
    bots_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = tmp_path / "backup_src"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "hello.md").write_bytes(b"# hello")

    with patch("codebot.process_manager.BOTS_DIR", bots_dir):
        restore_botnet(backup_dir)

    assert (bots_dir / "hello.md").exists()
    assert (bots_dir / "hello.md").read_bytes() == b"# hello"


def test_restore_botnet_raises_when_missing_Given_nonexistent_dir_When_restore_botnet_Then_raises(tmp_path: Path) -> None:
    """Given a nonexistent backup directory
    When restore_botnet() is called
    Then it raises FileNotFoundError."""
    _isolated_config(tmp_path)
    missing = tmp_path / "does_not_exist"
    with pytest.raises(FileNotFoundError):
        restore_botnet(missing)


def test_restore_botnet_overwrites_existing_Given_existing_file_When_restore_botnet_Then_overwritten(tmp_path: Path) -> None:
    """Given BOTS_DIR already has a file and backup has newer content
    When restore_botnet() is called
    Then the destination file is overwritten with backup content."""
    _isolated_config(tmp_path)
    bots_dir = tmp_path / "bots_overwrite"
    bots_dir.mkdir(parents=True, exist_ok=True)
    (bots_dir / "doc.md").write_bytes(b"old")
    backup_dir = tmp_path / "backup_overwrite"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "doc.md").write_bytes(b"new content")

    with patch("codebot.process_manager.BOTS_DIR", bots_dir):
        restore_botnet(backup_dir)

    assert (bots_dir / "doc.md").read_bytes() == b"new content"


# ---------------------------------------------------------------------------
# check_self_restart
# ---------------------------------------------------------------------------
def test_check_self_restart_returns_false_when_no_file_Given_no_restart_file_When_check_Then_false(tmp_path: Path) -> None:
    """Given no restart file
    When check_self_restart() is called
    Then it returns False and does not call supervisor."""
    _isolated_config(tmp_path)
    mock_supervisor = MagicMock()
    result = check_self_restart({}, lambda b, r: None, supervisor=mock_supervisor)
    assert result is False
    mock_supervisor.restart_self.assert_not_called()


def test_check_self_restart_stops_running_bots_and_restarts_Given_restart_file_and_running_bot_When_check_Then_stops_and_restarts(
    tmp_path: Path,
) -> None:
    """Given a restart file and a running bot
    When check_self_restart() is called
    Then it stops running bots, removes the file, and calls supervisor.restart_self()."""
    cfg = _isolated_config(tmp_path)
    cfg.restart_file.write_text("upgrade")

    bot_running = MagicMock()
    bot_running.process = MagicMock()
    bot_running.process.poll.return_value = None  # running
    bot_stopped = MagicMock()
    bot_stopped.process = MagicMock()
    bot_stopped.process.poll.return_value = 1  # already exited
    bot_no_process = MagicMock()
    bot_no_process.process = None

    bots = {"running": bot_running, "stopped": bot_stopped, "noprocess": bot_no_process}
    stop_calls: list[tuple[object, str]] = []

    def stop_fn(bot: object, reason: str) -> None:
        stop_calls.append((bot, reason))

    mock_supervisor = MagicMock()

    result = check_self_restart(bots, stop_fn, supervisor=mock_supervisor)

    assert result is True
    # only running bot should have been stopped with self-restart
    assert len(stop_calls) == 1
    assert stop_calls[0][0] is bot_running
    assert stop_calls[0][1] == "self-restart"
    assert not cfg.restart_file.exists()
    mock_supervisor.restart_self.assert_called_once()


def test_check_self_restart_handles_oses_on_read_Given_restart_file_When_read_raises_Then_uses_manual(tmp_path: Path) -> None:
    """Given a restart file that raises OSError on read
    When check_self_restart() is called
    Then it falls back to 'manual' reason and still restarts."""
    cfg = _isolated_config(tmp_path)
    cfg.restart_file.write_text("original")

    bot = MagicMock()
    bot.process = None

    # Patch Path.read_text to raise OSError
    with patch.object(Path, "read_text", side_effect=OSError("read fail")):
        mock_supervisor = MagicMock()
        result = check_self_restart({"b": bot}, lambda b, r: None, supervisor=mock_supervisor)

    assert result is True
    mock_supervisor.restart_self.assert_called_once()


def test_check_self_restart_removes_file_after_Given_restart_file_When_check_Then_file_gone(tmp_path: Path) -> None:
    """Given a restart file with whitespace reason
    When check_self_restart() is called
    Then the restart file is removed."""
    cfg = _isolated_config(tmp_path)
    cfg.restart_file.write_text("   \n  ")

    result = check_self_restart({}, lambda b, r: None, supervisor=MagicMock())

    assert result is True
    assert not cfg.restart_file.exists()


def test_check_self_restart_handles_unlink_oses_Given_restart_file_When_unlink_raises_Then_still_restarts(tmp_path: Path) -> None:
    """Given a restart file whose unlink raises OSError
    When check_self_restart() is called
    Then it still calls supervisor.restart_self() despite unlink failure."""
    cfg = _isolated_config(tmp_path)
    cfg.restart_file.write_text("reason")

    # Make unlink raise OSError; Path.unlink is called with missing_ok=True
    with patch.object(Path, "unlink", side_effect=OSError("unlink fail")):
        mock_supervisor = MagicMock()
        result = check_self_restart({}, lambda b, r: None, supervisor=mock_supervisor)

    assert result is True
    mock_supervisor.restart_self.assert_called_once()


def test_check_self_restart_uses_default_supervisor_when_none_Given_no_supervisor_When_check_Then_default_used(tmp_path: Path) -> None:
    """Given no explicit supervisor
    When check_self_restart() is called with a restart file
    Then it instantiates DefaultProcessSupervisor."""
    cfg = _isolated_config(tmp_path)
    cfg.restart_file.write_text("auto")

    with patch("codebot.process_supervisor.DefaultProcessSupervisor") as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance
        result = check_self_restart({}, lambda b, r: None)

    assert result is True
    mock_instance.restart_self.assert_called_once()


def test_check_self_restart_does_not_stop_non_running_Given_mixed_bots_When_check_Then_only_running_stopped(tmp_path: Path) -> None:
    """Given bots where some are not running
    When check_self_restart() is called
    Then only bots with poll() is None are stopped."""
    cfg = _isolated_config(tmp_path)
    cfg.restart_file.write_text("reason")

    running = MagicMock()
    running.process = MagicMock()
    running.process.poll.return_value = None

    exited = MagicMock()
    exited.process = MagicMock()
    exited.process.poll.return_value = 0

    none_proc = MagicMock()
    none_proc.process = None

    bots = {"r": running, "e": exited, "n": none_proc}
    stopped: list[object] = []

    def stop_fn(bot: object, reason: str) -> None:
        stopped.append(bot)

    check_self_restart(bots, stop_fn, supervisor=MagicMock())

    assert stopped == [running]


# ---------------------------------------------------------------------------
# safe_stop_all
# ---------------------------------------------------------------------------
def test_safe_stop_all_sets_drain_and_handles_mixed_bots_Given_mixed_bots_When_safe_stop_all_Then_correct_statuses(
    tmp_path: Path,
) -> None:
    """Given bots in mixed states (running vs already stopped)
    When safe_stop_all() is called
    Then it sets drain, stops running bots, and returns per-bot statuses."""
    cfg = _isolated_config(tmp_path)

    # Create bots
    running = MagicMock()
    running.process = MagicMock()
    running.process.poll.return_value = None  # running
    running.config.name = "running"

    stopped_none = MagicMock()
    stopped_none.process = None
    stopped_none.config.name = "stopped_none"

    stopped_exited = MagicMock()
    stopped_exited.process = MagicMock()
    stopped_exited.process.poll.return_value = 1
    stopped_exited.config.name = "stopped_exited"

    bots: dict[str, object] = {"running": running, "stopped_none": stopped_none, "stopped_exited": stopped_exited}

    stop_calls: list[object] = []

    def stop_fn(bot: object, reason: str) -> None:
        assert reason == "safe-stop"
        stop_calls.append(bot)

    with patch("codebot.process_manager.update_bot_state") as mock_update:
        result = safe_stop_all(bots, stop_fn)

    assert is_draining() is True
    assert cfg.drain_file.exists()
    assert result["running"] == "stopped"
    assert result["stopped_none"] == "already stopped"
    assert result["stopped_exited"] == "already stopped"
    assert stop_calls == [running]
    # update_bot_state should have been called for each bot
    assert mock_update.call_count == 3
    # running -> drained, others -> stopped
    call_statuses = [c.args[1] for c in mock_update.call_args_list]
    assert "drained" in call_statuses
    assert "stopped" in call_statuses


def test_safe_stop_all_drain_file_contains_safe_stop_reason_Given_bots_When_safe_stop_all_Then_drain_reason_is_safe_stop(
    tmp_path: Path,
) -> None:
    """Given any bots
    When safe_stop_all() is called
    Then the drain file reason is 'safe-stop'."""
    _isolated_config(tmp_path)
    bot = MagicMock()
    bot.process = None
    bot.config.name = "b1"

    with patch("codebot.process_manager.update_bot_state"):
        with patch.object(time, "time", return_value=9999.0):
            safe_stop_all({"b1": bot}, lambda b, r: None)

    status = drain_status()
    assert status["draining"] is True
    assert status["drain_reason"] is not None
    assert "safe-stop" in status["drain_reason"]


def test_safe_stop_all_empty_bots_Given_no_bots_When_safe_stop_all_Then_drain_set_and_empty_result(tmp_path: Path) -> None:
    """Given an empty bots dict
    When safe_stop_all() is called
    Then it still sets drain and returns an empty dict."""
    _isolated_config(tmp_path)
    with patch("codebot.process_manager.update_bot_state"):
        result = safe_stop_all({}, lambda b, r: None)
    assert result == {}
    assert is_draining() is True


def test_safe_stop_all_all_running_Given_all_running_When_safe_stop_all_Then_all_stopped(tmp_path: Path) -> None:
    """Given all bots running
    When safe_stop_all() is called
    Then all are stopped and marked drained."""
    _isolated_config(tmp_path)
    bots: dict[str, object] = {}
    for name in ("a", "b", "c"):
        bot = MagicMock()
        bot.process = MagicMock()
        bot.process.poll.return_value = None
        bot.config.name = name
        bots[name] = bot

    with patch("codebot.process_manager.update_bot_state") as mock_update:
        result = safe_stop_all(bots, lambda b, r: None)

    for v in result.values():
        assert v == "stopped"
    assert mock_update.call_count == 3
    for call in mock_update.call_args_list:
        assert call.args[1] == "drained"
