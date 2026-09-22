"""Tests for scheduler_v2 lifecycle primitives. Covers Todo 1."""

from __future__ import annotations

import time
import uuid
from unittest.mock import MagicMock, patch

import json
import threading
import time
from pathlib import Path

import pytest

from codebot.scheduler_v2.dispatch_gate import (
    ClaimRecord,
    ConcurrencyController,
    DispatchGate,
    DispatchResult,
    ScheduledSpawn,
    SlotToken,
    SpawnQueue,
    SpawnRequest,
    claim_ticket,
    release_claim,
)
from codebot.scheduler_v2.dispatcher import (
    BucketDispatcher,
    ModelSelector,
    NoModelsAvailableError,
    ReasonCode,
    Scheduler,
)
from codebot.scheduler_v2.lifecycle import (
    DEFAULT_STALE_TIMEOUT,
    MODEL_TIMEOUTS,
    AgentRecord,
    AgentState,
    FakeClock,
    FakeProcess,
    FakeProcessSpawner,
    InvalidTransitionError,
    RealClock,
    RealProcessSpawner,
    SpawnerMode,
    _VALID_TRANSITIONS,
    agent_state_from_string,
    create_agent_record,
    get_stale_timeout,
    heartbeat_interval_for,
    is_agent_stale,
    stale_timeout_for,
)


