"""Tests for codebot/adaptive_rate_limiter.py — rate limiting, backoff, cooldown."""
import queue
import threading
import time
from unittest.mock import MagicMock, patch, call

import pytest

from codebot.adaptive_rate_limiter import ModelRateState, AdaptiveRateLimiter
try:
    from codebot.adaptive_rate_limiter import QueuedRequest
except ImportError:
    # Fallback: QueuedRequest is now defined in adaptive_rate_limiter.py itself
    pass


# ===========================================================================
# ModelRateState
# ===========================================================================

class TestModelRateState:
    def test_default_values(self):
        s = ModelRateState(model="test")
        assert s.learned_rpm == pytest.approx(60.0)
        assert s.min_interval == pytest.approx(1.0)
        assert s.last_request == pytest.approx(0.0)
        assert s.consecutive_rate_limits == 0

    def test_effective_interval_no_backoff(self):
        s = ModelRateState(model="m", min_interval=2.0)
        assert s.effective_interval == pytest.approx(2.0)

    @pytest.mark.parametrize("consecutive,expected", [
        (1, 2.0),
        (2, 4.0),
        (3, 8.0),
        (4, 16.0),
        (5, 16.0),
        (10, 16.0),
    ])
    def test_effective_interval_exponential_capped(self, consecutive, expected):
        s = ModelRateState(model="m", min_interval=1.0, consecutive_rate_limits=consecutive)
        assert s.effective_interval == pytest.approx(expected)

    def test_effective_interval_custom_min(self):
        s = ModelRateState(model="m", min_interval=0.5, consecutive_rate_limits=2)
        assert s.effective_interval == pytest.approx(2.0)

    def test_record_request_updates_time_and_count(self):
        s = ModelRateState(model="m")
        now = 1000.0
        with patch("codebot.adaptive_rate_limiter.time.time", return_value=now):
            s.record_request()
        assert s.last_request == pytest.approx(now)
        assert s.total_requests == 1
        with patch("codebot.adaptive_rate_limiter.time.time", return_value=now + 1):
            s.record_request()
        assert s.total_requests == 2

    def test_record_rate_limit_doubles_interval(self):
        s = ModelRateState(model="m", min_interval=1.0)
        with patch("codebot.adaptive_rate_limiter.time.time", return_value=1000.0):
            s.record_rate_limit()
        assert s.min_interval == pytest.approx(2.0)
        assert s.consecutive_rate_limits == 1
        assert s.total_rate_limits == 1

    def test_record_rate_limit_capped_at_30(self):
        s = ModelRateState(model="m", min_interval=20.0)
        with patch("codebot.adaptive_rate_limiter.time.time", return_value=1000.0):
            s.record_rate_limit()
        assert s.min_interval == pytest.approx(30.0)

    def test_record_rate_limit_with_retry_after(self):
        s = ModelRateState(model="m", min_interval=1.0)
        with patch("codebot.adaptive_rate_limiter.time.time", return_value=1000.0):
            s.record_rate_limit(retry_after=5.0)
        assert s.min_interval == pytest.approx(5.0)
        assert s.consecutive_rate_limits == 1

    def test_record_rate_limit_retry_after_smaller_than_current(self):
        s = ModelRateState(model="m", min_interval=5.0)
        with patch("codebot.adaptive_rate_limiter.time.time", return_value=1000.0):
            s.record_rate_limit(retry_after=2.0)
        assert s.min_interval == pytest.approx(5.0)

    def test_record_rate_limit_learned_rpm(self):
        s = ModelRateState(model="m", min_interval=2.0)
        with patch("codebot.adaptive_rate_limiter.time.time", return_value=1000.0):
            s.record_rate_limit()
        assert s.learned_rpm == pytest.approx(60.0 / s.min_interval)

    def test_record_rate_limit_learned_rpm_at_least_1(self):
        s = ModelRateState(model="m", min_interval=30.0)
        with patch("codebot.adaptive_rate_limiter.time.time", return_value=1000.0):
            s.record_rate_limit()
        assert s.learned_rpm >= 1.0

    def test_record_rate_limit_sets_last_rate_limit(self):
        s = ModelRateState(model="m")
        now = 1234.5
        with patch("codebot.adaptive_rate_limiter.time.time", return_value=now):
            s.record_rate_limit()
        assert s.last_rate_limit == pytest.approx(now)

    def test_record_success_decrements_consecutive(self):
        s = ModelRateState(model="m", consecutive_rate_limits=3, min_interval=4.0)
        s.record_success()
        assert s.consecutive_rate_limits == 2
        assert s.min_interval == pytest.approx(4.0)

    def test_record_success_to_zero_reduces_interval(self):
        s = ModelRateState(model="m", consecutive_rate_limits=1, min_interval=4.0)
        s.record_success()
        assert s.consecutive_rate_limits == 0
        assert s.min_interval == pytest.approx(3.6)
        assert s.learned_rpm == pytest.approx(60.0 / 3.6)

    def test_record_success_floor_at_0_5(self):
        s = ModelRateState(model="m", consecutive_rate_limits=1, min_interval=0.5)
        s.record_success()
        assert s.min_interval == pytest.approx(0.5)

    def test_record_success_below_floor(self):
        s = ModelRateState(model="m", consecutive_rate_limits=1, min_interval=0.4)
        s.record_success()
        assert s.min_interval == pytest.approx(0.5)

    def test_record_success_no_change_when_already_zero(self):
        s = ModelRateState(model="m", consecutive_rate_limits=0, min_interval=2.0)
        s.record_success()
        assert s.consecutive_rate_limits == 0
        assert s.min_interval == pytest.approx(2.0)

    def test_record_success_multiple_steps_to_zero(self):
        s = ModelRateState(model="m", consecutive_rate_limits=2, min_interval=2.0)
        s.record_success()
        assert s.consecutive_rate_limits == 1
        s.record_success()
        assert s.consecutive_rate_limits == 0
        assert s.min_interval == pytest.approx(1.8)

    def test_consecutive_rate_limits_accumulate(self):
        s = ModelRateState(model="m", min_interval=1.0)
        for _ in range(3):
            with patch("codebot.adaptive_rate_limiter.time.time", return_value=1000.0):
                s.record_rate_limit()
        assert s.consecutive_rate_limits == 3
        assert s.total_rate_limits == 3


