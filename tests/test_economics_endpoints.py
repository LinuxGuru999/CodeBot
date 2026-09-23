"""Tests for economics endpoints in control_server.py.

Covers GET /economics/summary and GET /economics/budget-status endpoints,
including budget alerting logic at 80%/90% thresholds.
"""
import json
import time
from unittest.mock import patch, MagicMock

import pytest


class TestEconomicsBudgetStatus:
    """Tests for economics_budget_status() helper."""

    def test_economics_budget_status_returns_dict(self):
        """budget_status returns correct keys with mocked token_budget data."""
        from codebot.control_server import economics_budget_status

        mock_day = "2026-09-21"
        mock_total = 3_200_000_000  # 80% of 4B cap

        with patch("codebot.control_server.STATE_DIR", MagicMock()), \
             patch("codebot.control_server.json") as mock_json, \
             patch("codebot.control_server.Path") as mock_path:

            mock_ledger_path = MagicMock()
            mock_ledger_path.exists.return_value = True
            mock_ledger_path.read_text.return_value = json.dumps({
                "day_utc": mock_day,
                "by_model": {
                    "gpt-4o": {"prompt_actual": 1000, "completion_actual": 500},
                },
            })
            mock_path.return_value = mock_ledger_path

            with patch("codebot.token_budget.current_day_utc", return_value=mock_day), \
                 patch("codebot.token_budget.day_total", return_value=mock_total), \
                 patch("codebot.token_budget.get_budget_state", return_value="warn"), \
                 patch("codebot.token_budget.CAP", 4_000_000_000):

                result = economics_budget_status()

        assert isinstance(result, dict)
        assert "budget_cap" in result
        assert "budget_used" in result
        assert "budget_remaining" in result
        assert "budget_pct" in result
        assert "budget_state" in result
        assert "day_utc" in result
        assert "per_model_actual" in result
        assert result["budget_cap"] == 4_000_000_000
        assert result["budget_used"] == 3_200_000_000
        assert result["budget_state"] == "warn"

    def test_economics_budget_status_handles_import_error(self):
        """budget_status returns safe defaults when token_budget is unavailable."""
        # Simulate ImportError by patching the import to fail
        with patch.dict("sys.modules", {"codebot.token_budget": None}):
            # Force reimport scenario by using a fresh call
            from codebot.control_server import economics_budget_status
            result = economics_budget_status()

        assert isinstance(result, dict)
        assert result["budget_state"] == "budget-unknown"
        assert result["budget_used"] == 0


class TestCheckBudgetAlerts:
    """Tests for _check_budget_alerts() helper."""

    def test_budget_alerts_trigger_at_thresholds(self):
        """alerts list populated when usage crosses 80% and 90% thresholds."""
        from codebot.control_server import _check_budget_alerts

        # Test 80% threshold (warn)
        alerts_80 = _check_budget_alerts(80.0, "warn")
        assert len(alerts_80) == 1
        assert alerts_80[0]["threshold"] == 80
        assert alerts_80[0]["level"] == "warn"

        # Test 90% threshold (critical)
        alerts_90 = _check_budget_alerts(90.0, "shed_tier3")
        assert len(alerts_90) == 1
        assert alerts_90[0]["threshold"] == 90
        assert alerts_90[0]["level"] == "critical"

        # Test 100% (stop state)
        alerts_stop = _check_budget_alerts(100.0, "stop")
        assert len(alerts_stop) >= 1
        assert any(a["level"] == "critical" for a in alerts_stop)

    def test_budget_alerts_clean_below_threshold(self):
        """no alerts when usage is under 80%."""
        from codebot.control_server import _check_budget_alerts

        alerts = _check_budget_alerts(50.0, "ok")
        assert len(alerts) == 0

        alerts = _check_budget_alerts(79.9, "ok")
        assert len(alerts) == 0


class TestEconomicsSummary:
    """Tests for economics_summary() helper."""

    def test_economics_summary_returns_dict(self):
        """summary returns combined budget+cost data with correct structure."""
        from codebot.control_server import economics_summary

        mock_day = "2026-09-21"
        mock_total = 2_000_000_000  # 50% of cap

        with patch("codebot.control_server.STATE_DIR", MagicMock()), \
             patch("codebot.control_server.json") as mock_json_mod, \
             patch("codebot.control_server.Path") as mock_path:

            mock_ledger_path = MagicMock()
            mock_ledger_path.exists.return_value = False
            mock_path.return_value = mock_ledger_path

            mock_tracker = MagicMock()
            mock_tracker.build_summary.return_value = {
                "fleet_totals": {
                    "total_tokens": 1000000,
                    "prompt_tokens": 600000,
                    "completion_tokens": 400000,
                    "ticket_count": 5,
                },
                "tickets": {},
            }

            with patch("codebot.token_budget.current_day_utc", return_value=mock_day), \
                 patch("codebot.token_budget.day_total", return_value=mock_total), \
                 patch("codebot.token_budget.get_budget_state", return_value="ok"), \
                 patch("codebot.token_budget.CAP", 4_000_000_000), \
                 patch("codebot.cost_tracker.CostTracker", return_value=mock_tracker), \
                 patch("codebot.pricing_table.calculate_cost_usd", return_value=0.50):

                result = economics_summary()

        assert isinstance(result, dict)
        assert "version" in result
        assert "generated_at" in result
        assert "budget" in result
        assert "fleet" in result
        assert "by_model" in result
        assert "by_day" in result
        assert "alerts" in result
        assert result["version"] == 1
        assert isinstance(result["generated_at"], float)
        assert result["budget"]["cap"] == 4_000_000_000
        assert result["budget"]["used"] == 2_000_000_000
        assert result["budget"]["pct"] == 50.0
        assert result["budget"]["state"] == "ok"
        assert result["fleet"]["total_tokens"] == 1000000
        assert result["fleet"]["ticket_count"] == 5