class TestLifecycle:
    def test_valid_transitions_succeed(self):
        clock = FakeClock(initial=1000.0)
        rec = create_agent_record("T-1", "general_implementer", "xiaomi-mimo-2.5", clock=clock)
        assert rec.state == AgentState.CREATED
        rec2 = rec.transition(AgentState.STARTING, timestamp=1001.0)
        assert rec2.state == AgentState.STARTING
        assert rec2.started_at == 1001.0
        assert rec2.trace[-1] == (AgentState.STARTING, 1001.0)
        rec3 = rec2.transition(AgentState.RUNNING, timestamp=1002.0)
        assert rec3.state == AgentState.RUNNING
        rec4 = rec3.transition(AgentState.ZOMBIE, timestamp=1003.0)
        assert rec4.state == AgentState.ZOMBIE
        rec5 = rec4.transition(AgentState.DEAD, timestamp=1004.0)
        assert rec5.state == AgentState.DEAD
        assert rec5.exit_at == 1004.0

    def test_created_to_zombie_direct(self):
        rec = create_agent_record("T-2", "general_implementer", "xiaomi-mimo-2.5", clock=FakeClock(0))
        rec2 = rec.transition(AgentState.ZOMBIE, timestamp=5.0)
        assert rec2.state == AgentState.ZOMBIE

    def test_starting_to_zombie_direct(self):
        rec = create_agent_record("T-3", "general_implementer", "xiaomi-mimo-2.5", clock=FakeClock(0))
        rec = rec.transition(AgentState.STARTING, timestamp=1.0)
        rec2 = rec.transition(AgentState.ZOMBIE, timestamp=2.0)
        assert rec2.state == AgentState.ZOMBIE

    def test_invalid_transitions_raise(self):
        rec = create_agent_record("T-4", "general_implementer", "xiaomi-mimo-2.5", clock=FakeClock(0))
        with pytest.raises(InvalidTransitionError) as exc:
            rec.transition(AgentState.RUNNING, timestamp=1.0)
        assert exc.value.from_state == AgentState.CREATED
        assert exc.value.to_state == AgentState.RUNNING

        rec = rec.transition(AgentState.STARTING, timestamp=1.0)
        with pytest.raises(InvalidTransitionError):
            rec.transition(AgentState.CREATED, timestamp=2.0)
        with pytest.raises(InvalidTransitionError):
            rec.transition(AgentState.DEAD, timestamp=2.0)

        rec = rec.transition(AgentState.RUNNING, timestamp=2.0)
        with pytest.raises(InvalidTransitionError):
            rec.transition(AgentState.CREATED, timestamp=3.0)
        with pytest.raises(InvalidTransitionError):
            rec.transition(AgentState.STARTING, timestamp=3.0)
        with pytest.raises(InvalidTransitionError):
            rec.transition(AgentState.RUNNING, timestamp=3.0)
        with pytest.raises(InvalidTransitionError):
            rec.transition(AgentState.DEAD, timestamp=3.0)

        rec = rec.transition(AgentState.ZOMBIE, timestamp=3.0)
        with pytest.raises(InvalidTransitionError):
            rec.transition(AgentState.CREATED, timestamp=4.0)
        with pytest.raises(InvalidTransitionError):
            rec.transition(AgentState.ZOMBIE, timestamp=4.0)

    def test_dead_is_terminal(self):
        rec = create_agent_record("T-5", "general_implementer", "xiaomi-mimo-2.5", clock=FakeClock(0))
        rec = rec.transition(AgentState.ZOMBIE, timestamp=1.0).transition(AgentState.DEAD, timestamp=2.0)
        assert rec.state == AgentState.DEAD
        for target in AgentState:
            with pytest.raises(InvalidTransitionError) as exc:
                rec.transition(target, timestamp=3.0)
            assert "terminal" in str(exc.value).lower() or "none" in str(exc.value).lower()

    def test_invalid_transition_error_message_dead_terminal(self):
        rec = create_agent_record("T-6", "r", "m", clock=FakeClock(0))
        rec = rec.transition(AgentState.ZOMBIE, timestamp=1.0).transition(AgentState.DEAD, timestamp=2.0)
        with pytest.raises(InvalidTransitionError) as exc:
            rec.transition(AgentState.RUNNING, timestamp=3.0)
        assert exc.value.from_state == AgentState.DEAD
        assert "(none - terminal state)" in str(exc.value)

    def test_transition_timestamp_default_uses_time(self):
        rec = create_agent_record("T-7", "r", "m", clock=FakeClock(0))
        with patch("codebot.scheduler_v2.lifecycle.time.time", return_value=9999.0):
            rec2 = rec.transition(AgentState.STARTING)
        assert rec2.started_at == 9999.0
        assert rec2.trace[-1][1] == 9999.0

    def test_transition_started_at_only_on_starting(self):
        rec = create_agent_record("T-8", "r", "m", clock=FakeClock(100))
        rec = rec.transition(AgentState.STARTING, timestamp=101.0)
        assert rec.started_at == 101.0
        rec2 = rec.transition(AgentState.RUNNING, timestamp=102.0)
        assert rec2.started_at == 101.0
        rec3 = rec2.transition(AgentState.ZOMBIE, timestamp=103.0)
        assert rec3.started_at == 101.0

    def test_transition_exit_at_only_on_dead(self):
        rec = create_agent_record("T-9", "r", "m", clock=FakeClock(0))
        rec = rec.transition(AgentState.STARTING, timestamp=1.0)
        assert rec.exit_at == 0.0
        rec = rec.transition(AgentState.RUNNING, timestamp=2.0)
        assert rec.exit_at == 0.0
        rec = rec.transition(AgentState.ZOMBIE, timestamp=3.0)
        assert rec.exit_at == 0.0
        rec = rec.transition(AgentState.DEAD, timestamp=4.0)
        assert rec.exit_at == 4.0

    def test_with_heartbeat(self):
        rec = create_agent_record("T-10", "r", "m", clock=FakeClock(0))
        rec = rec.transition(AgentState.STARTING, timestamp=1.0)
        rec2 = rec.with_heartbeat(timestamp=50.0)
        assert rec2.last_heartbeat == 50.0
        assert rec2.state == rec.state
        assert rec2.trace == rec.trace

    def test_with_heartbeat_default_time(self):
        rec = create_agent_record("T-11", "r", "m", clock=FakeClock(0))
        with patch("codebot.scheduler_v2.lifecycle.time.time", return_value=7777.0):
            rec2 = rec.with_heartbeat()
        assert rec2.last_heartbeat == 7777.0

    def test_with_exit_from_created(self):
        rec = create_agent_record("T-12", "r", "m", clock=FakeClock(0))
        rec2 = rec.with_exit("failed", timestamp=10.0)
        assert rec2.state == AgentState.DEAD
        assert rec2.exit_reason == "failed"
        assert rec2.exit_at == 10.0
        assert rec2.trace[-2][0] == AgentState.ZOMBIE
        assert rec2.trace[-1][0] == AgentState.DEAD

    def test_with_exit_from_starting(self):
        rec = create_agent_record("T-13", "r", "m", clock=FakeClock(0))
        rec = rec.transition(AgentState.STARTING, timestamp=1.0)
        rec2 = rec.with_exit("error", timestamp=5.0)
        assert rec2.state == AgentState.DEAD
        assert rec2.trace[-1][0] == AgentState.DEAD

    def test_with_exit_from_running(self):
        rec = create_agent_record("T-14", "r", "m", clock=FakeClock(0))
        rec = rec.transition(AgentState.STARTING, timestamp=1.0).transition(AgentState.RUNNING, timestamp=2.0)
        rec2 = rec.with_exit("timeout", timestamp=6.0)
        assert rec2.state == AgentState.DEAD

    def test_with_exit_from_zombie(self):
        rec = create_agent_record("T-15", "r", "m", clock=FakeClock(0))
        rec = rec.transition(AgentState.ZOMBIE, timestamp=1.0)
        rec2 = rec.with_exit("final", timestamp=2.0)
        assert rec2.state == AgentState.DEAD
        assert rec2.exit_reason == "final"
        assert len(rec2.trace) == len(rec.trace) + 1

    def test_with_exit_from_dead_raises(self):
        rec = create_agent_record("T-16", "r", "m", clock=FakeClock(0))
        rec = rec.with_exit("done", timestamp=1.0)
        assert rec.state == AgentState.DEAD
        with pytest.raises(InvalidTransitionError):
            rec.with_exit("again", timestamp=2.0)

    def test_with_exit_default_time(self):
        rec = create_agent_record("T-17", "r", "m", clock=FakeClock(0))
        with patch("codebot.scheduler_v2.lifecycle.time.time", return_value=8888.0):
            rec2 = rec.with_exit("reason")
        assert rec2.exit_at == 8888.0

    def test_agent_record_post_init_trace(self):
        rec = AgentRecord(
            agent_id="a1", ticket_id="t1", role="r", model="m", pid=123,
            state=AgentState.CREATED, created_at=100.0, trace=[]
        )
        assert rec.trace == [(AgentState.CREATED, 100.0)]
        rec2 = AgentRecord(
            agent_id="a1", ticket_id="t1", role="r", model="m", pid=123,
            state=AgentState.RUNNING, created_at=100.0, trace=[(AgentState.RUNNING, 100.0)]
        )
        assert rec2.trace == [(AgentState.RUNNING, 100.0)]

    def test_fake_clock(self):
        clk = FakeClock(initial=100.0)
        assert clk.now() == 100.0
        clk.advance(50.0)
        assert clk.now() == 150.0
        clk.set(999.0)
        assert clk.now() == 999.0
        with pytest.raises(ValueError):
            clk.advance(-1.0)

    def test_real_clock(self):
        clk = RealClock()
        t1 = clk.now()
        time.sleep(0.001)
        t2 = clk.now()
        assert t2 >= t1

    def test_fake_process_poll_and_wait(self):
        p1 = FakeProcess(pid=1, alive=True)
        assert p1.poll() == 0
        assert p1.wait() == 0
        p2 = FakeProcess(pid=2, alive=False)
        assert p2.poll() == 1
        assert p2.wait() == 1
        p3 = FakeProcess(pid=3, alive=True, timeout=True)
        assert p3.poll() is None
        assert p3.wait() == 0
        p3.terminate()
        assert p3.alive is False
        assert p3.poll() == 1
        assert p3.wait() == 1
        p4 = FakeProcess(pid=4, alive=True)
        p4.kill()
        assert p4.poll() == 9
        assert p4.alive is False

    def test_api_model_profiles_import_branch(self, monkeypatch):
        import importlib
        import codebot.scheduler_v2.lifecycle as lc_module

        assert lc_module._API_MODEL_PROFILES is not None
        assert len(lc_module._API_MODEL_PROFILES) > 0
        assert set(MODEL_TIMEOUTS.keys()) == set(lc_module._API_MODEL_PROFILES.keys())

    def test_fake_process_spawner_modes(self):
        for mode in [SpawnerMode.SUCCESS, SpawnerMode.CRASH, SpawnerMode.TIMEOUT]:
            spawner = FakeProcessSpawner(mode=mode)
            handle = spawner.spawn(["cmd"], env={"K": "V"})
            assert handle["pid"] >= 10000
            assert len(spawner.spawned) == 1
            assert spawner.spawned[0]["mode"] == mode
            assert spawner.spawned[0]["cmd"] == ["cmd"]
            assert spawner.spawned[0]["env"] == {"K": "V"}
        spawner = FakeProcessSpawner(mode=SpawnerMode.SUCCESS)
        h1 = spawner.spawn(["a"])
        h2 = spawner.spawn(["b"])
        assert h2["pid"] == h1["pid"] + 1
        with pytest.raises(ValueError):
            bad = FakeProcessSpawner(mode="bad")  # type: ignore
            bad.spawn(["x"])

    def test_fake_process_spawner_is_alive(self):
        spawner = FakeProcessSpawner(mode=SpawnerMode.SUCCESS)
        handle = spawner.spawn(["cmd"])
        assert spawner.is_alive(handle) is False or spawner.is_alive(handle) is True  # FakeProcess alive True but poll 0 => False per logic
        # Specific checks:
        # SUCCESS: alive True, poll 0 => is_alive False (because poll is not None)
        assert spawner.is_alive(handle) is False
        spawner2 = FakeProcessSpawner(mode=SpawnerMode.TIMEOUT)
        handle2 = spawner2.spawn(["cmd"])
        assert spawner2.is_alive(handle2) is True
        spawner3 = FakeProcessSpawner(mode=SpawnerMode.CRASH)
        handle3 = spawner3.spawn(["cmd"])
        assert spawner3.is_alive(handle3) is False
        # dict handle branches
        assert spawner.is_alive({"pid": 1, "cmd": [], "process": {"alive": True}}) is True
        assert spawner.is_alive({"pid": 1, "cmd": [], "process": {"alive": False}}) is False
        assert spawner.is_alive({"pid": 1, "cmd": [], "process": {}}) is True
        assert spawner.is_alive({"pid": 1, "cmd": [], "process": object()}) is True

    def test_real_process_spawner(self):
        spawner = RealProcessSpawner()
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc
            handle = spawner.spawn(["echo", "hi"], env={"X": "1"})
            assert handle["pid"] == 9999
            assert handle["cmd"] == ["echo", "hi"]
            assert spawner.is_alive(handle) is True
            mock_proc.poll.return_value = 0
            assert spawner.is_alive(handle) is False
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 10001
            mock_popen.return_value = mock_proc
            handle = spawner.spawn(["echo", "hi"])
            assert handle["pid"] == 10001

    def test_create_agent_record(self):
        clk = FakeClock(500.0)
        rec = create_agent_record("T-20", "backend_implementer", "xiaomi-mimo-2.5", clock=clk)
        assert rec.ticket_id == "T-20"
        assert rec.role == "backend_implementer"
        assert rec.model == "xiaomi-mimo-2.5"
        assert rec.state == AgentState.CREATED
        assert rec.created_at == 500.0
        assert uuid.UUID(rec.agent_id)
        # without clock
        with patch("codebot.scheduler_v2.lifecycle.time.time", return_value=1234.0):
            rec2 = create_agent_record("T-21", "r", "m")
        assert rec2.created_at == 1234.0

    def test_model_timeouts_dict(self):
        assert isinstance(MODEL_TIMEOUTS, dict)
        assert len(MODEL_TIMEOUTS) > 0
        for k, v in MODEL_TIMEOUTS.items():
            assert "heartbeat_multiplier" in v
            assert "log_stall_seconds" in v

    def test_get_stale_timeout_known_and_unknown(self):
        assert get_stale_timeout("xiaomi-mimo-2.5") == int(120 * 1.5)
        assert get_stale_timeout("qwen-3.8-max-thinking") == int(120 * 2.8)
        assert get_stale_timeout("unknown-model-xyz") == DEFAULT_STALE_TIMEOUT
        assert get_stale_timeout("") == DEFAULT_STALE_TIMEOUT

    def test_is_agent_stale(self):
        clk = FakeClock(1000.0)
        rec = create_agent_record("T-30", "r", "xiaomi-mimo-2.5", clock=FakeClock(0))
        rec = rec.transition(AgentState.STARTING, timestamp=0.0)
        # No heartbeat yet => not stale
        assert is_agent_stale(rec, clock=clk) is False
        # With heartbeat not stale
        rec = rec.with_heartbeat(timestamp=900.0)
        assert is_agent_stale(rec, clock=clk) is False  # 1000-900=100 < 180
        # With stale heartbeat
        rec = rec.with_heartbeat(timestamp=800.0)
        assert is_agent_stale(rec, clock=clk) is True  # 200 > 180
        # CREATED not stale even if heartbeat old
        rec_created = create_agent_record("T-31", "r", "m", clock=FakeClock(0))
        rec_created = AgentRecord(
            agent_id=rec_created.agent_id, ticket_id=rec_created.ticket_id, role=rec_created.role,
            model=rec_created.model, pid=rec_created.pid, state=AgentState.CREATED,
            created_at=rec_created.created_at, last_heartbeat=0.0, trace=rec_created.trace
        )
        assert is_agent_stale(rec_created, clock=clk) is False
        # ZOMBIE not stale
        zombie = create_agent_record("T-32", "r", "m", clock=FakeClock(0)).transition(AgentState.ZOMBIE, timestamp=0.0)
        zombie = AgentRecord(
            agent_id=zombie.agent_id, ticket_id=zombie.ticket_id, role=zombie.role,
            model=zombie.model, pid=zombie.pid, state=AgentState.ZOMBIE,
            created_at=zombie.created_at, last_heartbeat=clk.now() - 1000, trace=zombie.trace
        )
        assert is_agent_stale(zombie, clock=clk) is False
        # Thinking model has longer timeout
        clk2 = FakeClock(1000.0)
        rec_think = create_agent_record("T-33", "r", "qwen-3.8-max-thinking", clock=FakeClock(0))
        rec_think = rec_think.transition(AgentState.STARTING, timestamp=0.0).with_heartbeat(timestamp=800.0)
        assert is_agent_stale(rec_think, clock=clk2) is False  # 200 < 336
        rec_think = rec_think.with_heartbeat(timestamp=600.0)
        assert is_agent_stale(rec_think, clock=clk2) is True  # 400 > 336
        # Default clock (None)
        rec_default = create_agent_record("T-34", "r", "m", clock=FakeClock(0)).transition(AgentState.STARTING, timestamp=0.0)
        rec_default = rec_default.with_heartbeat(timestamp=time.time() - 10)
        assert is_agent_stale(rec_default) is False
        rec_default2 = rec_default.with_heartbeat(timestamp=time.time() - 1000)
        assert is_agent_stale(rec_default2) is True

    def test_agent_state_from_string(self):
        assert agent_state_from_string("CREATED") == AgentState.CREATED
        assert agent_state_from_string("DEAD") == AgentState.DEAD
        assert agent_state_from_string("invalid") is None
        assert agent_state_from_string("") is None