class TestQueuedRequest:
    def test_defaults(self):
        q = QueuedRequest(model="m", bot_name="bot", callback=lambda: None)
        assert q.args == ()
        assert q.kwargs == {}
        assert q.priority == 0
        assert q.enqueued_at > 0

    def test_custom_args(self):
        cb = lambda x: x
        q = QueuedRequest(model="m", bot_name="bot", callback=cb, args=(1, 2), kwargs={"a": 1}, priority=5)
        assert q.args == (1, 2)
        assert q.kwargs == {"a": 1}
        assert q.priority == 5


class TestAdaptiveRateLimiterGetState:
    def test_default_rpm_known_model(self):
        rl = AdaptiveRateLimiter()
        s = rl._get_state("qwen-3.8-max")
        assert s.learned_rpm == pytest.approx(20)
        assert s.min_interval == pytest.approx(3.0)

    def test_default_rpm_unknown_model(self):
        rl = AdaptiveRateLimiter()
        s = rl._get_state("unknown-model-xyz")
        assert s.learned_rpm == pytest.approx(30)
        assert s.min_interval == pytest.approx(2.0)

    def test_default_limits_coverage(self):
        rl = AdaptiveRateLimiter()
        assert rl._default_limits["qwen-3.5-plus"] == 30
        assert rl._default_limits["qwen-3.8-max-thinking"] == 10

    def test_get_state_cached(self):
        rl = AdaptiveRateLimiter()
        s1 = rl._get_state("m")
        s2 = rl._get_state("m")
        assert s1 is s2

    def test_get_queue_creates(self):
        rl = AdaptiveRateLimiter()
        q1 = rl._get_queue("m")
        q2 = rl._get_queue("m")
        assert q1 is q2

    def test_different_models_different_queues(self):
        rl = AdaptiveRateLimiter()
        q1 = rl._get_queue("m1")
        q2 = rl._get_queue("m2")
        assert q1 is not q2


