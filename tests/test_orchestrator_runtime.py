from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebot.orchestrator_runtime import prune_stale_dynamic_bot_state, run_main_loop


def test_main_loop_owns_pid_file_only_while_running(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    paths = MagicMock(state_dir=state_dir)
    pid_present_during_loop: list[bool] = []

    def interrupt_after_observing_pid(_bots: dict[str, object]) -> None:
        pid_present_during_loop.append((state_dir / ".orchestrator.pid").exists())
        raise KeyboardInterrupt

    with patch("codebot.state_manager.get_paths", return_value=paths):
        with pytest.raises(SystemExit):
            run_main_loop(
                {},
                1,
                check_all_bots_fn=interrupt_after_observing_pid,
                stop_bot_fn=MagicMock(),
                run_alignment_pipeline_for_all_fn=MagicMock(),
            )

    assert pid_present_during_loop == [True]
    assert not (state_dir / ".orchestrator.pid").exists()


def test_prune_stale_dynamic_bot_state_removes_only_retained_worker_artifacts(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    now = 1_000.0

    for suffix in ("heartbeat", "state.json", "status.json", "checkpoint.json"):
        (state_dir / f"implementation_planner-7.{suffix}").write_text("0")
        (state_dir / f"implementation_planner.{suffix}").write_text("0")
        (state_dir / f"implementation_planner-8.{suffix}").write_text(str(now))

    removed = prune_stale_dynamic_bot_state(state_dir, now=now)

    assert removed == 4
    assert not list(state_dir.glob("implementation_planner-7.*"))
    assert len(list(state_dir.glob("implementation_planner.*"))) == 4
    assert len(list(state_dir.glob("implementation_planner-8.*"))) == 4