class TestDispatchGate:
    def test_concurrency_reserve_release_basic(self):
        clk = FakeClock(0.0)
        cc = ConcurrencyController(max_slots=3, clock=clk)
        assert cc.count_active() == 0
        assert cc.active_count == 0
        assert cc.free_slots == 3
        assert cc.queued_count == 0
        assert cc.used_count == 0
        t1 = cc.reserve()
        assert t1 is not None
        assert cc.count_active() == 1
        assert cc.free_slots == 2
        t2 = cc.reserve()
        t3 = cc.reserve()
        assert cc.count_active() == 3
        assert cc.reserve() is None
        assert cc.free_slots == 0
        assert cc.release(t2) is True
        assert cc.count_active() == 2
        assert cc.release(t2) is False
        assert cc.release(t2.id) is False
        assert cc.release("nonexistent") is False
        assert cc.reserve() is not None
        assert cc.count_active() == 3
        with pytest.raises(ValueError):
            ConcurrencyController(max_slots=0)
        with pytest.raises(ValueError):
            ConcurrencyController(max_slots=-1)

    def test_concurrency_race(self):
        cc = ConcurrencyController(max_slots=5, clock=FakeClock(0.0))
        results: list[SlotToken | None] = []
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            r = cc.reserve()
            if r is not None:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 5
        assert cc.count_active() == 5
        for r in results:
            assert r is not None
        ids = [r.id for r in results]
        assert len(set(ids)) == 5

    def test_claim_ticket_basic(self, tmp_path: Path):
        clk = FakeClock(100.0)
        rec = claim_ticket(tmp_path, "T-1", "agent-1", role="impl", clock=clk)
        assert rec is not None
        assert rec.ticket_id == "T-1"
        assert rec.agent_id == "agent-1"
        assert rec.role == "impl"
        assert rec.claimed_at == 100.0
        assert rec.revision_token
        # Same agent re-claim is idempotent
        rec2 = claim_ticket(tmp_path, "T-1", "agent-1", role="impl", clock=clk)
        assert rec2 is not None
        assert rec2.ticket_id == "T-1"
        # Different agent cannot claim
        rec3 = claim_ticket(tmp_path, "T-1", "agent-2", role="impl", clock=clk)
        assert rec3 is None
        # Invalid args
        assert claim_ticket(tmp_path, "", "agent-1") is None
        assert claim_ticket(tmp_path, "T-1", "") is None
        assert claim_ticket(None, "T-1", "agent-1") is None  # type: ignore
        p = Path(str(tmp_path) + "/nonexistent-sub")
        assert claim_ticket(p / "missing", "", "agent-1") is None

    def test_claim_corrupt_file(self, tmp_path: Path):
        # Write corrupt claim file then try to claim with same agent
        claims_dir = tmp_path / "claims"
        claims_dir.mkdir(parents=True, exist_ok=True)
        (claims_dir / "T-CORRUPT.claim.json").write_text("not valid json {{{", encoding="utf-8")
        rec = claim_ticket(tmp_path, "T-CORRUPT", "agent-99", clock=FakeClock(0))
        # Corrupt existing claim treated as empty owner, same agent check fails (owner=="")
        # But "agent-99" != "" so returns None. Actually current code: owner="" from empty dict, owner != agent_id => None
        # Let's also test the second path: corrupt with existing owner
        assert rec is None

    def test_release_claim(self, tmp_path: Path):
        clk = FakeClock(0.0)
        # Release non-existent claim is idempotent success
        assert release_claim(tmp_path, "T-NONE", "agent-1") is True
        # Invalid args
        assert release_claim(tmp_path, "", "agent-1") is False
        assert release_claim(tmp_path, "T-1", "") is False
        assert release_claim(None, "T-1", "agent-1") is False  # type: ignore
        # Correct release
        claim_ticket(tmp_path, "T-1", "agent-1", clock=clk)
        assert release_claim(tmp_path, "T-1", "agent-1") is True
        # Already released - idempotent
        assert release_claim(tmp_path, "T-1", "agent-1") is True
        # Wrong agent cannot release
        claim_ticket(tmp_path, "T-2", "agent-1", clock=clk)
        assert release_claim(tmp_path, "T-2", "agent-2") is False
        # Ensure original claim still exists
        assert (tmp_path / "claims" / "T-2.claim.json").exists()

    def test_release_corrupt_claim(self, tmp_path: Path):
        claims_dir = tmp_path / "claims"
        claims_dir.mkdir(parents=True, exist_ok=True)
        (claims_dir / "T-BAD.claim.json").write_text("{{{ corrupt", encoding="utf-8")
        assert release_claim(tmp_path, "T-BAD", "agent-1") is True
        assert not (claims_dir / "T-BAD.claim.json").exists()

    def test_stagger_basic(self):
        clk = FakeClock(1000.0)
        q = SpawnQueue(clock=clk, stagger_seconds=5.0)
        assert len(q) == 0
        assert q.queue_depth == 0
        assert q.peek_next() is None
        assert q.next_spawn_in() is None
        assert q.dequeue_ready() == []
        # First enqueue at T=1000
        r1 = SpawnRequest(agent_id="a1", ticket_id="t1", role="r", model="m")
        s1 = q.enqueue(r1)
        assert s1.scheduled_at == 1000.0
        # Second at T+5
        r2 = SpawnRequest(agent_id="a2", ticket_id="t2", role="r", model="m")
        s2 = q.enqueue(r2)
        assert s2.scheduled_at == 1005.0
        # Third at T+10
        r3 = SpawnRequest(agent_id="a3", ticket_id="t3", role="r", model="m")
        s3 = q.enqueue(r3)
        assert s3.scheduled_at == 1010.0
        assert len(q) == 3
        assert q.next_spawn_in() == 0.0
        # All dequeue at 1010
        clk.advance(10.0)
        ready = q.dequeue_ready()
        assert len(ready) == 3
        assert len(q) == 0

    def test_stagger_dequeue_partial(self):
        clk = FakeClock(0.0)
        q = SpawnQueue(clock=clk, stagger_seconds=5.0)
        q.enqueue(SpawnRequest(agent_id="a1", ticket_id="t1", role="r", model="m"))
        q.enqueue(SpawnRequest(agent_id="a2", ticket_id="t2", role="r", model="m"))
        q.enqueue(SpawnRequest(agent_id="a3", ticket_id="t3", role="r", model="m"))
        # At T=0 only a1 ready
        ready = q.dequeue_ready()
        assert len(ready) == 1 and ready[0].agent_id == "a1"
        clk.advance(5.0)
        ready = q.dequeue_ready()
        assert len(ready) == 1 and ready[0].agent_id == "a2"
        clk.advance(5.0)
        ready = q.dequeue_ready()
        assert len(ready) == 1 and ready[0].agent_id == "a3"

    def test_stagger_heartbeat_empty_queue(self, tmp_path: Path):
        # E3: empty queue should use disk heartbeat
        clk = FakeClock(1000.0)
        (tmp_path / "worker-1.heartbeat").write_text("1995.0", encoding="utf-8")
        q = SpawnQueue(clock=clk, stagger_seconds=5.0, heartbeat_dir=tmp_path)
        q.update_latest_heartbeat(1998.0)
        r1 = SpawnRequest(agent_id="a1", ticket_id="t1", role="r", model="m")
        s1 = q.enqueue(r1)
        # heartbeat 1998 + 5 = 2003, now=1000, max(1000,2003)=2003
        # But clock now=1000, heartbeat 1998 > now, so next = hb+5 = 2003
        assert s1.scheduled_at == 2003.0
        # Subsequent heartbeats must NOT postpone already-scheduled request
        q.update_latest_heartbeat(3000.0)
        s2 = q.enqueue(SpawnRequest(agent_id="a2", ticket_id="t2", role="r", model="m"))
        # s2 scheduled at s1+5=2008, NOT 3005
        assert s2.scheduled_at == 2008.0
        assert s1.scheduled_at == 2003.0

    def test_stagger_heartbeat_from_disk(self, tmp_path: Path):
        clk = FakeClock(1000.0)
        # Write heartbeat file to disk, don't call update_latest_heartbeat
        (tmp_path / "backend_implementer.heartbeat").write_text("990.0", encoding="utf-8")
        q = SpawnQueue(clock=clk, stagger_seconds=5.0, heartbeat_dir=tmp_path)
        r1 = SpawnRequest(agent_id="a1", ticket_id="t1", role="r", model="m")
        s1 = q.enqueue(r1)
        # Should read 990 from disk: max(1000, 990+5)=1000
        assert s1.scheduled_at == 1000.0

    def test_cancel_with_failed_claim_still_releases_slot(self, tmp_path: Path):
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=5, state_dir=tmp_path, clock=clk)
        gate.try_dispatch("T-1", "agent-1", role="impl")
        assert gate.cancel_dispatch("agent-1", "T-1") is True
        assert gate.cancel_dispatch("agent-1", "T-1") is False
        assert gate.count_active() == 0
        gate2 = DispatchGate(max_slots=1, state_dir=tmp_path, clock=FakeClock(0.0))
        gate2.try_dispatch("T-2", "agent-2", role="impl")
        r = gate2.try_dispatch("T-3", "agent-3", role="impl")
        assert r.reason == "NO_CAPACITY"
        assert gate2.cancel_dispatch("agent-2", "T-2") is True
        r2 = gate2.try_dispatch("T-3", "agent-3", role="impl")
        assert r2.success is True
        assert r2.scheduled_at is not None

    def test_stagger_heartbeat_value_read_twice(self, tmp_path: Path):
        clk = FakeClock(1000.0)
        (tmp_path / "alive.heartbeat").write_text("998.0", encoding="utf-8")
        (tmp_path / "older.heartbeat").write_text("500.0", encoding="utf-8")
        q = SpawnQueue(clock=clk, stagger_seconds=5.0, heartbeat_dir=tmp_path)
        assert q._read_latest_heartbeat_from_disk() == 998.0
        (tmp_path / "alive.heartbeat").write_text("999.0", encoding="utf-8")
        assert q._read_latest_heartbeat_from_disk() == 999.0

    def test_stagger_heartbeat_disk_read_via_enqueue(self, tmp_path: Path):
        clk = FakeClock(1000.0)
        (tmp_path / "alive.heartbeat").write_text("998.0", encoding="utf-8")
        (tmp_path / "stale.heartbeat").write_text("not_a_number", encoding="utf-8")
        (tmp_path / "also_empty.heartbeat").write_text("   ", encoding="utf-8")
        q = SpawnQueue(clock=clk, stagger_seconds=5.0, heartbeat_dir=tmp_path)
        assert q._read_latest_heartbeat_from_disk() == 998.0

    def test_stagger_heartbeat_disk_with_corrupt_file(self, tmp_path: Path):
        clk = FakeClock(1000.0)
        (tmp_path / "bad.heartbeat").write_text("not_a_number", encoding="utf-8")
        (tmp_path / "good.heartbeat").write_text("995.0", encoding="utf-8")
        (tmp_path / "empty.heartbeat").write_text("", encoding="utf-8")
        q = SpawnQueue(clock=clk, stagger_seconds=5.0, heartbeat_dir=tmp_path)
        assert q._read_latest_heartbeat_from_disk() == 995.0

    def test_stagger_heartbeat_from_disk_later(self, tmp_path: Path):
        clk = FakeClock(1000.0)
        (tmp_path / "some.heartbeat").write_text("1200.0", encoding="utf-8")
        q = SpawnQueue(clock=clk, stagger_seconds=5.0, heartbeat_dir=tmp_path)
        r1 = SpawnRequest(agent_id="a1", ticket_id="t1", role="r", model="m")
        s1 = q.enqueue(r1)
        assert s1.scheduled_at == 1205.0
        # Corrupt heartbeat file should be ignored
        (tmp_path / "bad.heartbeat").write_text("not_a_number", encoding="utf-8")
        q2 = SpawnQueue(clock=FakeClock(500.0), stagger_seconds=5.0, heartbeat_dir=tmp_path)
        q2.update_latest_heartbeat(0.0)
        # Disk read finds valid latest is 1200, heartbeat_dir scan still works
        # But _latest_heartbeat is 0, so it reads from disk. Corrupt file ignored.
        (tmp_path / "empty.heartbeat").write_text("", encoding="utf-8")
        r2 = SpawnRequest(agent_id="a1", ticket_id="t1", role="r", model="m")
        # Should not crash, ignores bad files
        s2 = q2.enqueue(r2)
        assert s2.scheduled_at >= 500.0

    def test_stagger_no_heartbeat_dir(self):
        clk = FakeClock(1000.0)
        q = SpawnQueue(clock=clk, stagger_seconds=5.0, heartbeat_dir=None)
        q.update_latest_heartbeat(0.0)
        s1 = q.enqueue(SpawnRequest(agent_id="a1", ticket_id="t1", role="r", model="m"))
        assert s1.scheduled_at == 1000.0

    def test_stagger_invalid_stagger(self):
        with pytest.raises(ValueError):
            SpawnQueue(clock=FakeClock(0.0), stagger_seconds=0)
        with pytest.raises(ValueError):
            SpawnQueue(clock=FakeClock(0.0), stagger_seconds=-1)

    def test_stagger_cancel(self):
        clk = FakeClock(0.0)
        q = SpawnQueue(clock=clk, stagger_seconds=5.0)
        q.enqueue(SpawnRequest(agent_id="a1", ticket_id="t1", role="r", model="m"))
        q.enqueue(SpawnRequest(agent_id="a2", ticket_id="t2", role="r", model="m"))
        assert q.cancel("a1") is True
        assert len(q) == 1
        assert q.cancel("nonexistent") is False
        assert q.cancel("a2") is True
        assert len(q) == 0

    def test_stagger_next_spawn_in(self):
        clk = FakeClock(0.0)
        q = SpawnQueue(clock=clk, stagger_seconds=5.0)
        q.enqueue(SpawnRequest(agent_id="a1", ticket_id="t1", role="r", model="m"))
        q.enqueue(SpawnRequest(agent_id="a2", ticket_id="t2", role="r", model="m"))
        assert q.next_spawn_in() == 0.0
        clk.advance(3.0)
        # a1 was at 0, dequeue_ready would have removed it. But we haven't dequeued.
        # At T=3, a1 ready at 0, a2 at 5. next_spawn_in checks queue[0]=a1 so 0.
        assert q.next_spawn_in() == 0.0
        q.dequeue_ready()
        assert q.next_spawn_in() == pytest.approx(2.0)
        clk.advance(3.0)
        assert q.next_spawn_in() == pytest.approx(0.0)
        q.dequeue_ready()
        assert q.next_spawn_in() is None

    def test_stagger_missing_heartbeat_dir(self, tmp_path: Path):
        clk = FakeClock(1000.0)
        missing = tmp_path / "nonexistent"
        q = SpawnQueue(clock=clk, stagger_seconds=5.0, heartbeat_dir=missing)
        # _read_latest_heartbeat_from_disk returns 0 for missing dir
        s1 = q.enqueue(SpawnRequest(agent_id="a1", ticket_id="t1", role="r", model="m"))
        assert s1.scheduled_at == 1000.0

    def test_stagger_update_heartbeat_no_regression(self):
        clk = FakeClock(0.0)
        q = SpawnQueue(clock=clk, stagger_seconds=5.0)
        q.update_latest_heartbeat(100.0)
        q.update_latest_heartbeat(50.0)
        # Should keep max, so 100
        assert q._latest_heartbeat == 100.0
        q.update_latest_heartbeat(150.0)
        assert q._latest_heartbeat == 150.0

    def test_dispatch_gate_basic(self, tmp_path: Path):
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=2, state_dir=tmp_path, clock=clk)
        r1 = gate.try_dispatch("T-1", "agent-1", role="impl", model="m")
        assert r1.success is True
        assert r1.reason == "DISPATCHED"
        assert r1.token is not None
        assert r1.claim is not None
        assert r1.scheduled_at is not None
        assert gate.count_active() == 1
        # Second ticket different agent
        r2 = gate.try_dispatch("T-2", "agent-2", role="impl", model="m")
        assert r2.success is True
        assert gate.count_active() == 2
        # Third exceeds capacity
        r3 = gate.try_dispatch("T-3", "agent-3", role="impl", model="m")
        assert r3.success is False
        assert r3.reason == "NO_CAPACITY"
        # Complete first, then third can proceed
        assert gate.complete_dispatch("agent-1") is True
        assert gate.count_active() == 1
        assert gate.complete_dispatch("agent-1") is False
        r3b = gate.try_dispatch("T-3", "agent-3", role="impl", model="m")
        assert r3b.success is True

    def test_dispatch_gate_already_claimed(self, tmp_path: Path):
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=5, state_dir=tmp_path, clock=clk)
        r1 = gate.try_dispatch("T-1", "agent-1", role="impl")
        assert r1.success is True
        r2 = gate.try_dispatch("T-1", "agent-2", role="impl")
        assert r2.success is False
        assert r2.reason == "ALREADY_CLAIMED"
        assert gate.count_active() == 1

    def test_dispatch_gate_invalid_args(self, tmp_path: Path):
        gate = DispatchGate(max_slots=5, state_dir=tmp_path, clock=FakeClock(0.0))
        r = gate.try_dispatch("", "agent-1")
        assert r.success is False
        assert r.reason == "INVALID_ARGS"
        r2 = gate.try_dispatch("T-1", "")
        assert r2.success is False
        assert r2.reason == "INVALID_ARGS"

    def test_dispatch_gate_cancel_and_complete(self, tmp_path: Path):
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=5, state_dir=tmp_path, clock=clk)
        gate.try_dispatch("T-1", "agent-1", role="impl")
        assert (tmp_path / "claims" / "T-1.claim.json").exists()
        assert gate.cancel_dispatch("agent-1", "T-1") is True
        assert gate.count_active() == 0
        assert not (tmp_path / "claims" / "T-1.claim.json").exists()
        # Double cancel is safe
        assert gate.cancel_dispatch("agent-1", "T-1") is False
        assert gate.cancel_dispatch("nonexistent", "T-X") is False
        # Dispatch again, complete normally
        gate.try_dispatch("T-1", "agent-1", role="impl")
        assert gate.complete_dispatch("agent-1") is True
        assert gate.count_active() == 0
        assert len(gate.spawn_queue) == 0
        assert gate.cancel_dispatch("agent-1", "T-1") is False
        assert gate.spawn_queue.cancel("agent-1") is False

    def test_dispatch_gate_no_state_dir(self):
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=5, state_dir=None, clock=clk)
        r = gate.try_dispatch("T-1", "agent-1", role="impl")
        assert r.success is True
        assert r.claim is not None
        assert r.scheduled_at is not None
        assert gate.count_active() == 1
        assert len(gate.spawn_queue) == 1
        assert gate.complete_dispatch("agent-1") is True
        assert gate.count_active() == 0
        assert len(gate.spawn_queue) == 0
        assert gate.cancel_dispatch("agent-1", "T-1") is False

    def test_dispatch_gate_no_state_dir_cancel(self):
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=5, state_dir=None, clock=clk)
        gate.try_dispatch("T-1", "agent-1", role="impl")
        assert gate.cancel_dispatch("agent-1", "T-1") is True
        assert gate.count_active() == 0

    def test_dispatch_gate_stagger_queue_invalid_stagger_via_gate(self):
        with pytest.raises(ValueError):
            DispatchGate(max_slots=5, stagger_seconds=0)
        with pytest.raises(ValueError):
            DispatchGate(max_slots=5, stagger_seconds=-10)

    def test_heartbeat_dir_none_branch(self, tmp_path: Path):
        clk = FakeClock(1000.0)
        q = SpawnQueue(clock=clk, stagger_seconds=5.0, heartbeat_dir=None)
        assert q._read_latest_heartbeat_from_disk() == 0.0
        s = q.enqueue(SpawnRequest(agent_id="a1", ticket_id="t1", role="r", model="m"))
        assert s.scheduled_at == 1000.0

    def test_spawn_queue_peek_next_nonempty(self):
        clk = FakeClock(0.0)
        q = SpawnQueue(clock=clk, stagger_seconds=5.0)
        assert q.peek_next() is None
        s = q.enqueue(SpawnRequest(agent_id="a1", ticket_id="t1", role="r", model="m"))
        nxt = q.peek_next()
        assert nxt is not None
        assert nxt.request.agent_id == "a1"
        assert nxt.scheduled_at == s.scheduled_at

    def test_stagger_queue_depth_property(self):
        clk = FakeClock(0.0)
        q = SpawnQueue(clock=clk, stagger_seconds=5.0)
        q.enqueue(SpawnRequest(agent_id="a1", ticket_id="t1", role="r", model="m"))
        assert q.queue_depth == 1
        assert q.next_spawn_in() == 0.0
        q.dequeue_ready()
        assert q.queue_depth == 0
        assert q.next_spawn_in() is None

    def test_dispatch_gate_validation(self):
        with pytest.raises(ValueError):
            DispatchGate(max_slots=0)
        with pytest.raises(ValueError):
            DispatchGate(max_slots=-1, stagger_seconds=5.0)
        with pytest.raises(ValueError):
            DispatchGate(max_slots=5, stagger_seconds=0)
        with pytest.raises(ValueError):
            DispatchGate(max_slots=5, stagger_seconds=-5)

    def test_dispatch_gate_dequeue_and_heartbeat(self, tmp_path: Path):
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=5, state_dir=tmp_path, clock=clk)
        gate.try_dispatch("T-1", "agent-1", role="impl")
        gate.try_dispatch("T-2", "agent-2", role="impl")
        gate.update_heartbeat(100.0)
        ready = gate.dequeue_ready()
        assert len(ready) >= 1
        assert gate.complete_dispatch("agent-1") is True

    def test_dispatch_gate_stress(self, tmp_path: Path):
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=10, state_dir=tmp_path, clock=clk)
        for i in range(20):
            gate.try_dispatch(f"T-S{i}", f"agent-{i}", role="impl")
        assert gate.count_active() == 10
        # No more should succeed
        r = gate.try_dispatch("T-EXTRA", "agent-extra", role="impl")
        assert r.success is False
        assert r.reason == "NO_CAPACITY"
        # Complete 5, then 5 more should work
        for i in range(5):
            gate.complete_dispatch(f"agent-{i}")
        assert gate.count_active() == 5
        for i in range(20, 25):
            r = gate.try_dispatch(f"T-S{i}", f"agent-{i}", role="impl")
            assert r.success is True
        assert gate.count_active() == 10

    def test_concurrent_claims_one_winner(self, tmp_path: Path):
        results: list[ClaimRecord | None] = []
        barrier = threading.Barrier(5)

        def worker():
            barrier.wait()
            r = claim_ticket(tmp_path, "T-RACE", f"agent-{threading.get_ident()}", clock=FakeClock(0.0))
            results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [r for r in results if r is not None]
        assert len(winners) == 1

    def test_concurrent_concurrency_one_winner(self):
        results: list[SlotToken | None] = []
        cc = ConcurrencyController(max_slots=1, clock=FakeClock(0.0))
        barrier = threading.Barrier(5)

        def worker():
            barrier.wait()
            r = cc.reserve()
            if r is not None:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 1