class TestCanSpawnNow:
    def test_ok_when_no_recent_request(self):
        rl = AdaptiveRateLimiter()
        with patch("codebot.adaptive_rate_limiter.time.time", return_value=1000.0):
            ok, reason = rl.can_spawn_now("unknown-model-xyz")
            assert ok is True
            assert reason == "ok"

    def test_backoff_when_recent(self):
        rl = AdaptiveRateLimiter()
        now = 1000.0
        with patch("codebot.adaptive_rate_limiter.time.time", return_value=now):
            s = rl._get_state("test-model")
            s.min_interval = 5.0
            s.last_request = now - 2.0
        with patch("codebot.adaptive_rate_limiter.time.time", return_value=now):
            ok, reason = rl.can_spawn_now("test-model")
            assert ok is False
            assert "backoff" in reason

    def test_backoff_value(self):
        rl = AdaptiveRateLimiter()
        now = 1000.0
        with patch("codebot.adaptive_rate_limiter.time.time", return_value=now):
            s = rl._get_state("m")
            s.min_interval = 10.0
            s.last_request = now - 3.0
        with patch("codebot.adaptive_rate_limiter.time.time", return_value=now):
            ok, reason = rl.can_spawn_now("m")
            assert "7.0s" in reason

    def test_queue_full(self):
        rl = AdaptiveRateLimiter()
        q = rl._get_queue("m")
        for _ in range(11):
            q.put(MagicMock())
        s = rl._get_state("m")
        s.last_request = 0
        s.min_interval = 0.1
        with patch("codebot.adaptive_rate_limiter.time.time", return_value=10000.0):
            ok, reason = rl.can_spawn_now("m")
            assert ok is False
            assert "queue full" in reason
        while not q.empty():
            q.get()

    def test_exactly_10_not_full(self):
        rl = AdaptiveRateLimiter()
        q = rl._get_queue("m")
        for _ in range(10):
            q.put(MagicMock())
        s = rl._get_state("m")
        s.last_request = 0
        s.min_interval = 0.1
        with patch("codebot.adaptive_rate_limiter.time.time", return_value=10000.0):
            ok, _ = rl.can_spawn_now("m")
            assert ok is True
        while not q.empty():
            q.get()

    def test_backoff_takes_precedence_over_queue_full(self):
        rl = AdaptiveRateLimiter()
        q = rl._get_queue("m")
        for _ in range(20):
            q.put(MagicMock())
        s = rl._get_state("m")
        s.min_interval = 10.0
        s.last_request = time.time()
        ok, reason = rl.can_spawn_now("m")
        assert ok is False
        assert "backoff" in reason
        while not q.empty():
            q.get()


class TestRecordMethods:
    def test_record_rate_limit_thread_safe(self):
        rl = AdaptiveRateLimiter()
        rl.record_rate_limit("m", retry_after=5.0)
        s = rl._get_state("m")
        assert s.min_interval >= 5.0
        assert s.consecutive_rate_limits == 1

    def test_record_success_thread_safe(self):
        rl = AdaptiveRateLimiter()
        s = rl._get_state("m")
        s.consecutive_rate_limits = 2
        rl.record_success("m")
        assert s.consecutive_rate_limits == 1

    def test_record_rate_limit_without_retry_after(self):
        rl = AdaptiveRateLimiter()
        s = rl._get_state("m")
        initial = s.min_interval
        rl.record_rate_limit("m")
        assert s.min_interval == pytest.approx(min(initial * 2, 30.0))


