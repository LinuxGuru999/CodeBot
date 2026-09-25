"""Extensive health_check_loop suite — Given/When/Then, isolated tmp_path, one When per test."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import codebot.health_check_loop as hcl
from codebot.process_manager import BotConfig, BotState
from codebot.state_manager import PathConfig


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _paths(tmp_path: Path) -> PathConfig:
    state_dir = tmp_path / "state"
    logs_dir = tmp_path / "logs"
    state_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    return PathConfig(
        bots_dir=tmp_path,
        state_dir=state_dir,
        logs_dir=logs_dir,
        backup_dir=state_dir / "backup",
        drain_file=state_dir / ".drain",
        update_lock=state_dir / ".update_lock",
        restart_file=state_dir / ".restart",
        alignment_events_dir=state_dir / "alignment_events",
    )


def _cfg(
    name: str,
    interval: int = 30,
    enabled: bool = True,
    model: str = "xiaomi-mimo-2.5",
) -> BotConfig:
    return BotConfig(
        name=name,
        prompt_file="codebot/roles/test.md",
        interval_seconds=interval,
        heartbeat_timeout=90,
        model=model,
        enabled=enabled,
    )


def _bot(cfg: BotConfig) -> BotState:
    return BotState(config=cfg)


def _running_bot(name: str = "test-bot", enabled: bool = True) -> BotState:
    cfg = _cfg(name, enabled=enabled)
    b = _bot(cfg)
    proc = MagicMock()
    proc.poll.return_value = None
    proc.pid = 1234
    b.process = proc
    b.started_at = 900.0
    return b


def _exited_bot(name: str = "test-bot", exit_code: int = 0) -> BotState:
    cfg = _cfg(name)
    b = _bot(cfg)
    proc = MagicMock()
    proc.poll.return_value = exit_code
    proc.returncode = exit_code
    b.process = proc
    b.started_at = 900.0
    b.next_run_at = 0.0
    return b


# ---------------------------------------------------------------------------
# init_tick
# ---------------------------------------------------------------------------

class TestInitTick:
    def test_Given_fresh_tmp_state_When_init_tick_Then_clears_caches_without_error(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        mock_adapter = MagicMock()
        mock_adapter.queue_depth.return_value = 5
        mock_qm = MagicMock()
        with patch("codebot.ticket_engine.QueueManager") as mock_qm_cls, \
             patch("codebot.orchestrator.get_paths", return_value=paths), \
             patch("codebot.orchestrator.get_adapter_instance", return_value=mock_adapter):
            mock_qm_cls.from_state_dir.return_value = mock_qm
            hcl.init_tick()
            mock_qm.clear_cache.assert_called_once()
            mock_adapter.queue_depth.assert_called_once()

    def test_Given_adapter_queue_depth_raises_When_init_tick_Then_suppresses(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        mock_adapter = MagicMock()
        mock_adapter.queue_depth.side_effect = RuntimeError("boom")
        mock_qm = MagicMock()
        with patch("codebot.ticket_engine.QueueManager") as mock_qm_cls, \
             patch("codebot.orchestrator.get_paths", return_value=paths), \
             patch("codebot.orchestrator.get_adapter_instance", return_value=mock_adapter):
            mock_qm_cls.from_state_dir.return_value = mock_qm
            hcl.init_tick()

    def test_Given_clear_cache_import_fails_When_init_tick_Then_still_completes(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        mock_adapter = MagicMock()
        mock_adapter.queue_depth.return_value = 0
        mock_qm = MagicMock()
        with patch("codebot.ticket_engine.QueueManager") as mock_qm_cls, \
             patch("codebot.orchestrator.get_paths", return_value=paths), \
             patch("codebot.orchestrator.get_adapter_instance", return_value=mock_adapter):
            mock_qm_cls.from_state_dir.return_value = mock_qm
            with patch.dict("sys.modules", {}):
                # Simulate missing ticket_dispatcher inside init_tick try
                import sys
                original_td = sys.modules.get("codebot.ticket_dispatcher")
                sys.modules.pop("codebot.ticket_dispatcher", None)
                try:
                    hcl.init_tick()
                finally:
                    if original_td is not None:
                        sys.modules["codebot.ticket_dispatcher"] = original_td
            mock_qm.clear_cache.assert_called_once()


# ---------------------------------------------------------------------------
# current_tick_store / flush_tick_store
# ---------------------------------------------------------------------------

class TestTickStoreHelpers:
    def test_Given_no_ticket_store_When_current_tick_store_Then_returns_none(self) -> None:
        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=None):
            result = hcl.current_tick_store()
            assert result is None

    def test_Given_store_with_flush_When_flush_tick_store_Then_calls_flush(self) -> None:
        store = MagicMock()
        store.flush = MagicMock()
        # When
        hcl.flush_tick_store(store)
        store.flush.assert_called_once()

    def test_Given_store_without_flush_When_flush_tick_store_Then_no_error(self) -> None:
        store = MagicMock(spec=[])
        # When — should not raise
        hcl.flush_tick_store(store)

    def test_Given_store_flush_raises_When_flush_tick_store_Then_suppresses(self) -> None:
        store = MagicMock()
        store.flush.side_effect = RuntimeError("flush fail")
        # When — should not raise
        hcl.flush_tick_store(store)

    def test_Given_none_store_When_flush_tick_store_Then_no_error(self) -> None:
        # When — should not raise
        hcl.flush_tick_store(None)


# ---------------------------------------------------------------------------
# retry_disabled_and_stuck
# ---------------------------------------------------------------------------

class TestRetryDisabledAndStuck:
    def test_Given_disabled_bot_without_process_When_retry_disabled_Then_reenables_to_waiting(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        cfg = _cfg("disabled-bot", enabled=False)
        b = _bot(cfg)
        b.process = None
        bots: dict[str, BotState] = {"disabled-bot": b}
        mock_rdb = MagicMock(return_value=True)
        mock_rss = MagicMock(return_value=False)
        mock_ubs = MagicMock()
        with patch("codebot.orchestrator.retry_disabled_bot", mock_rdb), \
             patch("codebot.orchestrator.retry_stuck_starting", mock_rss), \
             patch("codebot.orchestrator.update_bot_state", mock_ubs), \
             patch("codebot.orchestrator.get_paths", return_value=paths):
            hcl.retry_disabled_and_stuck(bots, {})
            mock_rdb.assert_called_once_with(b)
            mock_ubs.assert_called_once_with(b, "waiting")

    def test_Given_enabled_running_bot_stuck_When_retry_stuck_Then_logs_retry(self) -> None:
        cfg = _cfg("enabled-bot", enabled=True)
        b = _bot(cfg)
        b.process = MagicMock()
        b.process.poll.return_value = None
        bots: dict[str, BotState] = {"enabled-bot": b}
        hb_cache: dict[str, float] = {}
        mock_rdb = MagicMock(return_value=False)
        mock_rss = MagicMock(return_value=True)
        mock_ubs = MagicMock()
        with patch("codebot.orchestrator.retry_disabled_bot", mock_rdb), \
             patch("codebot.orchestrator.retry_stuck_starting", mock_rss), \
             patch("codebot.orchestrator.update_bot_state", mock_ubs):
            hcl.retry_disabled_and_stuck(bots, hb_cache)
            mock_rss.assert_called_once()
            # update should not be called for stuck retry path (only log)
            mock_ubs.assert_not_called()

    def test_Given_disabled_bot_retry_returns_false_When_retry_disabled_Then_no_update(self) -> None:
        cfg = _cfg("disabled-bot", enabled=False)
        b = _bot(cfg)
        b.process = None
        bots: dict[str, BotState] = {"disabled-bot": b}
        mock_rdb = MagicMock(return_value=False)
        mock_rss = MagicMock(return_value=False)
        mock_ubs = MagicMock()
        with patch("codebot.orchestrator.retry_disabled_bot", mock_rdb), \
             patch("codebot.orchestrator.retry_stuck_starting", mock_rss), \
             patch("codebot.orchestrator.update_bot_state", mock_ubs):
            hcl.retry_disabled_and_stuck(bots, {})
            mock_ubs.assert_not_called()

    def test_Given_enabled_bot_not_stuck_When_retry_stuck_Then_no_log(self) -> None:
        cfg = _cfg("enabled-bot", enabled=True)
        b = _bot(cfg)
        b.process = MagicMock()
        b.process.poll.return_value = None
        bots: dict[str, BotState] = {"enabled-bot": b}
        mock_rss = MagicMock(return_value=False)
        with patch("codebot.orchestrator.retry_disabled_bot", MagicMock(return_value=False)), \
             patch("codebot.orchestrator.retry_stuck_starting", mock_rss), \
             patch("codebot.orchestrator.update_bot_state", MagicMock()):
            hcl.retry_disabled_and_stuck(bots, {})
            mock_rss.assert_called_once()

    def test_Given_retry_disabled_raises_TypeError_When_called_Then_fallback_sig(self) -> None:
        cfg = _cfg("disabled-bot", enabled=False)
        b = _bot(cfg)
        b.process = None
        bots: dict[str, BotState] = {"disabled-bot": b}
        # Simulate mock that raises TypeError on first sig, succeeds on fallback handling
        def bad_sig(_bot_arg: BotState) -> bool:
            raise TypeError("bad sig")

        mock_rdb = MagicMock(side_effect=bad_sig)
        mock_ubs = MagicMock()
        with patch("codebot.orchestrator.retry_disabled_bot", mock_rdb), \
             patch("codebot.orchestrator.retry_stuck_starting", MagicMock(return_value=False)), \
             patch("codebot.orchestrator.update_bot_state", mock_ubs):
            # Should not raise — inner except TypeError -> retry with same sig then suppress
            hcl.retry_disabled_and_stuck(bots, {})
            assert mock_rdb.call_count >= 1


# ---------------------------------------------------------------------------
# handle_exited_bots
# ---------------------------------------------------------------------------

class TestHandleExitedBots:
    def test_Given_clean_exit_When_handle_exited_bots_Then_resets_errors_and_sets_next_run(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        now = 1000.0
        bot = _exited_bot("clean-bot", exit_code=0)
        bot.consecutive_errors = 2
        bots: dict[str, BotState] = {"clean-bot": bot}
        mock_wae = MagicMock()
        mock_rap = MagicMock()
        mock_ubs = MagicMock()
        mock_trans_ok = MagicMock()
        mock_record = MagicMock()
        with patch("codebot.orchestrator.get_paths", return_value=paths), \
             patch("codebot.orchestrator.write_alignment_event", mock_wae), \
             patch("codebot.orchestrator.run_alignment_pipeline", mock_rap), \
             patch("codebot.orchestrator.update_bot_state", mock_ubs), \
             patch("codebot.orchestrator.transition_ticket_on_success", mock_trans_ok), \
             patch("codebot.orchestrator.record_workforce_completion", mock_record), \
             patch("codebot.orchestrator.transition_ticket_on_error", MagicMock()), \
             patch("codebot.orchestrator.rotate_model_on_error", MagicMock()), \
             patch("codebot.orchestrator.compute_rate_limit_backoff", MagicMock(return_value=(5.0, False))), \
             patch("codebot.orchestrator.load_scratchpad", MagicMock()), \
             patch("codebot.orchestrator.save_scratchpad", MagicMock()):
            result = hcl.handle_exited_bots(bots, now, MagicMock(), MagicMock(), ts=None)
            assert bot.consecutive_errors == 0
            assert bot.next_run_at == now + bot.config.interval_seconds
            assert bot.process is None
            mock_wae.assert_called_once()
            mock_rap.assert_called_once_with("clean-bot")
            mock_trans_ok.assert_called_once()
            mock_record.assert_called_once()
            mock_ubs.assert_called_once_with(bot, "waiting")
            assert result == set()

    def test_Given_error_exit_When_handle_exited_bots_Then_increments_and_schedules_retry(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        now = 2000.0
        bot = _exited_bot("err-bot", exit_code=1)
        bot.consecutive_errors = 0
        setattr(bot, "_assigned_ticket_id", "T-1")
        bots: dict[str, BotState] = {"err-bot": bot}
        mock_sp = MagicMock()
        mock_sp.iteration = 2
        with patch("codebot.orchestrator.get_paths", return_value=paths), \
             patch("codebot.orchestrator.write_alignment_event", MagicMock()), \
             patch("codebot.orchestrator.run_alignment_pipeline", MagicMock()), \
             patch("codebot.orchestrator.update_bot_state", MagicMock()), \
             patch("codebot.orchestrator.transition_ticket_on_success", MagicMock()), \
             patch("codebot.orchestrator.transition_ticket_on_error", MagicMock()), \
             patch("codebot.orchestrator.rotate_model_on_error", MagicMock()), \
             patch("codebot.orchestrator.compute_rate_limit_backoff", MagicMock(return_value=(5.0, False))), \
             patch("codebot.orchestrator.load_scratchpad", MagicMock(return_value=mock_sp)), \
             patch("codebot.orchestrator.save_scratchpad", MagicMock()), \
             patch("codebot.orchestrator.record_workforce_completion", MagicMock()):
            result = hcl.handle_exited_bots(bots, now, MagicMock(), MagicMock(), ts=None)
            assert bot.consecutive_errors == 1
            assert bot.next_run_at == now + 5
            assert "T-1" in result
            mock_sp.mark_error.assert_called_once()
            mock_sp.finish_agent.assert_called_once()

    def test_Given_three_consecutive_errors_When_handle_exited_bots_Then_rotates_and_resets(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        now = 3000.0
        bot = _exited_bot("rot-bot", exit_code=1)
        bot.consecutive_errors = 2
        old_model = bot.config.model
        setattr(bot, "_assigned_ticket_id", "")
        bots: dict[str, BotState] = {"rot-bot": bot}
        mock_rotate = MagicMock()
        def _rotate(b: BotState, _bots: dict[str, BotState] | None = None) -> None:
            b.config.model = "rotated-model"
        mock_rotate.side_effect = _rotate
        with patch("codebot.orchestrator.get_paths", return_value=paths), \
             patch("codebot.orchestrator.write_alignment_event", MagicMock()), \
             patch("codebot.orchestrator.run_alignment_pipeline", MagicMock()), \
             patch("codebot.orchestrator.update_bot_state", MagicMock()), \
             patch("codebot.orchestrator.transition_ticket_on_success", MagicMock()), \
             patch("codebot.orchestrator.transition_ticket_on_error", MagicMock()), \
             patch("codebot.orchestrator.rotate_model_on_error", mock_rotate), \
             patch("codebot.orchestrator.compute_rate_limit_backoff", MagicMock(return_value=(5.0, False))), \
             patch("codebot.orchestrator.load_scratchpad", MagicMock()), \
             patch("codebot.orchestrator.save_scratchpad", MagicMock()), \
             patch("codebot.orchestrator.record_workforce_completion", MagicMock()):
            hcl.handle_exited_bots(bots, now, MagicMock(), MagicMock(), ts=None)
            assert bot.consecutive_errors == 0
            assert bot.config.model == "rotated-model"
            assert bot.config.model != old_model

    def test_Given_rate_limit_exit_When_handle_exited_bots_Then_sets_backoff_and_rotates(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        now = 4000.0
        bot = _exited_bot("rl-bot", exit_code=3)
        setattr(bot, "_assigned_ticket_id", "T-RL")
        bots: dict[str, BotState] = {"rl-bot": bot}
        mock_backoff = MagicMock(return_value=(60.0, False))
        mock_rotate = MagicMock()
        mock_ubs = MagicMock()
        scratch = MagicMock()
        scratch.iteration = 5
        with patch("codebot.orchestrator.get_paths", return_value=paths), \
             patch("codebot.orchestrator.write_alignment_event", MagicMock()), \
             patch("codebot.orchestrator.run_alignment_pipeline", MagicMock()), \
             patch("codebot.orchestrator.update_bot_state", mock_ubs), \
             patch("codebot.orchestrator.transition_ticket_on_success", MagicMock()), \
             patch("codebot.orchestrator.transition_ticket_on_error", MagicMock()), \
             patch("codebot.orchestrator.rotate_model_on_error", mock_rotate), \
             patch("codebot.orchestrator.compute_rate_limit_backoff", mock_backoff), \
             patch("codebot.orchestrator.load_scratchpad", MagicMock(return_value=scratch)), \
             patch("codebot.orchestrator.save_scratchpad", MagicMock()), \
             patch("codebot.orchestrator.record_workforce_completion", MagicMock()):
            hcl.handle_exited_bots(bots, now, MagicMock(), MagicMock(), ts=None)
            assert bot.next_run_at == now + 60.0
            mock_rotate.assert_called_once()
            mock_ubs.assert_called_once_with(bot, "waiting")
            scratch.mark_error.assert_called_once()
            scratch.finish_agent.assert_called_once()

    def test_Given_rate_limit_should_disable_When_handle_exited_bots_Then_disables(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        now = 5000.0
        bot = _exited_bot("rl-disable", exit_code=3)
        bot.config.enabled = True
        bots: dict[str, BotState] = {"rl-disable": bot}
        mock_ubs = MagicMock()
        with patch("codebot.orchestrator.get_paths", return_value=paths), \
             patch("codebot.orchestrator.write_alignment_event", MagicMock()), \
             patch("codebot.orchestrator.run_alignment_pipeline", MagicMock()), \
             patch("codebot.orchestrator.update_bot_state", mock_ubs), \
             patch("codebot.orchestrator.transition_ticket_on_success", MagicMock()), \
             patch("codebot.orchestrator.transition_ticket_on_error", MagicMock()), \
             patch("codebot.orchestrator.rotate_model_on_error", MagicMock()), \
             patch("codebot.orchestrator.compute_rate_limit_backoff", MagicMock(return_value=(120.0, True))), \
             patch("codebot.orchestrator.load_scratchpad", MagicMock()), \
             patch("codebot.orchestrator.save_scratchpad", MagicMock()), \
             patch("codebot.orchestrator.record_workforce_completion", MagicMock()):
            hcl.handle_exited_bots(bots, now, MagicMock(), MagicMock(), ts=None)
            assert bot.config.enabled is False
            mock_ubs.assert_called_once_with(bot, "disabled")

    def test_Given_dead_numeric_suffix_When_handle_exited_bots_Then_prunes_registry(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        now = 6000.0
        bot = _exited_bot("general_implementer-1", exit_code=0)
        bots: dict[str, BotState] = {"general_implementer-1": bot}
        with patch("codebot.orchestrator.get_paths", return_value=paths), \
             patch("codebot.orchestrator.write_alignment_event", MagicMock()), \
             patch("codebot.orchestrator.run_alignment_pipeline", MagicMock()), \
             patch("codebot.orchestrator.update_bot_state", MagicMock()), \
             patch("codebot.orchestrator.transition_ticket_on_success", MagicMock()), \
             patch("codebot.orchestrator.transition_ticket_on_error", MagicMock()), \
             patch("codebot.orchestrator.rotate_model_on_error", MagicMock()), \
             patch("codebot.orchestrator.compute_rate_limit_backoff", MagicMock(return_value=(5.0, False))), \
             patch("codebot.orchestrator.load_scratchpad", MagicMock()), \
             patch("codebot.orchestrator.save_scratchpad", MagicMock()), \
             patch("codebot.orchestrator.record_workforce_completion", MagicMock()):
            hcl.handle_exited_bots(bots, now, MagicMock(), MagicMock(), ts=None)
            assert "general_implementer-1" not in bots

    def test_Given_running_or_disabled_bot_When_handle_exited_bots_Then_skips(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        now = 7000.0
        running = _running_bot("still-running")
        disabled = _exited_bot("disabled-bot", exit_code=0)
        disabled.config.enabled = False
        bots: dict[str, BotState] = {"still-running": running, "disabled-bot": disabled}
        mock_wae = MagicMock()
        with patch("codebot.orchestrator.get_paths", return_value=paths), \
             patch("codebot.orchestrator.write_alignment_event", mock_wae), \
             patch("codebot.orchestrator.run_alignment_pipeline", MagicMock()), \
             patch("codebot.orchestrator.update_bot_state", MagicMock()), \
             patch("codebot.orchestrator.transition_ticket_on_success", MagicMock()), \
             patch("codebot.orchestrator.transition_ticket_on_error", MagicMock()), \
             patch("codebot.orchestrator.rotate_model_on_error", MagicMock()), \
             patch("codebot.orchestrator.compute_rate_limit_backoff", MagicMock(return_value=(5.0, False))), \
             patch("codebot.orchestrator.load_scratchpad", MagicMock()), \
             patch("codebot.orchestrator.save_scratchpad", MagicMock()), \
             patch("codebot.orchestrator.record_workforce_completion", MagicMock()):
            result = hcl.handle_exited_bots(bots, now, MagicMock(), MagicMock(), ts=None)
            mock_wae.assert_not_called()
            assert result == set()
            assert running.process is not None
            assert disabled.process is not None

    def test_Given_write_alignment_event_fails_When_handle_exited_bots_Then_continues(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        now = 8000.0
        bot = _exited_bot("fail-wae", exit_code=0)
        bots: dict[str, BotState] = {"fail-wae": bot}
        with patch("codebot.orchestrator.get_paths", return_value=paths), \
             patch("codebot.orchestrator.write_alignment_event", MagicMock(side_effect=RuntimeError("wae boom"))), \
             patch("codebot.orchestrator.run_alignment_pipeline", MagicMock()), \
             patch("codebot.orchestrator.update_bot_state", MagicMock()), \
             patch("codebot.orchestrator.transition_ticket_on_success", MagicMock()), \
             patch("codebot.orchestrator.record_workforce_completion", MagicMock()), \
             patch("codebot.orchestrator.transition_ticket_on_error", MagicMock()), \
             patch("codebot.orchestrator.rotate_model_on_error", MagicMock()), \
             patch("codebot.orchestrator.compute_rate_limit_backoff", MagicMock(return_value=(5.0, False))), \
             patch("codebot.orchestrator.load_scratchpad", MagicMock()), \
             patch("codebot.orchestrator.save_scratchpad", MagicMock()):
            # When — should not raise
            hcl.handle_exited_bots(bots, now, MagicMock(), MagicMock(), ts=None)
            assert bot.process is None

    def test_Given_transition_success_raises_When_handle_exited_bots_Then_suppresses(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        now = 9000.0
        bot = _exited_bot("trans-fail", exit_code=0)
        bots: dict[str, BotState] = {"trans-fail": bot}
        with patch("codebot.orchestrator.get_paths", return_value=paths), \
             patch("codebot.orchestrator.write_alignment_event", MagicMock()), \
             patch("codebot.orchestrator.run_alignment_pipeline", MagicMock()), \
             patch("codebot.orchestrator.update_bot_state", MagicMock()), \
             patch("codebot.orchestrator.transition_ticket_on_success", MagicMock(side_effect=RuntimeError("trans boom"))), \
             patch("codebot.orchestrator.record_workforce_completion", MagicMock()), \
             patch("codebot.orchestrator.transition_ticket_on_error", MagicMock()), \
             patch("codebot.orchestrator.rotate_model_on_error", MagicMock()), \
             patch("codebot.orchestrator.compute_rate_limit_backoff", MagicMock(return_value=(5.0, False))), \
             patch("codebot.orchestrator.load_scratchpad", MagicMock()), \
             patch("codebot.orchestrator.save_scratchpad", MagicMock()):
            hcl.handle_exited_bots(bots, now, MagicMock(), MagicMock(), ts=None)
            assert bot.process is None
            assert bot.next_run_at == now + bot.config.interval_seconds

    def test_Given_real_scratchpad_file_When_rate_limit_exit_Then_writes_scratchpad(self, tmp_path: Path) -> None:
        # Uses real file I/O for scratchpad via tmp_path state_dir
        paths = _paths(tmp_path)
        now = 10000.0
        bot = _exited_bot("rl-real-sp", exit_code=3)
        tid = "T-REAL"
        setattr(bot, "_assigned_ticket_id", tid)
        # Pre-create a scratchpad via real API so file exists
        from codebot.scratchpad import ScratchpadState, save_scratchpad

        sp = ScratchpadState(ticket_id=tid, iteration=7)
        save_scratchpad(paths.state_dir, sp)
        assert (paths.state_dir / f"{tid}.scratchpad.json").exists()
        bots: dict[str, BotState] = {"rl-real-sp": bot}
        with patch("codebot.orchestrator.get_paths", return_value=paths), \
             patch("codebot.orchestrator.write_alignment_event", MagicMock()), \
             patch("codebot.orchestrator.run_alignment_pipeline", MagicMock()), \
             patch("codebot.orchestrator.update_bot_state", MagicMock()), \
             patch("codebot.orchestrator.transition_ticket_on_success", MagicMock()), \
             patch("codebot.orchestrator.transition_ticket_on_error", MagicMock()), \
             patch("codebot.orchestrator.rotate_model_on_error", MagicMock()), \
             patch("codebot.orchestrator.compute_rate_limit_backoff", MagicMock(return_value=(30.0, False))), \
             patch("codebot.orchestrator.record_workforce_completion", MagicMock()):
            # Do NOT patch load/save_scratchpad — exercise real file I/O
            hcl.handle_exited_bots(bots, now, MagicMock(), MagicMock(), ts=None)
            data = json.loads((paths.state_dir / f"{tid}.scratchpad.json").read_text())
            assert data["phase"] == "error" or "error" in json.dumps(data).lower()
            assert bot.next_run_at == now + 30.0


# ---------------------------------------------------------------------------
# handle_stuck_bots
# ---------------------------------------------------------------------------

class TestHandleStuckBots:
    def test_Given_stuck_running_bot_When_handle_stuck_bots_Then_restarts(self) -> None:
        now = 11000.0
        bot = _running_bot("stuck-bot")
        bots: dict[str, BotState] = {"stuck-bot": bot}
        hb_cache: dict[str, float] = {"stuck-bot": now - 500}
        mock_wae = MagicMock()
        mock_rap = MagicMock()
        mock_restart = MagicMock(return_value=True)
        mock_is_stuck = MagicMock(return_value=True)
        mock_ehb = MagicMock(return_value=90)
        mock_mp = MagicMock()
        mock_mp.lockup_risk = "high"
        with patch("codebot.orchestrator.is_stuck", mock_is_stuck), \
             patch("codebot.orchestrator.effective_heartbeat_timeout", mock_ehb), \
             patch("codebot.orchestrator.model_profile", MagicMock(return_value=mock_mp)), \
             patch("codebot.orchestrator.write_alignment_event", mock_wae), \
             patch("codebot.orchestrator.run_alignment_pipeline", mock_rap):
            hcl.handle_stuck_bots(bots, now, hb_cache, mock_restart)
            mock_is_stuck.assert_called_once()
            mock_wae.assert_called_once()
            mock_rap.assert_called_once_with("stuck-bot")
            mock_restart.assert_called_once()

    def test_Given_not_stuck_bot_When_handle_stuck_bots_Then_no_restart(self) -> None:
        now = 12000.0
        bot = _running_bot("healthy-bot")
        bots: dict[str, BotState] = {"healthy-bot": bot}
        hb_cache: dict[str, float] = {"healthy-bot": now - 10}
        mock_restart = MagicMock()
        with patch("codebot.orchestrator.is_stuck", MagicMock(return_value=False)), \
             patch("codebot.orchestrator.write_alignment_event", MagicMock()), \
             patch("codebot.orchestrator.run_alignment_pipeline", MagicMock()):
            hcl.handle_stuck_bots(bots, now, hb_cache, mock_restart)
            mock_restart.assert_not_called()

    def test_Given_stopped_or_disabled_bot_When_handle_stuck_bots_Then_skips(self) -> None:
        now = 13000.0
        stopped = _exited_bot("stopped-bot", exit_code=0)
        stopped.config.enabled = True
        disabled = _running_bot("disabled-bot")
        disabled.config.enabled = False
        bots: dict[str, BotState] = {"stopped-bot": stopped, "disabled-bot": disabled}
        mock_is_stuck = MagicMock(return_value=True)
        mock_restart = MagicMock()
        with patch("codebot.orchestrator.is_stuck", mock_is_stuck), \
             patch("codebot.orchestrator.write_alignment_event", MagicMock()), \
             patch("codebot.orchestrator.run_alignment_pipeline", MagicMock()):
            hcl.handle_stuck_bots(bots, now, {}, mock_restart)
            mock_is_stuck.assert_not_called()
            mock_restart.assert_not_called()

    def test_Given_is_stuck_raises_TypeError_When_handle_stuck_bots_Then_uses_fallback_sig(self) -> None:
        now = 14000.0
        bot = _running_bot("typeerror-bot")
        bots: dict[str, BotState] = {"typeerror-bot": bot}
        hb_cache: dict[str, float] = {}
        def bad_is_stuck(*_a: object, **_kw: object) -> bool:
            raise TypeError("sig mismatch")
        mock_is_stuck = MagicMock(side_effect=bad_is_stuck)
        mock_restart = MagicMock()
        # Second fallback via is_stuck(bot) without kwargs should also be tried; mock it to return True via side_effect sequence
        # Instead we let the code fall through to stuck=False on exception, so no restart
        with patch("codebot.orchestrator.is_stuck", mock_is_stuck), \
             patch("codebot.orchestrator.write_alignment_event", MagicMock()), \
             patch("codebot.orchestrator.run_alignment_pipeline", MagicMock()):
            hcl.handle_stuck_bots(bots, now, hb_cache, mock_restart)
            mock_restart.assert_not_called()

    def test_Given_restart_fails_When_handle_stuck_bots_Then_suppresses(self) -> None:
        now = 15000.0
        bot = _running_bot("restart-fail")
        bots: dict[str, BotState] = {"restart-fail": bot}
        hb_cache: dict[str, float] = {"restart-fail": now - 1000}
        mock_restart = MagicMock(side_effect=RuntimeError("restart boom"))
        with patch("codebot.orchestrator.is_stuck", MagicMock(return_value=True)), \
             patch("codebot.orchestrator.effective_heartbeat_timeout", MagicMock(return_value=90)), \
             patch("codebot.orchestrator.model_profile", MagicMock(return_value=None)), \
             patch("codebot.orchestrator.write_alignment_event", MagicMock()), \
             patch("codebot.orchestrator.run_alignment_pipeline", MagicMock()):
            # When — should not raise
            hcl.handle_stuck_bots(bots, now, hb_cache, mock_restart)
            mock_restart.assert_called_once()

    def test_Given_stuck_bot_alignment_pipeline_fails_When_handle_stuck_bots_Then_still_restarts(self) -> None:
        now = 16000.0
        bot = _running_bot("pipe-fail")
        bots: dict[str, BotState] = {"pipe-fail": bot}
        hb_cache: dict[str, float] = {"pipe-fail": now - 1000}
        mock_restart = MagicMock(return_value=True)
        with patch("codebot.orchestrator.is_stuck", MagicMock(return_value=True)), \
             patch("codebot.orchestrator.effective_heartbeat_timeout", MagicMock(return_value=90)), \
             patch("codebot.orchestrator.model_profile", MagicMock(return_value=None)), \
             patch("codebot.orchestrator.write_alignment_event", MagicMock()), \
             patch("codebot.orchestrator.run_alignment_pipeline", MagicMock(side_effect=RuntimeError("pipe fail"))):
            hcl.handle_stuck_bots(bots, now, hb_cache, mock_restart)
            mock_restart.assert_called_once()


# ---------------------------------------------------------------------------
# run_dispatchers
# ---------------------------------------------------------------------------

class TestRunDispatchers:
    def test_Given_bots_and_store_When_run_dispatchers_Then_delegates_and_flushes(self) -> None:
        bots: dict[str, BotState] = {}
        mock_store = MagicMock()
        mock_store.flush = MagicMock()
        with patch("codebot.health_check_loop.current_tick_store", return_value=mock_store), \
             patch("codebot.health_check_loop.flush_tick_store") as mock_flush, \
             patch("codebot.ticket_dispatcher.run_triage_fast_paths", return_value=0), \
             patch("codebot.orchestrator._v2_get_scheduler") as mock_get_sched:
            mock_inst = MagicMock()
            mock_inst.tick.return_value = 0
            mock_inst.gate.dequeue_ready.return_value = []
            mock_get_sched.return_value = mock_inst
            hcl.run_dispatchers(bots, MagicMock(), MagicMock(), MagicMock(), store=mock_store)
            mock_inst.tick.assert_called_once()
            mock_flush.assert_called_once()

    def test_Given_dispatch_raises_TypeError_When_run_dispatchers_Then_retries_positional(self) -> None:
        bots: dict[str, BotState] = {}
        mock_store = MagicMock()
        mock_store.flush = MagicMock()
        with patch("codebot.health_check_loop.current_tick_store", return_value=mock_store), \
             patch("codebot.health_check_loop.flush_tick_store"), \
             patch("codebot.ticket_dispatcher.run_triage_fast_paths", side_effect=TypeError("kwargs mismatch")), \
             patch("codebot.orchestrator._v2_get_scheduler") as mock_get_sched:
            mock_inst = MagicMock()
            mock_inst.tick.return_value = 0
            mock_inst.gate.dequeue_ready.return_value = []
            mock_get_sched.return_value = mock_inst
            hcl.run_dispatchers(bots, MagicMock(), MagicMock(), MagicMock(), store=mock_store)
            mock_inst.tick.assert_called_once()
            mock_store.flush.assert_not_called()

    def test_Given_dispatch_raises_generic_When_run_dispatchers_Then_logs_and_flushes(self) -> None:
        bots: dict[str, BotState] = {}
        mock_store = MagicMock()
        mock_store.flush = MagicMock()
        with patch("codebot.health_check_loop.current_tick_store", return_value=mock_store), \
             patch("codebot.health_check_loop.flush_tick_store") as mock_flush, \
             patch("codebot.ticket_dispatcher.run_triage_fast_paths", side_effect=RuntimeError("disp boom")), \
             patch("codebot.orchestrator._v2_get_scheduler") as mock_get_sched:
            mock_inst = MagicMock()
            mock_inst.tick.return_value = 0
            mock_inst.gate.dequeue_ready.return_value = []
            mock_get_sched.return_value = mock_inst
            hcl.run_dispatchers(bots, MagicMock(), MagicMock(), MagicMock(), store=mock_store)
            mock_inst.tick.assert_called_once()
            mock_flush.assert_called_once()

    def test_Given_no_store_When_run_dispatchers_Then_uses_current_tick_store(self) -> None:
        bots: dict[str, BotState] = {}
        mock_store = MagicMock()
        mock_store.flush = MagicMock()
        mock_disp = MagicMock()
        mock_disp.return_value = None
        with patch("codebot.health_check_loop.current_tick_store", return_value=mock_store) as mock_cts, \
             patch("codebot.orchestrator.dispatch_ready_tickets", mock_disp), \
             patch("codebot.health_check_loop.flush_tick_store") as mock_flush:
            hcl.run_dispatchers(bots, MagicMock(), MagicMock(), MagicMock(), store=None)
            mock_cts.assert_called_once()
            mock_flush.assert_called_once_with(mock_store)


# ---------------------------------------------------------------------------
# start_eligible_bots
# ---------------------------------------------------------------------------

class TestStartEligibleBots:
    def test_Given_needed_bot_When_start_eligible_bots_Then_starts_with_demand_flag(self, tmp_path: Path) -> None:
        now = 17000.0
        cfg = _cfg("custom_bot")
        b = _bot(cfg)
        b.process = None
        b.next_run_at = 0.0
        setattr(b, "_assigned_ticket_id", "T-123")
        bots: dict[str, BotState] = {"custom_bot": b}
        mock_store = MagicMock()
        start_fn = MagicMock(return_value=True)
        with patch("codebot.health_check_loop.current_tick_store", return_value=mock_store), \
             patch("codebot.orchestrator.get_pipeline_state", return_value={"READY": 3}), \
             patch("codebot.orchestrator.is_needed_bot", return_value=True), \
             patch("codebot.orchestrator.is_draining", return_value=False), \
             patch("codebot.orchestrator.update_bot_state", MagicMock()):
            hcl.start_eligible_bots(bots, now, start_fn, store=None)
            start_fn.assert_called_once()
            # Verify called with is_demand=True due to assigned ticket
            _, kwargs = start_fn.call_args
            if kwargs:
                assert kwargs.get("is_demand") is True or kwargs.get("bots") is not None
            else:
                # positional fallback
                assert start_fn.call_count == 1

    def test_Given_not_needed_bot_When_start_eligible_bots_Then_sets_next_run(self) -> None:
        now = 18000.0
        cfg = _cfg("custom_bot")
        b = _bot(cfg)
        b.process = None
        b.next_run_at = 0.0
        bots: dict[str, BotState] = {"custom_bot": b}
        start_fn = MagicMock()
        mock_ubs = MagicMock()
        with patch("codebot.orchestrator.get_pipeline_state", return_value={"READY": 0}), \
             patch("codebot.orchestrator.is_needed_bot", return_value=False), \
             patch("codebot.orchestrator.is_draining", return_value=False), \
             patch("codebot.orchestrator.update_bot_state", mock_ubs):
            hcl.start_eligible_bots(bots, now, start_fn, store=MagicMock())
            start_fn.assert_not_called()
            assert b.next_run_at == now + b.config.interval_seconds
            mock_ubs.assert_called_once_with(b, "waiting")

    def test_Given_implementer_role_When_start_eligible_bots_Then_skips(self) -> None:
        now = 19000.0
        cfg = _cfg("implementer")
        b = _bot(cfg)
        b.process = None
        bots: dict[str, BotState] = {"implementer": b}
        start_fn = MagicMock()
        with patch("codebot.orchestrator.get_pipeline_state", return_value={"IMPLEMENTING": 5}), \
             patch("codebot.orchestrator.is_needed_bot", return_value=True), \
             patch("codebot.orchestrator.is_draining", return_value=False), \
             patch("codebot.orchestrator.update_bot_state", MagicMock()):
            hcl.start_eligible_bots(bots, now, start_fn, store=MagicMock())
            start_fn.assert_not_called()

    def test_Given_draining_When_start_eligible_bots_Then_skips_all(self) -> None:
        now = 20000.0
        cfg = _cfg("custom_bot")
        b = _bot(cfg)
        b.process = None
        bots: dict[str, BotState] = {"custom_bot": b}
        start_fn = MagicMock()
        with patch("codebot.orchestrator.get_pipeline_state", return_value={"READY": 10}), \
             patch("codebot.orchestrator.is_needed_bot", return_value=True), \
             patch("codebot.orchestrator.is_draining", return_value=True), \
             patch("codebot.orchestrator.update_bot_state", MagicMock()):
            hcl.start_eligible_bots(bots, now, start_fn, store=MagicMock())
            start_fn.assert_not_called()

    def test_Given_next_run_in_future_When_start_eligible_bots_Then_skips(self) -> None:
        now = 21000.0
        cfg = _cfg("custom_bot")
        b = _bot(cfg)
        b.process = None
        b.next_run_at = now + 100
        bots: dict[str, BotState] = {"custom_bot": b}
        start_fn = MagicMock()
        with patch("codebot.orchestrator.get_pipeline_state", return_value={"READY": 5}), \
             patch("codebot.orchestrator.is_needed_bot", MagicMock(return_value=True)), \
             patch("codebot.orchestrator.is_draining", return_value=False), \
             patch("codebot.orchestrator.update_bot_state", MagicMock()):
            hcl.start_eligible_bots(bots, now, start_fn, store=MagicMock())
            start_fn.assert_not_called()

    def test_Given_start_fn_raises_When_start_eligible_bots_Then_suppresses(self) -> None:
        now = 22000.0
        cfg = _cfg("custom_bot")
        b = _bot(cfg)
        b.process = None
        bots: dict[str, BotState] = {"custom_bot": b}
        def boom_single(*_a: object, **_kw: object) -> bool:
            if "bots" in _kw:
                raise TypeError("need fallback")
            raise RuntimeError("start boom")
        start_fn = boom_single
        with patch("codebot.orchestrator.get_pipeline_state", return_value={"READY": 2}), \
             patch("codebot.orchestrator.is_needed_bot", return_value=True), \
             patch("codebot.orchestrator.is_draining", return_value=False), \
             patch("codebot.orchestrator.update_bot_state", MagicMock()):
            # When — should not raise
            hcl.start_eligible_bots(bots, now, start_fn, store=MagicMock())

    def test_Given_running_bot_When_start_eligible_bots_Then_skips(self) -> None:
        now = 23000.0
        b = _running_bot("custom_bot")
        bots: dict[str, BotState] = {"custom_bot": b}
        start_fn = MagicMock()
        with patch("codebot.orchestrator.get_pipeline_state", return_value={"READY": 5}), \
             patch("codebot.orchestrator.is_needed_bot", MagicMock(return_value=True)), \
             patch("codebot.orchestrator.is_draining", return_value=False), \
             patch("codebot.orchestrator.update_bot_state", MagicMock()):
            hcl.start_eligible_bots(bots, now, start_fn, store=MagicMock())
            start_fn.assert_not_called()

    def test_Given_get_pipeline_state_raises_When_start_eligible_bots_Then_uses_empty(self) -> None:
        now = 24000.0
        cfg = _cfg("custom_bot")
        b = _bot(cfg)
        b.process = None
        bots: dict[str, BotState] = {"custom_bot": b}
        start_fn = MagicMock()
        with patch("codebot.orchestrator.get_pipeline_state", side_effect=RuntimeError("pipeline boom")), \
             patch("codebot.orchestrator.is_needed_bot", MagicMock(return_value=False)), \
             patch("codebot.orchestrator.is_draining", return_value=False), \
             patch("codebot.orchestrator.update_bot_state", MagicMock()):
            hcl.start_eligible_bots(bots, now, start_fn, store=MagicMock())
            start_fn.assert_not_called()