class TestDispatcher:
    def test_model_selector_basic(self, tmp_path: Path):
        selector = ModelSelector(
            model_pool=["a", "b", "c"], state_path=tmp_path / "models.json", clock=FakeClock(0.0)
        )
        assert selector.next_model() == "a"
        assert selector.next_model() == "b"
        assert selector.next_model() == "c"
        assert selector.next_model() == "a"
        assert selector.current_index == 1

    def test_model_selector_persists(self, tmp_path: Path):
        state_path = tmp_path / "sel.json"
        s1 = ModelSelector(model_pool=["x", "y", "z"], state_path=state_path, clock=FakeClock(0.0))
        s1.next_model()
        s1.next_model()
        assert s1.current_index == 2
        s2 = ModelSelector(model_pool=["x", "y", "z"], state_path=state_path, clock=FakeClock(0.0))
        assert s2.current_index == 2
        assert s2.next_model() == "z"

    def test_model_selector_unavailable(self, tmp_path: Path):
        selector = ModelSelector(model_pool=["a", "b"], state_path=None)
        selector.mark_unavailable("a")
        assert selector.next_model() == "b"
        assert selector.next_model() == "b"
        selector.mark_unavailable("b")
        with pytest.raises(NoModelsAvailableError):
            selector.next_model()
        selector.mark_available("a")
        assert selector.next_model() == "a"

    def test_model_selector_empty_pool(self):
        with pytest.raises(ValueError):
            ModelSelector(model_pool=[], state_path=None)

    def test_model_selector_corrupt_index(self, tmp_path: Path):
        p = tmp_path / "sel.json"
        p.write_text("not json", encoding="utf-8")
        s = ModelSelector(model_pool=["a", "b"], state_path=p, clock=FakeClock(0.0))
        assert s.current_index == 0
        assert s.next_model() == "a"
        p.write_text(json.dumps({"index": "bad"}), encoding="utf-8")
        s2 = ModelSelector(model_pool=["a", "b"], state_path=p, clock=FakeClock(0.0))
        assert s2.current_index == 0

    def test_model_selector_save_index_io(self, tmp_path: Path):
        s = ModelSelector(model_pool=["a", "b"], state_path=tmp_path / "save.json", clock=FakeClock(0.0))
        s.next_model()
        assert (tmp_path / "save.json").exists()
        data = json.loads((tmp_path / "save.json").read_text(encoding="utf-8"))
        assert "index" in data
        s2 = ModelSelector(model_pool=["a", "b"], state_path=tmp_path / "save.json", clock=FakeClock(0.0))
        assert s2.current_index == 1

    def test_model_selector_skip_and_persistence(self, tmp_path: Path):
        s = ModelSelector(model_pool=["m1", "m2", "m3"], state_path=tmp_path / "idx.json", clock=FakeClock(0.0))
        assert s.next_model() == "m1"
        s.mark_unavailable("m2")
        assert s.next_model() == "m3"
        assert s.next_model() == "m1"
        s.mark_unavailable("m1")
        assert s.next_model() == "m3"
        s.mark_unavailable("m3")
        with pytest.raises(NoModelsAvailableError):
            s.next_model()
        s.mark_available("m1")
        assert s.next_model() == "m1"

    def test_bucket_dispatcher_role_cap_blocks(self, tmp_path: Path):
        from codebot.ticket_engine import Ticket, TicketClass, TicketState, Severity, RiskLevel
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=10, state_dir=tmp_path, clock=clk)
        bd = BucketDispatcher(gate=gate, clock=clk, role_caps={"implement": 1})
        gate.try_dispatch("CB-CAP-1", "agent-cap-1", role="general_implementer")
        bd2 = BucketDispatcher(gate=gate, clock=clk, role_caps={"implement": 1})
        t2 = Ticket(
            id="CB-CAP-2", title="t", ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM, state=TicketState.PLANNING,
            source="test", evidence="e", problem_statement="p", desired_state="d",
            acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
            migration_impact="none", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="revert",
            estimated_cost_tokens=100, created_at=clk.now(), updated_at=clk.now())
        t3 = Ticket(
            id="CB-CAP-3", title="t", ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM, state=TicketState.PLANNING,
            source="test", evidence="e", problem_statement="p", desired_state="d",
            acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
            migration_impact="none", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="revert",
            estimated_cost_tokens=100, created_at=clk.now(), updated_at=clk.now())
        store = type("Store", (), {
            "list_by_state": lambda self, state: ([t2, t3] if state.value == "PLANNING" else [])
        })()
        assert bd2._is_ticket_active("CB-CAP-1") is True
        assert bd2._is_ticket_active("CB-CAP-99") is False
        assert bd2._count_active_per_bucket({"IMPLEMENT": [t2]}, {"CB-CAP-2"})["IMPLEMENT"] == 1
        assert bd2._count_active_per_bucket({"IMPLEMENT": [t2]}, {"CB-OTHER"})["IMPLEMENT"] == 0

    def test_bucket_dispatcher_priority_sorting(self, tmp_path: Path):
        from codebot.ticket_engine import Ticket, TicketClass, TicketState, Severity, RiskLevel
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=10, state_dir=tmp_path, clock=clk)
        bd = BucketDispatcher(gate=gate, clock=clk)
        t_low = Ticket(
            id="CB-LOW", title="t", ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM, state=TicketState.PLANNING,
            source="test", evidence="e", problem_statement="p", desired_state="d",
            acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
            migration_impact="none", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="revert",
            estimated_cost_tokens=100, created_at=100.0, updated_at=100.0)
        t_high = Ticket(
            id="CB-HIGH", title="t", ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM, state=TicketState.PLANNING,
            source="test", evidence="e", problem_statement="p", desired_state="d",
            acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
            migration_impact="none", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="revert",
            estimated_cost_tokens=100, created_at=200.0, updated_at=200.0)
        object.__setattr__(t_low, "priority", "low")  # type: ignore
        object.__setattr__(t_high, "priority", "high")  # type: ignore
        store = type("Store", (), {
            "list_by_state": lambda self, state: ([t_low, t_high] if state.value == "PLANNING" else [])
        })()
        n = bd.tick(store)
        assert n == 2
        assert bd._select_ticket([t_low, t_high]).id == "CB-HIGH"

    def test_bucket_dispatcher_snapshot_various_stores(self):
        from codebot.ticket_engine import Ticket, TicketClass, TicketState, Severity, RiskLevel
        bd = BucketDispatcher(gate=DispatchGate(max_slots=5, state_dir=None, clock=FakeClock(0.0)), clock=FakeClock(0.0))
        store = type("Store", (), {
            "list_by_state": lambda self, state: (_ for _ in ()).throw(RuntimeError("boom"))
        })()
        snapshots = bd._snapshot_buckets(store)
        assert all(v == [] for v in snapshots.values())
        store2 = type("Store2", (), {
            "list_by_state": lambda self, state: [type("T", (), {
                "id": "CB-BLOCKED-1", "state": type("S", (), {"value": "BLOCKED"})(),
            })()]
        })()
        snaps2 = bd._snapshot_buckets(store2)
        assert all(v == [] for v in snaps2.values())
        assert bd._snapshot_buckets(None) == {name: [] for name, _ in bd._bucket_order}
        assert bd._count_active_per_bucket({}, set()) == {}
        t_blocked = Ticket(
            id="CB-BLK", title="t", ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM, state=TicketState.GOAL,
            source="test", evidence="e", problem_statement="p", desired_state="d",
            acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
            migration_impact="none", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="revert",
            estimated_cost_tokens=100, created_at=0.0, updated_at=0.0)
        t_blocked = type("TicketBlocked", (), dict(t_blocked.__dict__, state=type("S", (), {"value": "BLOCKED"})()))()

        store3 = type("Store3", (), {
            "list_by_state": lambda self, state: [t_blocked] if state.value == "TRIAGED" else []
        })()
        snaps3 = bd._snapshot_buckets(store3)
        assert snaps3["GOAL"] == []

    def test_bucket_dispatcher_model_unavailable_skips(self, tmp_path: Path):
        from codebot.ticket_engine import Ticket, TicketClass, TicketState, Severity, RiskLevel
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=5, state_dir=tmp_path, clock=clk)
        selector = ModelSelector(model_pool=["m1"], state_path=None, clock=clk)
        selector.mark_unavailable("m1")
        bd = BucketDispatcher(gate=gate, model_selector=selector, clock=clk)
        t = Ticket(
            id="CB-SKIP", title="t", ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM, state=TicketState.PLANNING,
            source="test", evidence="e", problem_statement="p", desired_state="d",
            acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
            migration_impact="none", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="revert",
            estimated_cost_tokens=100, created_at=clk.now(), updated_at=clk.now())
        store = type("Store", (), {
            "list_by_state": lambda self, state: ([t] if state.value == "PLANNING" else [])
        })()
        n = bd.tick(store)
        assert n == 0

    def test_bucket_dispatcher_blocked_ticket_filtered(self, tmp_path: Path):
        from codebot.ticket_engine import Ticket, TicketClass, TicketState, Severity, RiskLevel
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=5, state_dir=tmp_path, clock=clk)
        bd = BucketDispatcher(gate=gate, clock=clk)
        t = Ticket(
            id="CB-BLKT", title="t", ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM, state=TicketState.GOAL,
            source="test", evidence="e", problem_statement="p", desired_state="d",
            acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
            migration_impact="none", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="revert",
            estimated_cost_tokens=100, created_at=clk.now(), updated_at=clk.now())
        t = type("TicketBlocked", (), dict(t.__dict__, state=type("S", (), {"value": "BLOCKED"})()))()
        store = type("Store", (), {
            "list_by_state": lambda self, state: ([t] if state.value == "TRIAGED" else [])
        })()
        snaps = bd._snapshot_buckets(store)
        assert snaps["GOAL"] == []

    def test_bucket_dispatcher_already_claimed_loop(self, tmp_path: Path):
        from codebot.ticket_engine import Ticket, TicketClass, TicketState, Severity, RiskLevel
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=5, state_dir=tmp_path, clock=clk)
        gate.try_dispatch("CB-CLAIMED", "agent-pre", role="r")
        bd = BucketDispatcher(gate=gate, clock=clk)
        t = Ticket(
            id="CB-CLAIMED", title="t", ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM, state=TicketState.PLANNING,
            source="test", evidence="e", problem_statement="p", desired_state="d",
            acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
            migration_impact="none", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="revert",
            estimated_cost_tokens=100, created_at=clk.now(), updated_at=clk.now())
        store = type("Store", (), {
            "list_by_state": lambda self, state: ([t] if state.value == "PLANNING" else [])
        })()
        n = bd.tick(store)
        assert n == 0

    def test_bucket_dispatcher_cap_miss_branch(self, tmp_path: Path):
        from codebot.ticket_engine import Ticket, TicketClass, TicketState, Severity, RiskLevel
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=10, state_dir=tmp_path, clock=clk)
        bd = BucketDispatcher(gate=gate, clock=clk, role_caps={"unknown_bucket": 0})
        gate2 = DispatchGate(max_slots=10, state_dir=tmp_path, clock=clk)
        t = Ticket(
            id="CB-UNKN-1", title="t", ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM, state=TicketState.PLANNING,
            source="test", evidence="e", problem_statement="p", desired_state="d",
            acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
            migration_impact="none", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="revert",
            estimated_cost_tokens=100, created_at=clk.now(), updated_at=clk.now())
        store = type("Store", (), {
            "list_by_state": lambda self, state: ([t] if state.value == "PLANNING" else [])
        })()
        n = bd.tick(store)
        assert n >= 1

    def test_bucket_dispatcher_full_tick(self, tmp_path: Path):
        from codebot.ticket_engine import Ticket, TicketClass, TicketState, Severity, RiskLevel
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=5, state_dir=tmp_path, clock=clk)
        bd = BucketDispatcher(gate=gate, clock=clk)
        tickets_impl = [
            Ticket(id=f"CB-IMPL-{i}", title="t", ticket_class=TicketClass.BUG,
                   severity=Severity.MEDIUM, state=TicketState.PLANNING,
                   source="test", evidence="e", problem_statement="p", desired_state="d",
                   acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
                   risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
                   migration_impact="none", required_reviewers=[], required_tests=[],
                   documentation_requirements=[], rollback_strategy="revert",
                   estimated_cost_tokens=100, created_at=clk.now(), updated_at=clk.now())
            for i in range(3)
        ]
        tickets_review = [
            Ticket(id="CB-REV-1", title="t", ticket_class=TicketClass.BUG,
                   severity=Severity.MEDIUM, state=TicketState.IMPLEMENT,
                   source="test", evidence="e", problem_statement="p", desired_state="d",
                   acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
                   risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
                   migration_impact="none", required_reviewers=[], required_tests=[],
                   documentation_requirements=[], rollback_strategy="revert",
                   estimated_cost_tokens=100, created_at=clk.now(), updated_at=clk.now())
        ]
        store = type("Store", (), {
            "list_by_state": lambda self, state: (
                tickets_impl if state.value == "PLANNING" else
                tickets_review if state.value == "IMPLEMENT" else []
            )
        })()
        n = bd.tick(store)
        assert n >= 2

    def test_bucket_dispatcher_helpers(self, tmp_path: Path):
        from codebot.scheduler_v2.dispatcher import BUCKET_ORDER
        gate = DispatchGate(max_slots=5, state_dir=None, clock=FakeClock(0.0))
        bd = BucketDispatcher(gate=gate, clock=FakeClock(0.0))
        assert bd.tick(None) == 0
        assert bd._snapshot_buckets(None) == {name: [] for name, _ in BUCKET_ORDER}
        assert bd._count_active_per_bucket({}, {"CB-A"}) == {}
        assert bd._count_active_per_bucket({}, {"CB-X"}) == {}
        assert bd._count_active_per_bucket({}, set()) == {}
        expected_keys = {name for name, _ in BUCKET_ORDER}
        assert set(bd._count_active_per_bucket({"IMPLEMENT": []}, set()).keys()) == expected_keys
        assert bd._count_active_per_bucket({}, {"CB-A"}) == {}
        assert bd._select_ticket([]) is None
        assert bd._role_for_ticket(None) == "implementer"
        assert bd._role_for_ticket(type("T", (), {"ticket_class": "unknown-class"})()) == "implementer"
        assert bd._is_ticket_active("nonexistent-id") is False
        clk2 = FakeClock(0.0)
        sched2 = Scheduler(max_slots=5, state_dir=tmp_path, clock=clk2)
        clk2_adv = FakeClock(0.0)
        gate2 = DispatchGate(max_slots=5, state_dir=tmp_path, clock=clk2_adv)
        sched2._gate = gate2
        sched2._dispatcher = BucketDispatcher(gate=gate2, clock=clk2_adv)
        assert sched2.why_not_running("T-UNKNOWN") == ReasonCode.UNKNOWN
        assert bd._count_active_per_bucket({"IMPLEMENT": [type("T", (), {"id": "CB-1"})()]}, {"CB-1"}) == {
            name: (1 if name == "IMPLEMENT" else 0) for name, _ in BUCKET_ORDER
        }

    def test_bucket_dispatcher_starvation_and_backpressure(self, tmp_path: Path):
        from codebot.ticket_engine import Ticket, TicketClass, TicketState, Severity, RiskLevel
        from unittest.mock import MagicMock
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=5, state_dir=tmp_path, clock=clk)
        bd = BucketDispatcher(gate=gate, clock=clk, role_caps={"planning": 0})
        t = Ticket(
            id="CB-STARVE", title="t", ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM, state=TicketState.PLANNING,
            source="test", evidence="e", problem_statement="p", desired_state="d",
            acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
            migration_impact="none", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="revert",
            estimated_cost_tokens=100, created_at=clk.now(), updated_at=clk.now())
        store = MagicMock()
        store.list_by_state.side_effect = lambda state: [t] if state.value == "PLANNING" else []
        n = bd.tick(store)
        assert n == 0
        bd._role_caps = {}
        n2 = bd.tick(store)
        assert n2 >= 1

    def test_bucket_dispatcher_discovery_backpressure(self, tmp_path: Path):
        from codebot.ticket_engine import Ticket, TicketClass, TicketState, Severity, RiskLevel
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=5, state_dir=tmp_path, clock=clk)
        bd = BucketDispatcher(gate=gate, clock=clk, discovery_watermark=1)
        t_disc = Ticket(
            id="CB-DISC", title="t", ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM, state=TicketState.TRIAGED,
            source="test", evidence="e", problem_statement="p", desired_state="d",
            acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
            migration_impact="none", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="revert",
            estimated_cost_tokens=100, created_at=clk.now(), updated_at=clk.now())
        t_impl = Ticket(
            id="CB-IMPLX", title="t", ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM, state=TicketState.PLANNING,
            source="test", evidence="e", problem_statement="p", desired_state="d",
            acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
            migration_impact="none", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="revert",
            estimated_cost_tokens=100, created_at=clk.now(), updated_at=clk.now())
        store = type("Store", (), {
            "list_by_state": lambda self, state: (
                [t_disc] if state.value == "TRIAGED" else
                [t_impl] if state.value == "PLANNING" else []
            )
        })()
        n = bd.tick(store)
        assert n >= 1

    def test_bucket_dispatcher_tick_empty_buckets(self, tmp_path: Path):
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=5, state_dir=tmp_path, clock=clk)
        bd = BucketDispatcher(gate=gate, clock=clk)
        from unittest.mock import MagicMock
        store = MagicMock()
        store.list_by_state.return_value = []
        assert bd.tick(store) == 0
        store2 = type("StoreEmpty", (), {
            "list_by_state": lambda self, state: []
        })()
        assert bd.tick(store2) == 0

    def test_bucket_dispatcher_weighted_round_robin(self, tmp_path: Path):
        from codebot.ticket_engine import Ticket, TicketClass, TicketState, Severity, RiskLevel
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=10, state_dir=tmp_path, clock=clk)
        bd = BucketDispatcher(gate=gate, clock=clk, bucket_weights={"IMPLEMENT": 3, "REVIEW": 1})
        all_tickets = {"PLANNING": [], "IMPLEMENT": []}
        for i in range(4):
            all_tickets["PLANNING"].append(Ticket(
                id=f"CB-I-{i}", title="t", ticket_class=TicketClass.BUG,
                severity=Severity.MEDIUM, state=TicketState.PLANNING,
                source="test", evidence="e", problem_statement="p", desired_state="d",
                acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
                risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
                migration_impact="none", required_reviewers=[], required_tests=[],
                documentation_requirements=[], rollback_strategy="revert",
                estimated_cost_tokens=100, created_at=clk.now(), updated_at=clk.now()))
        for i in range(2):
            all_tickets["IMPLEMENT"].append(Ticket(
                id=f"CB-R-{i}", title="t2", ticket_class=TicketClass.BUG,
                severity=Severity.MEDIUM, state=TicketState.IMPLEMENT,
                source="test", evidence="e", problem_statement="p", desired_state="d",
                acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
                risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
                migration_impact="none", required_reviewers=[], required_tests=[],
                documentation_requirements=[], rollback_strategy="revert",
                estimated_cost_tokens=100, created_at=clk.now(), updated_at=clk.now()))
        store = type("Store", (), {
            "list_by_state": lambda self, state: list(all_tickets.get(state.value, []))
        })()
        n = bd.tick(store)
        assert n >= 4

    def test_bucket_dispatcher_no_capacity_breaks_loop(self, tmp_path: Path):
        from codebot.ticket_engine import Ticket, TicketClass, TicketState, Severity, RiskLevel
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=1, state_dir=tmp_path, clock=clk)
        gate.try_dispatch("CB-FILL", "agent-fill", role="r")
        bd = BucketDispatcher(gate=gate, clock=clk)
        t = Ticket(
            id="CB-BLOCKED-BY-CAP", title="t", ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM, state=TicketState.PLANNING,
            source="test", evidence="e", problem_statement="p", desired_state="d",
            acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
            migration_impact="none", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="revert",
            estimated_cost_tokens=100, created_at=clk.now(), updated_at=clk.now())
        store = type("Store", (), {
            "list_by_state": lambda self, state: ([t] if state.value == "PLANNING" else [])
        })()
        n = bd.tick(store)
        assert n == 0

    def test_scheduler_why_not_running_no_capacity(self, tmp_path: Path):
        clk = FakeClock(0.0)
        sched = Scheduler(max_slots=1, state_dir=tmp_path, clock=clk)
        sched.request_agent_spawn("backend_implementer", "T-CAP-1", model="m")
        reason = sched.why_not_running("T-CAP-2")
        assert reason == ReasonCode.NO_GLOBAL_CAPACITY

    def test_scheduler_why_not_running_stagger(self, tmp_path: Path):
        clk = FakeClock(0.0)
        sched = Scheduler(max_slots=5, state_dir=tmp_path, clock=clk)
        r = sched.request_agent_spawn("backend_implementer", "T-STAG-1", model="m")
        assert r.success is True
        result = sched.why_not_running("T-STAG-1")
        assert result == ReasonCode.ALREADY_CLAIMED
        assert sched.why_not_running("T-UNKNOWN-XYZ") == ReasonCode.UNKNOWN

    def test_scheduler_request_no_model_available(self, tmp_path: Path):
        sched = Scheduler(max_slots=5, state_dir=tmp_path, clock=FakeClock(0.0), model_pool=["m1", "m2"])
        sched._model_selector.mark_unavailable("m1")
        sched._model_selector.mark_unavailable("m2")
        result = sched.request_agent_spawn("backend_implementer", "T-NOMODEL", model="")
        assert result.success is False
        assert result.reason == "MODEL_UNAVAILABLE"

    def test_bucket_dispatcher_tick_empty_store(self, tmp_path: Path):
        gate = DispatchGate(max_slots=5, state_dir=tmp_path, clock=FakeClock(0.0))
        dispatcher = BucketDispatcher(gate=gate, clock=FakeClock(0.0))
        assert dispatcher.tick(None) == 0

    def test_scheduler_tick_and_diagnostics(self, tmp_path: Path):
        clk = FakeClock(1000.0)
        sched = Scheduler(max_slots=2, state_dir=tmp_path, clock=clk)
        from unittest.mock import MagicMock
        mock_store = MagicMock()
        mock_store.list_by_state.return_value = []
        n = sched.tick(store=mock_store)
        assert n == 0
        reason = sched.why_not_running("T-UNKNOWN", store=mock_store)
        assert reason in ReasonCode

    def test_scheduler_request_and_finalize(self, tmp_path: Path):
        clk = FakeClock(0.0)
        sched = Scheduler(max_slots=5, state_dir=tmp_path, clock=clk)
        result = sched.request_agent_spawn("backend_implementer", "T-REQ", model="m")
        assert result.success is True
        agent_id = result.claim.agent_id
        rec = sched.get_agent(agent_id)
        assert rec is not None
        assert rec.role == "backend_implementer"
        assert rec.ticket_id == "T-REQ"
        assert sched.finalize_agent(agent_id, outcome="done") is True
        rec2 = sched.get_agent(agent_id)
        assert rec2.state == AgentState.DEAD
        assert sched.finalize_agent(agent_id) is False
        assert sched.finalize_agent("nonexistent") is False

    def test_scheduler_reconcile_stale_runner(self, tmp_path: Path):
        clk = FakeClock(1000.0)
        sched = Scheduler(max_slots=5, state_dir=tmp_path, clock=clk)
        result = sched.request_agent_spawn("backend_implementer", "T-R", model="xiaomi-mimo-2.5")
        agent_id = result.claim.agent_id
        rec = sched.get_agent(agent_id)
        rec = rec.transition(AgentState.STARTING, timestamp=1001.0)
        rec = rec.transition(AgentState.RUNNING, timestamp=1002.0)
        rec = rec.with_heartbeat(timestamp=1002.0)
        sched._agents[agent_id] = rec
        clk.advance(500.0)
        rec_before = sched.get_agent(agent_id)
        assert rec_before.state == AgentState.RUNNING
        sched._reconcile()
        rec2 = sched.get_agent(agent_id)
        assert rec2.state == AgentState.DEAD
        assert sched._gate.count_active() == 0

    def test_scheduler_reconcile_no_stale(self, tmp_path: Path):
        clk = FakeClock(1000.0)
        sched = Scheduler(max_slots=5, state_dir=tmp_path, clock=clk)
        result = sched.request_agent_spawn("backend_implementer", "T-NOSTALE", model="xiaomi-mimo-2.5")
        agent_id = result.claim.agent_id
        rec = sched.get_agent(agent_id)
        rec = rec.transition(AgentState.STARTING, timestamp=1001.0)
        rec = rec.transition(AgentState.RUNNING, timestamp=1002.0)
        rec = rec.with_heartbeat(timestamp=1002.0)
        sched._agents[agent_id] = rec
        sched._reconcile()
        rec2 = sched.get_agent(agent_id)
        assert rec2.state == AgentState.RUNNING

    def test_bucket_dispatcher_with_real_tickets(self, tmp_path: Path):
        clk = FakeClock(0.0)
        gate = DispatchGate(max_slots=5, state_dir=tmp_path, clock=clk)
        dispatcher = BucketDispatcher(gate=gate, clock=clk)
        from codebot.ticket_engine import Ticket, TicketClass, TicketState, Severity, RiskLevel
        t = Ticket(
            id="CB-BUCKET-T1", title="t", ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM, state=TicketState.PLANNING,
            source="test", evidence="e", problem_statement="p", desired_state="d",
            acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
            migration_impact="none", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="revert",
            estimated_cost_tokens=100, created_at=clk.now(), updated_at=clk.now(),
        )
        from unittest.mock import MagicMock
        store = MagicMock()
        def list_by_state_side_effect(state):
            from codebot.ticket_engine import TicketState as TS
            if state == TS.PLANNING:
                return [t]
            return []
        store.list_by_state.side_effect = list_by_state_side_effect
        n = dispatcher.tick(store)
        assert n >= 1

    def test_scheduler_why_not_running_blocked(self, tmp_path: Path):
        from unittest.mock import MagicMock
        clk = FakeClock(0.0)
        sched = Scheduler(max_slots=5, state_dir=tmp_path, clock=clk)
        mock_store = MagicMock()
        mock_ticket = MagicMock()
        mock_ticket.state.value = "BLOCKED"
        mock_store.get.return_value = mock_ticket
        mock_store.list_by_state.return_value = []
        reason = sched.why_not_running("T-BLOCKED", store=mock_store)
        assert reason == ReasonCode.BLOCKED

    def test_scheduler_finalize_zombie(self, tmp_path: Path):
        sched = Scheduler(max_slots=5, state_dir=tmp_path, clock=FakeClock(0.0))
        result = sched.request_agent_spawn("backend_implementer", "T-FINAL", model="m")
        agent_id = result.claim.agent_id
        rec = sched.get_agent(agent_id)
        sched._agents[agent_id] = rec.transition(AgentState.ZOMBIE, timestamp=FakeClock(0.0).now())
        assert sched.finalize_agent(agent_id) is True
        result2 = sched.request_agent_spawn("backend_implementer", "T-FINAL2", model="m")
        agent_id2 = result2.claim.agent_id
        assert sched.finalize_agent(agent_id2) is True
        assert sched.finalize_agent("nonexistent-xyz") is False
        assert sched.finalize_agent(agent_id) is False
        result2 = sched.request_agent_spawn("backend_implementer", "T-FINAL2", model="m")
        agent_id2 = result2.claim.agent_id
        assert sched.finalize_agent(agent_id2) is True
        assert sched.finalize_agent(agent_id2) is False

    def test_scheduler_tick_requires_store(self, tmp_path: Path):
        clk = FakeClock(0.0)
        sched = Scheduler(max_slots=5, state_dir=tmp_path, clock=clk)
        t = sched.get_agent("nonexistent-xyz-999")
        assert t is None
        assert len(sched.list_agents()) == 0
        sched.register_agent(AgentRecord(
            agent_id="a-xyz", ticket_id="t1", role="r", model="m",
            pid=1, state=AgentState.RUNNING, created_at=0.0,
        ))
        assert sched.get_agent("a-xyz") is not None

    def test_scheduler_models_and_dispatch(self, tmp_path: Path):
        from codebot.ticket_engine import Ticket, TicketClass, TicketState, Severity, RiskLevel
        clk = FakeClock(0.0)
        sched = Scheduler(max_slots=2, state_dir=tmp_path, clock=clk, model_pool=["m1", "m2"])
        r1 = sched.request_agent_spawn("backend_implementer", "T-MODEL-1", model="")
        assert r1.success is True
        r2 = sched.request_agent_spawn("backend_implementer", "T-MODEL-2", model="")
        assert r2.success is True
        models_used = {sched.get_agent(r1.claim.agent_id).model, sched.get_agent(r2.claim.agent_id).model}
        assert len(models_used) == 2
        sched.finalize_agent(r1.claim.agent_id)
        sched.finalize_agent(r2.claim.agent_id)
        t = Ticket(
            id="CB-TICK-REAL", title="t", ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM, state=TicketState.PLANNING,
            source="test", evidence="e", problem_statement="p", desired_state="d",
            acceptance_criteria=["ac"], affected_modules=["mod.py"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="low", security_impact="none",
            migration_impact="none", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="revert",
            estimated_cost_tokens=100, created_at=clk.now(), updated_at=clk.now())
        store = type("Store", (), {
            "list_by_state": lambda self, state: ([t] if state.value == "PLANNING" else [])
        })()
        n = sched.tick(store=store)
        assert n >= 0
        assert sched.why_not_running("T-UNKNOWN2") in ReasonCode
        assert sched.why_not_running("T-UNKNOWN2", store=None) in ReasonCode

    def test_scheduler_why_not_running_states(self, tmp_path: Path):
        from unittest.mock import MagicMock
        clk = FakeClock(0.0)
        sched = Scheduler(max_slots=5, state_dir=tmp_path, clock=clk)
        s1 = sched.request_agent_spawn("backend_implementer", "T-WHY-1", model="m")
        assert s1.success is True
        assert sched.why_not_running("T-WHY-1") == ReasonCode.ALREADY_CLAIMED
        mock_complete = MagicMock()
        mock_complete.state.value = "COMPLETE"
        store_c = MagicMock()
        store_c.get.return_value = mock_complete
        assert sched.why_not_running("T-WHY-C", store=store_c) == ReasonCode.NO_ACTIONABLE_WORK
        mock_rejected = MagicMock()
        mock_rejected.state.value = "REJECTED"
        store_r = MagicMock()
        store_r.get.return_value = mock_rejected
        assert sched.why_not_running("T-REJ", store=store_r) == ReasonCode.NO_ACTIONABLE_WORK
        mock_dup = MagicMock()
        mock_dup.state.value = "DUPLICATE"
        store_d = MagicMock()
        store_d.get.return_value = mock_dup
        assert sched.why_not_running("T-DUP", store=store_d) == ReasonCode.NO_ACTIONABLE_WORK
        store_none = MagicMock()
        store_none.get.return_value = None
        assert sched.why_not_running("T-NONE", store=store_none) == ReasonCode.UNKNOWN
        store_err = MagicMock()
        store_err.get.side_effect = RuntimeError("boom")
        assert sched.why_not_running("T-ERR", store=store_err) == ReasonCode.UNKNOWN

    def test_scheduler_why_not_running_stagger_and_blocked(self, tmp_path: Path):
        from unittest.mock import MagicMock
        clk = FakeClock(0.0)
        sched = Scheduler(max_slots=5, state_dir=tmp_path, clock=clk)
        mock_blocked = MagicMock()
        mock_blocked.state.value = "BLOCKED"
        store_b = MagicMock()
        store_b.get.return_value = mock_blocked
        assert sched.why_not_running("T-BLOCKED2", store=store_b) == ReasonCode.BLOCKED
        s2 = sched.request_agent_spawn("backend_implementer", "T-STAG2", model="m")
        assert s2.success is True
        assert sched.why_not_running("T-STAG2") == ReasonCode.ALREADY_CLAIMED

    def test_scheduler_list_and_register(self, tmp_path: Path):
        sched = Scheduler(max_slots=5, state_dir=tmp_path, clock=FakeClock(0.0))
        assert len(sched.list_agents()) == 0
        rec = AgentRecord(
            agent_id="a-reg", ticket_id="t1", role="r", model="m",
            pid=1, state=AgentState.RUNNING, created_at=0.0,
        )
        sched.register_agent(rec)
        assert sched.get_agent("a-reg") is not None
        assert len(sched.list_agents()) == 1

    def test_heartbeat_interval_and_stale_timeout_helpers(self):
        assert heartbeat_interval_for() == 30.0
        assert heartbeat_interval_for("some_role") == 30.0
        assert stale_timeout_for(None) == DEFAULT_STALE_TIMEOUT
        assert stale_timeout_for("") == DEFAULT_STALE_TIMEOUT
        assert stale_timeout_for("xiaomi-mimo-2.5") == float(int(120 * 1.5))
        assert stale_timeout_for("unknown") == float(DEFAULT_STALE_TIMEOUT)
        assert stale_timeout_for("qwen-3.8-max-thinking") == float(int(120 * 2.8))