class TestEnqueue:
    def test_enqueue_adds_to_queue(self):
        rl = AdaptiveRateLimiter()
        with patch.object(rl, "ensure_worker"):
            rl.enqueue("m", "bot-a", lambda: 42)
        assert rl.get_queue_size("m") == 1
        rl._get_queue("m").get()

    def test_enqueue_with_args(self):
        rl = AdaptiveRateLimiter()
        with patch.object(rl, "ensure_worker"):
            rl.enqueue("m", "bot-a", lambda x: x, args=(1,), kwargs={"k": 2}, priority=1)
        q = rl._get_queue("m")
        req = q.get()
        assert req.args == (1,)
        assert req.kwargs == {"k": 2}
        assert req.priority == 1

    def test_enqueue_calls_ensure_worker(self):
        rl = AdaptiveRateLimiter()
        with patch.object(rl, "ensure_worker") as mock_ensure:
            rl.enqueue("m", "bot-a", lambda: None)
            mock_ensure.assert_called_once_with("m")
        rl._get_queue("m").get()

    def test_enqueue_shutdown_noop(self):
        rl = AdaptiveRateLimiter()
        rl._shutdown = True
        rl.enqueue("m", "bot-a", lambda: None)
        assert rl.get_queue_size("m") == 0

    def test_enqueue_multiple(self):
        rl = AdaptiveRateLimiter()
        with patch.object(rl, "ensure_worker"):
            for i in range(5):
                rl.enqueue("m", f"bot-{i}", lambda: None)
        assert rl.get_queue_size("m") == 5
        for _ in range(5):
            rl._get_queue("m").get()


class TestEnsureWorker:
    def test_starts_thread(self):
        rl = AdaptiveRateLimiter()
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        with patch("codebot.adaptive_rate_limiter.threading.Thread", return_value=mock_thread) as mock_cls:
            rl.ensure_worker("m")
            mock_cls.assert_called_once()
            mock_thread.start.assert_called_once()
            assert rl._threads["m"] is mock_thread

    def test_no_restart_if_alive(self):
        rl = AdaptiveRateLimiter()
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        rl._threads["m"] = mock_thread
        with patch("codebot.adaptive_rate_limiter.threading.Thread") as mock_cls:
            rl.ensure_worker("m")
            mock_cls.assert_not_called()

    def test_restarts_if_dead(self):
        rl = AdaptiveRateLimiter()
        mock_old = MagicMock()
        mock_old.is_alive.return_value = False
        rl._threads["m"] = mock_old
        mock_new = MagicMock()
        mock_new.is_alive.return_value = True
        with patch("codebot.adaptive_rate_limiter.threading.Thread", return_value=mock_new):
            rl.ensure_worker("m")
            assert rl._threads["m"] is mock_new
            mock_new.start.assert_called_once()


class TestGetQueueSize:
    def test_empty(self):
        rl = AdaptiveRateLimiter()
        assert rl.get_queue_size("new-model") == 0

    def test_with_items(self):
        rl = AdaptiveRateLimiter()
        with patch.object(rl, "ensure_worker"):
            rl.enqueue("m", "bot", lambda: None)
            rl.enqueue("m", "bot", lambda: None)
        assert rl.get_queue_size("m") == 2
        for _ in range(2):
            rl._get_queue("m").get()

    def test_different_models_isolated(self):
        rl = AdaptiveRateLimiter()
        with patch.object(rl, "ensure_worker"):
            rl.enqueue("m1", "bot", lambda: None)
        assert rl.get_queue_size("m1") == 1
        assert rl.get_queue_size("m2") == 0
        rl._get_queue("m1").get()


class TestGetStats:
    def test_empty(self):
        rl = AdaptiveRateLimiter()
        stats = rl.get_stats()
        assert stats["models"] == {}

    def test_with_models(self):
        rl = AdaptiveRateLimiter()
        rl._get_state("m1")
        rl._get_state("m2")
        stats = rl.get_stats()
        assert "m1" in stats["models"]
        assert "m2" in stats["models"]
        for m, data in stats["models"].items():
            assert "learned_rpm" in data
            assert "min_interval" in data
            assert "effective_interval" in data
            assert "total_requests" in data
            assert "total_rate_limits" in data
            assert "consecutive_rate_limits" in data
            assert "queue_size" in data

    def test_stats_reflect_backoff(self):
        rl = AdaptiveRateLimiter()
        s = rl._get_state("m")
        s.consecutive_rate_limits = 2
        stats = rl.get_stats()
        assert stats["models"]["m"]["effective_interval"] == pytest.approx(s.effective_interval)
        assert stats["models"]["m"]["consecutive_rate_limits"] == 2

    def test_stats_queue_size(self):
        rl = AdaptiveRateLimiter()
        with patch.object(rl, "ensure_worker"):
            rl.enqueue("m", "bot", lambda: None)
        stats = rl.get_stats()
        assert stats["models"]["m"]["queue_size"] == 1
        rl._get_queue("m").get()


