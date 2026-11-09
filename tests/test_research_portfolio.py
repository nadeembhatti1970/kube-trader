import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main


def rising_bars(count=90, start=datetime(2026, 11, 6, 14, 30, tzinfo=timezone.utc)):
    bars = []
    for index in range(count):
        close = 100.0 if index < 30 else 100.0 + (index - 29)
        bars.append({
            "ts": start + timedelta(minutes=index),
            "open": close,
            "high": close * 1.001,
            "low": close * 0.999,
            "close": close,
        })
    return bars


class PortfolioResearchEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_runs_the_portfolio_simulation_and_reports_validation(self):
        with patch.object(main, "latest_universe_snapshot",
                          return_value=[{"symbol": "AAPL", "selected": True}]), \
                patch.object(main, "recent_bars", return_value=rising_bars()), \
                patch.object(main, "latest_account_snapshot",
                             return_value={"equity": 100_000.0}):
            response = self.client.get("/research/portfolio", params={"days": 500})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["period"]["symbols"], 1)
        self.assertIn("significance", body)
        self.assertIn("config_hash", body["validation"])
        self.assertFalse(body["validation"]["validated"])
        self.assertFalse(body["persisted"])
        self.assertIn("metrics", body)

    def test_persist_records_the_validation_verdict(self):
        with patch.object(main, "latest_universe_snapshot",
                          return_value=[{"symbol": "AAPL", "selected": True}]), \
                patch.object(main, "recent_bars", return_value=rising_bars()), \
                patch.object(main, "latest_account_snapshot",
                             return_value={"equity": 100_000.0}), \
                patch.object(main, "save_strategy_validation") as save:
            response = self.client.get(
                "/research/portfolio", params={"days": 500, "persist": True})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["persisted"])
        save.assert_called_once()
        args = save.call_args.args
        self.assertEqual(args[1], "sma-crossover-confirmed-v1")
        self.assertFalse(args[7])

    def test_missing_symbols_returns_not_found(self):
        with patch.object(main, "latest_universe_snapshot", return_value=[]):
            response = self.client.get("/research/portfolio")

        self.assertEqual(response.status_code, 404)

    def test_candidate_overrides_are_simulated_without_touching_settings(self):
        baseline_spread = main.settings.signal_min_spread_pct
        with patch.object(main, "latest_universe_snapshot",
                          return_value=[{"symbol": "AAPL", "selected": True}]), \
                patch.object(main, "recent_bars", return_value=rising_bars()), \
                patch.object(main, "latest_account_snapshot",
                             return_value={"equity": 100_000.0}):
            response = self.client.get("/research/portfolio", params={
                "days": 500, "min_spread_pct": 0.02, "min_buy_rsi": 65,
                "fast_bars": 8, "slow_bars": 30,
            })

        body = response.json()
        self.assertEqual(body["config"]["min_spread_pct"], 0.02)
        self.assertEqual(body["config"]["min_buy_rsi"], 65)
        self.assertEqual(body["config"]["fast_bars"], 8)
        self.assertEqual(main.settings.signal_min_spread_pct, baseline_spread)


if __name__ == "__main__":
    unittest.main()
