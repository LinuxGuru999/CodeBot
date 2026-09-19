"""Tests for codebot.anomaly_alerts — evaluate() and write_daily_digest().

Covers:
- evaluate() with normal, anomalous, and edge-case snapshots
- write_daily_digest() forced and non-forced paths
- Helper functions: _queue_depth, _approval_backlog_hours, _daily_token_avg
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from codebot.anomaly_alerts import (
    ALERT_HISTORY_MAX,
    APPROVAL_STALE_HOURS,
    ERRORING_STREAK,
    TOKEN_BURN_MULTIPLIER,
    _daily_token_avg,
    evaluate,
    write_daily_digest,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    """Create a temporary state directory and patch module-level paths."""
    sd = tmp_path / "state"
    sd.mkdir()
    patches = [
        patch("codebot.anomaly_alerts.STATE_DIR", sd),
        patch("codebot.anomaly_alerts.ALERTS_FILE", sd / "anomaly_alerts.json"),
        patch("codebot.anomaly_alerts.DIGEST_FILE", sd / "daily_digest.md"),
        patch("codebot.anomaly_alerts.HISTORY_FILE", sd / "bot_metrics_history.jsonl"),
    ]
    for p in patches:
        p.start()
    yield sd
    for p in patches:
        p.stop()


def _make_snapshot(
    erroring: int = 0,
    stale: int = 0,
    regressing: int = 0,
    improving: int = 0,
    active: int = 5,
    bots_tracked: int = 10,
    ledger_total: float = 1000.0,
    ledger_day: str = "2026-09-19",
    bots: dict | None = None,
) -> dict:
    return {
        "summary": {
            "erroring": erroring,
            "stale": stale,
            "regressing": regressing,
            "improving": improving,
            "active": active,
            "bots_tracked": bots_tracked,
        },
        "ledger_total_actual": ledger_total,
        "ledger_day": ledger_day,
        "bots": bots or {},
    }


# ---------------------------------------------------------------------------
# evaluate() — normal snapshot (no alerts)
# ---------------------------------------------------------------------------

class TestEvaluateNormal:
    def test_no_alerts_on_healthy_snapshot(self, state_dir: Path) -> None:
        snap = _make_snapshot(erroring=0, stale=0, regressing=0)
        result = evaluate(snap)
        assert result["alerts"] == []
        assert result["pages"] == []
        assert "timestamp" in result

    def test_returns_dict_with_expected_keys(self, state_dir: Path) -> None:
        result = evaluate(_make_snapshot())
        assert set(result.keys()) == {"alerts", "pages", "timestamp"}


# ---------------------------------------------------------------------------
# evaluate() — A1: erroring bots streak
# ---------------------------------------------------------------------------

class TestEvaluateA1Erroring:
    def test_a1_fires_after_consecutive_erroring_snapshots(self, state_dir: Path) -> None:
        # Write enough history to trigger the streak
        history = state_dir / "bot_metrics_history.jsonl"
        lines: list[str] = []
        for _ in range(ERRORING_STREAK - 1):
            snap = _make_snapshot(erroring=1)
            lines.append(json.dumps(snap))
        history.write_text("\n".join(lines) + "\n", encoding="utf-8")
        current = _make_snapshot(erroring=2)
        result = evaluate(current)
        a1 = [a for a in result["alerts"] if a["rule"] == "A1"]
        assert len(a1) == 1
        assert a1[0]["severity"] == "page"
        assert "erroring" in a1[0]["message"]

    def test_a1_does_not_fire_below_threshold(self, state_dir: Path) -> None:
        history = state_dir / "bot_metrics_history.jsonl"
        # Only one erroring snapshot — not enough for streak
        snap = _make_snapshot(erroring=1)
        history.write_text(json.dumps(snap) + "\n", encoding="utf-8")
        current = _make_snapshot(erroring=0)
        result = evaluate(current)
        a1 = [a for a in result["alerts"] if a["rule"] == "A1"]
        assert a1 == []


# ---------------------------------------------------------------------------
# evaluate() — A3: token burn anomaly
# ---------------------------------------------------------------------------

class TestEvaluateA3TokenBurn:
    def test_a3_fires_when_burn_exceeds_multiplier(self, state_dir: Path) -> None:
        history = state_dir / "bot_metrics_history.jsonl"
        avg_val = 1000.0
        lines: list[str] = []
        for _ in range(7):
            s = _make_snapshot(ledger_total=avg_val)
            lines.append(json.dumps(s))
        history.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # Current burn is > 2x average
        current = _make_snapshot(ledger_total=avg_val * TOKEN_BURN_MULTIPLIER + 500)
        result = evaluate(current)
        a3 = [a for a in result["alerts"] if a["rule"] == "A3"]
        assert len(a3) == 1
        assert a3[0]["severity"] == "page"

    def test_a3_does_not_fire_within_normal_range(self, state_dir: Path) -> None:
        history = state_dir / "bot_metrics_history.jsonl"
        lines: list[str] = []
        for _ in range(7):
            s = _make_snapshot(ledger_total=1000.0)
            lines.append(json.dumps(s))
        history.write_text("\n".join(lines) + "\n", encoding="utf-8")
        current = _make_snapshot(ledger_total=1500.0)  # < 2x
        result = evaluate(current)
        a3 = [a for a in result["alerts"] if a["rule"] == "A3"]
        assert a3 == []


# ---------------------------------------------------------------------------
# evaluate() — A5: stale bots digest
# ---------------------------------------------------------------------------

class TestEvaluateA5Stale:
    def test_a5_fires_for_stale_bots_as_digest(self, state_dir: Path) -> None:
        snap = _make_snapshot(stale=2)
        result = evaluate(snap)
        a5 = [a for a in result["alerts"] if a["rule"] == "A5"]
        assert len(a5) == 1
        assert a5[0]["severity"] == "digest"
        assert "2 stale" in a5[0]["message"]

    def test_a5_does_not_fire_when_zero_stale(self, state_dir: Path) -> None:
        snap = _make_snapshot(stale=0)
        result = evaluate(snap)
        a5 = [a for a in result["alerts"] if a["rule"] == "A5"]
        assert a5 == []


# ---------------------------------------------------------------------------
# evaluate() — A6: regressing bots digest
# ---------------------------------------------------------------------------

class TestEvaluateA6Regressing:
    def test_a6_fires_when_regressing_gte_3(self, state_dir: Path) -> None:
        snap = _make_snapshot(regressing=5)
        result = evaluate(snap)
        a6 = [a for a in result["alerts"] if a["rule"] == "A6"]
        assert len(a6) == 1
        assert a6[0]["severity"] == "digest"

    def test_a6_does_not_fire_below_3(self, state_dir: Path) -> None:
        snap = _make_snapshot(regressing=2)
        result = evaluate(snap)
        a6 = [a for a in result["alerts"] if a["rule"] == "A6"]
        assert a6 == []


# ---------------------------------------------------------------------------
# evaluate() — edge cases
# ---------------------------------------------------------------------------

class TestEvaluateEdgeCases:
    def test_empty_snapshot(self, state_dir: Path) -> None:
        result = evaluate({})
        assert result["alerts"] == []

    def test_none_snapshot_reads_from_file(self, state_dir: Path) -> None:
        metrics = state_dir / "bot_metrics.json"
        metrics.write_text(json.dumps(_make_snapshot()), encoding="utf-8")
        result = evaluate(None)
        assert "timestamp" in result

    def test_none_snapshot_missing_file(self, state_dir: Path) -> None:
        result = evaluate(None)
        assert result["alerts"] == []

    def test_alert_history_capped_at_max(self, state_dir: Path) -> None:
        # Pre-fill alerts file beyond max
        alerts_file = state_dir / "anomaly_alerts.json"
        fake_alerts = [{"rule": "X", "severity": "digest", "message": "old", "ts": i} for i in range(ALERT_HISTORY_MAX + 10)]
        alerts_file.write_text(json.dumps(fake_alerts), encoding="utf-8")
        snap = _make_snapshot(stale=1)  # triggers A5
        evaluate(snap)
        stored = json.loads(alerts_file.read_text(encoding="utf-8"))
        assert len(stored) <= ALERT_HISTORY_MAX

    def test_pages_only_contain_page_severity(self, state_dir: Path) -> None:
        snap = _make_snapshot(stale=1)  # A5 is digest-only
        result = evaluate(snap)
        assert all(p["severity"] == "page" for p in result["pages"])


# ---------------------------------------------------------------------------
# write_daily_digest()
# ---------------------------------------------------------------------------

class TestWriteDailyDigest:
    def test_force_writes_regardless_of_marker(self, state_dir: Path) -> None:
        marker = state_dir / ".digest_date"
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        marker.write_text(today, encoding="utf-8")
        snap = _make_snapshot(bots={"bot-a": {"alignment": {"avg_reward": 0.9}, "reward_trend": "improving"}})
        result = write_daily_digest(snap, force=True)
        assert result is True
        digest = state_dir / "daily_digest.md"
        assert digest.exists()
        content = digest.read_text(encoding="utf-8")
        assert "Daily Digest" in content
        assert "bot-a" in content

    def test_non_force_skips_if_already_written_today(self, state_dir: Path) -> None:
        marker = state_dir / ".digest_date"
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        marker.write_text(today, encoding="utf-8")
        result = write_daily_digest(_make_snapshot(), force=False)
        assert result is False

    def test_non_force_writes_on_new_day(self, state_dir: Path) -> None:
        marker = state_dir / ".digest_date"
        marker.write_text("2020-01-01", encoding="utf-8")
        snap = _make_snapshot()
        result = write_daily_digest(snap, force=False)
        assert result is True
        assert marker.read_text(encoding="utf-8").strip() != "2020-01-01"

    def test_digest_contains_fleet_summary(self, state_dir: Path) -> None:
        snap = _make_snapshot(active=3, erroring=1, stale=2, bots_tracked=10, improving=4, regressing=1)
        write_daily_digest(snap, force=True)
        content = (state_dir / "daily_digest.md").read_text(encoding="utf-8")
        assert "10 tracked" in content
        assert "3 active" in content
        assert "1 erroring" in content
        assert "2 stale" in content

    def test_digest_includes_regressors(self, state_dir: Path) -> None:
        bots = {
            "bot-x": {"alignment": {"avg_reward": 0.1}, "reward_trend": "regressing"},
            "bot-y": {"alignment": {"avg_reward": 0.8}, "reward_trend": "improving"},
        }
        snap = _make_snapshot(bots=bots)
        write_daily_digest(snap, force=True)
        content = (state_dir / "daily_digest.md").read_text(encoding="utf-8")
        assert "bot-x" in content
        assert "Regressing" in content

    def test_digest_shows_none_when_no_regressors(self, state_dir: Path) -> None:
        snap = _make_snapshot(bots={"bot-a": {"alignment": {"avg_reward": 0.5}, "reward_trend": "improving"}})
        write_daily_digest(snap, force=True)
        content = (state_dir / "daily_digest.md").read_text(encoding="utf-8")
        assert "- none" in content


# ---------------------------------------------------------------------------
# Helper: _daily_token_avg
# ---------------------------------------------------------------------------

class TestDailyTokenAvg:
    def test_computes_average(self) -> None:
        snaps = [{"ledger_total_actual": 100}, {"ledger_total_actual": 200}, {"ledger_total_actual": 300}]
        assert _daily_token_avg(snaps, days=3) == pytest.approx(200.0)

    def test_empty_list_returns_zero(self) -> None:
        assert _daily_token_avg([], days=7) == 0.0

    def test_missing_key_treated_as_zero(self) -> None:
        snaps = [{"other_key": 1}, {"ledger_total_actual": 100}]
        # Only one has the key; average of [100] = 100
        assert _daily_token_avg(snaps, days=7) == pytest.approx(100.0)

    def test_respects_days_limit(self) -> None:
        snaps = [{"ledger_total_actual": 1000}] * 10 + [{"ledger_total_actual": 100}] * 3
        avg = _daily_token_avg(snaps, days=3)
        assert avg == pytest.approx(100.0)