class TestProcessQueue:
    def test_processes_success(self):
        rl = AdaptiveRateLimiter()
        s = rl._get_state("m")
        s.last_request = time.time() - 100
        q = rl._get_queue("m")
        called = []

        def cb():
            called.append(1)
            return "ok"

        with patch.object(rl, "ensure_worker"):
            rl.enqueue("m", "bot", cb)
        rl._shutdown = False
        orig_get = queue.Queue.get

        def fake_get(self_, timeout=1.0):
            if not hasattr(fake_get, "called"):
                fake_get.called = True  # type: ignore
                return orig_get(q, timeout=0.1)
            rl._shutdown = True
            raise queue.Empty()

        with patch.object(queue.Queue, "get", fake_get):
            with patch("codebot.adaptive_rate_limiter.time.sleep"):
                rl._process_queue("m")
        assert len(called) == 1
        assert s.consecutive_rate_limits == 0
        rl._shutdown = False
        while not q.empty():
            q.get()

    def test_requeues_on_rate_limit(self):
        rl = AdaptiveRateLimiter()
        s = rl._get_state("m")
        s.last_request = time.time() - 100
        q = rl._get_queue("m")

        def failing_cb():
            raise RuntimeError("429 rate limit exceeded")

        req = QueuedRequest(model="m", bot_name="bot", callback=failing_cb)
        q.put(req)
        rl._shutdown = False
        orig_get = queue.Queue.get

        def fake_get(self_, timeout=1.0):
            if not hasattr(fake_get, "count"):
                fake_get.count = 0  # type: ignore
            fake_get.count += 1  # type: ignore
            if fake_get.count == 1:  # type: ignore
                return orig_get(q, timeout=0.1)
            rl._shutdown = True
            raise queue.Empty()

        with patch.object(queue.Queue, "get", fake_get):
            with patch("codebot.adaptive_rate_limiter.time.sleep"):
                rl._process_queue("m")
        assert s.consecutive_rate_limits == 1
        assert s.total_rate_limits == 1
        assert q.qsize() >= 1
        while not q.empty():
            q.get()
        rl._shutdown = False

    def test_rate_limit_detection_variants(self):
        for msg in ["429 error", "rate limited", "limit exceeded"]:
            rl = AdaptiveRateLimiter()
            s = rl._get_state("m")
            s.last_request = time.time() - 100
            s.consecutive_rate_limits = 0
            q = queue.Queue()
            rl._queues["m"] = q
            rl._models["m"] = s

            def cb(m=msg):
                raise RuntimeError(m)

            req = QueuedRequest(model="m", bot_name="bot", callback=cb)
            q.put(req)
            rl._shutdown = False
            orig_get = queue.Queue.get

            def fake_get(self_, timeout=1.0):
                if not hasattr(fake_get, "count"):
                    fake_get.count = 0  # type: ignore
                fake_get.count += 1  # type: ignore
                if fake_get.count == 1:  # type: ignore
                    return orig_get(q, timeout=0.1)
                rl._shutdown = True
                raise queue.Empty()

            with patch.object(queue.Queue, "get", fake_get):
                with patch("codebot.adaptive_rate_limiter.time.sleep"):
                    rl._process_queue("m")
            assert s.consecutive_rate_limits == 1, f"failed for {msg}"
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
            s.consecutive_rate_limits = 0
            rl._shutdown = False

    def test_non_rate_error_raises(self):
        rl = AdaptiveRateLimiter()
        s = rl._get_state("m")
        s.last_request = time.time() - 100
        q = queue.Queue()
        rl._queues["m"] = q

        def bad_cb():
            raise ValueError("something went badly")

        req = QueuedRequest(model="m", bot_name="bot", callback=bad_cb)
        q.put(req)
        rl._shutdown = False

        def fake_get(self_, timeout=1.0):
            if not hasattr(fake_get, "done"):
                fake_get.done = True  # type: ignore
                return req
            rl._shutdown = True
            raise queue.Empty()

        with patch.object(queue.Queue, "get", fake_get):
            with patch("codebot.adaptive_rate_limiter.time.sleep"):
                with pytest.raises(ValueError, match="something went badly"):
                    rl._process_queue("m")
        rl._shutdown = False
        while not q.empty():
            try:
                q.get_nowait()
            except queue.Empty:
                break

    def test_delay_sleep(self):
        rl = AdaptiveRateLimiter()
        s = rl._get_state("m")
        now = 1000.0
        s.min_interval = 5.0
        s.last_request = now - 2.0
        q = queue.Queue()
        rl._queues["m"] = q
        rl._models["m"] = s
        q.put(QueuedRequest(model="m", bot_name="bot", callback=lambda: None))
        rl._shutdown = False
        orig_get = queue.Queue.get

        def fake_get(self_, timeout=1.0):
            if not hasattr(fake_get, "count"):
                fake_get.count = 0  # type: ignore
            fake_get.count += 1  # type: ignore
            if fake_get.count == 1:  # type: ignore
                return orig_get(q, timeout=0.1)
            rl._shutdown = True
            raise queue.Empty()

        with patch.object(queue.Queue, "get", fake_get):
            with patch("codebot.adaptive_rate_limiter.time.time", return_value=now):
                with patch("codebot.adaptive_rate_limiter.time.sleep") as mock_sleep:
                    rl._process_queue("m")
                    mock_sleep.assert_called_once()
                    assert mock_sleep.call_args[0][0] == pytest.approx(3.0)
        rl._shutdown = False

    def test_none_sentinel_breaks(self):
        rl = AdaptiveRateLimiter()
        q = queue.Queue()
        rl._queues["m"] = q
        q.put(None)
        rl._shutdown = False
        rl._process_queue("m")
        assert q.empty()
        rl._shutdown = False


