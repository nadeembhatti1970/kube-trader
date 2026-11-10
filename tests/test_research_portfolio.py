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


def daily_rising_bars(count=260, start=datetime(2025, 1, 1, tzinfo=timezone.utc)):
    bars = []
    moment = start
    for index in range(count):
        close = 100.0 + index * 0.2
        bars.append({
            "time": moment.date().isoformat(),
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": 1000,
        })
        moment += timedelta(days=1)
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
        self.assertTrue(body["validation"]["benchmark_relative_required"])
        self.assertIn("benchmark_relative", body["out_of_sample"])
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


    def test_exit_style_and_resample_overrides_reach_the_simulation(self):
        with patch.object(main, "latest_universe_snapshot",
                          return_value=[{"symbol": "AAPL", "selected": True}]), \
                patch.object(main, "recent_bars", return_value=rising_bars()), \
                patch.object(main, "latest_account_snapshot",
                             return_value={"equity": 100_000.0}):
            response = self.client.get("/research/portfolio", params={
                "days": 500, "exit_style": "protective_only", "resample_minutes": 5,
            })

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["config"]["exit_style"], "protective_only")
        self.assertEqual(body["config"]["resample_minutes"], 5)

    def test_exit_variants_produce_distinct_validation_hashes(self):
        base = {"days": 500}
        with patch.object(main, "latest_universe_snapshot",
                          return_value=[{"symbol": "AAPL", "selected": True}]), \
                patch.object(main, "recent_bars", return_value=rising_bars()), \
                patch.object(main, "latest_account_snapshot",
                             return_value={"equity": 100_000.0}):
            signal = self.client.get("/research/portfolio", params=base).json()
            protective = self.client.get(
                "/research/portfolio",
                params={**base, "exit_style": "protective_only"}).json()

        self.assertNotEqual(
            signal["validation"]["config_hash"],
            protective["validation"]["config_hash"],
        )

    def test_split_fraction_is_reported(self):
        with patch.object(main, "latest_universe_snapshot",
                          return_value=[{"symbol": "AAPL", "selected": True}]), \
                patch.object(main, "recent_bars", return_value=rising_bars()), \
                patch.object(main, "latest_account_snapshot",
                             return_value={"equity": 100_000.0}):
            response = self.client.get(
                "/research/portfolio", params={"days": 500, "split_fraction": 0.6})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["split_fraction"], 0.6)
        self.assertIn("in_sample", body)
        self.assertIn("out_of_sample", body)

    def test_invalid_split_fraction_returns_bad_request(self):
        response = self.client.get(
            "/research/portfolio", params={"split_fraction": 0.95})
        self.assertEqual(response.status_code, 422)

    def test_premise_override_reaches_the_simulation(self):
        with patch.object(main, "latest_universe_snapshot",
                          return_value=[{"symbol": "AAPL", "selected": True}]), \
                patch.object(main, "recent_bars", return_value=rising_bars()), \
                patch.object(main, "latest_account_snapshot",
                             return_value={"equity": 100_000.0}):
            response = self.client.get(
                "/research/portfolio", params={"days": 500, "premise": "gap_up"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["config"]["premise"], "gap_up")

    def test_invalid_premise_returns_bad_request(self):
        response = self.client.get(
            "/research/portfolio", params={"premise": "magic"})
        self.assertEqual(response.status_code, 400)

    def test_daily_endpoint_runs_the_swing_and_core_pools(self):
        with patch.object(main, "latest_universe_snapshot",
                          return_value=[{"symbol": "AAPL", "selected": True}]), \
                patch.object(main, "recent_daily_bars", return_value=daily_rising_bars()), \
                patch.object(main, "latest_account_snapshot",
                             return_value={"equity": 100_000.0}):
            response = self.client.get(
                "/research/portfolio/daily", params={"days": 260})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("out_of_sample", body)
        self.assertIn("benchmark_relative", body["out_of_sample"])
        self.assertEqual(body["validation"]["namespace"], "daily-swing-core")
        self.assertTrue(body["validation"]["benchmark_relative_required"])
        self.assertFalse(body["validation"]["validated"])

    def test_portfolio_endpoint_requires_equity_when_no_snapshot(self):
        with patch.object(main, "latest_universe_snapshot",
                          return_value=[{"symbol": "AAPL", "selected": True}]), \
                patch.object(main, "recent_bars", return_value=rising_bars()), \
                patch.object(main, "latest_account_snapshot", return_value=None):
            response = self.client.get("/research/portfolio", params={"days": 500})

        self.assertEqual(response.status_code, 503)

    def test_explicit_starting_equity_works_without_a_snapshot(self):
        with patch.object(main, "latest_universe_snapshot",
                          return_value=[{"symbol": "AAPL", "selected": True}]), \
                patch.object(main, "recent_bars", return_value=rising_bars()), \
                patch.object(main, "latest_account_snapshot", return_value=None):
            response = self.client.get("/research/portfolio", params={
                "days": 500, "starting_equity": 50_000})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["config"]["starting_equity"], 50_000)

    def test_daily_endpoint_requires_equity_when_no_snapshot(self):
        with patch.object(main, "latest_universe_snapshot",
                          return_value=[{"symbol": "AAPL", "selected": True}]), \
                patch.object(main, "recent_daily_bars",
                             return_value=daily_rising_bars()), \
                patch.object(main, "latest_account_snapshot", return_value=None):
            response = self.client.get(
                "/research/portfolio/daily", params={"days": 260})

        self.assertEqual(response.status_code, 503)

    def test_daily_endpoint_requires_daily_bars(self):
        with patch.object(main, "latest_universe_snapshot",
                          return_value=[{"symbol": "AAPL", "selected": True}]), \
                patch.object(main, "recent_daily_bars", return_value=[]):
            response = self.client.get("/research/portfolio/daily")

        self.assertEqual(response.status_code, 404)

    def test_absolute_mandate_is_reported(self):
        with patch.object(main, "latest_universe_snapshot",
                          return_value=[{"symbol": "AAPL", "selected": True}]), \
                patch.object(main, "recent_bars", return_value=rising_bars()), \
                patch.object(main, "latest_account_snapshot",
                             return_value={"equity": 100_000.0}):
            response = self.client.get(
                "/research/portfolio", params={"days": 500, "mandate": "absolute"})

        self.assertEqual(response.status_code, 200)
        validation = response.json()["validation"]
        self.assertEqual(validation["mandate"], "absolute")
        self.assertFalse(validation["benchmark_relative_required"])

    def test_invalid_mandate_returns_bad_request(self):
        response = self.client.get(
            "/research/portfolio", params={"mandate": "magic"})
        self.assertEqual(response.status_code, 400)

    def test_invalid_exit_style_returns_bad_request(self):
        response = self.client.get(
            "/research/portfolio", params={"exit_style": "magic"})
        self.assertEqual(response.status_code, 400)

    def test_trailing_style_requires_a_trail_distance(self):
        response = self.client.get(
            "/research/portfolio", params={"exit_style": "trailing"})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