class TestShutdown:
    def test_shutdown_sets_flag_and_joins(self):
        rl = AdaptiveRateLimiter()
        mock_q = MagicMock(spec=queue.Queue)
        mock_thread = MagicMock()
        rl._queues["m"] = mock_q
        rl._threads["m"] = mock_thread
        rl.shutdown()
        assert rl._shutdown is True
        mock_q.put.assert_called_with(None)
        mock_thread.join.assert_called_once_with(timeout=5.0)

    def test_shutdown_empty(self):
        rl = AdaptiveRateLimiter()
        rl.shutdown()
        assert rl._shutdown is True


class TestGlobalInstance:
    def test_global_exists(self):
        from codebot.adaptive_rate_limiter import rate_limiter
        assert isinstance(rate_limiter, AdaptiveRateLimiter)

    def test_cooldown_period_recovery(self):
        s = ModelRateState(model="m", min_interval=4.0, consecutive_rate_limits=1)
        assert s.effective_interval == pytest.approx(8.0)
        s.record_success()
        assert s.consecutive_rate_limits == 0
        assert s.min_interval == pytest.approx(3.6)

    def test_requeue_timing_backoff_increases(self):
        s = ModelRateState(model="m", min_interval=1.0)
        intervals = []
        for _ in range(3):
            with patch("codebot.adaptive_rate_limiter.time.time", return_value=1000.0):
                s.record_rate_limit()
            intervals.append(s.effective_interval)
        assert intervals[0] < intervals[1] < intervals[2]
        assert intervals[0] == pytest.approx(4.0)
        assert intervals[1] == pytest.approx(16.0)
        assert intervals[2] == pytest.approx(64.0)
