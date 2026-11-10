import unittest
from datetime import date, timedelta

from app.portfolio_daily_sim import simulate_daily_portfolio


def daily(closes, start=date(2026, 1, 5)):
    bars = []
    moment = start
    for close in closes:
        bars.append({
            "time": moment.isoformat(),
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
        })
        moment += timedelta(days=1)
    return bars


BASE = {"swing_sma": 20, "core_fast": 50, "core_slow": 200,
        "cost_bps_per_side": 0.0}


class DailySimulationTests(unittest.TestCase):
    def test_swing_pool_trades_a_close_sma_cross(self):
        closes = ([100.0] * 30 + [100.0 + index for index in range(21)]
                  + [120.0 - 0.5 * index for index in range(1, 25)])
        result = simulate_daily_portfolio({"AAPL": daily(closes)}, BASE)

        self.assertEqual(result["status"], "ok")
        self.assertGreater(len(result["trades"]), 0)
        trade = result["trades"][0]
        self.assertEqual(trade["pool"], "swing")
        self.assertIn(trade["exit_reason"], {"signal", "stop"})

    def test_core_pool_trades_a_golden_cross(self):
        closes = ([100.0] * 30 + [100.0 + index for index in range(30)]
                  + [130.0 - index for index in range(1, 30)])
        config = {**BASE, "core_fast": 5, "core_slow": 20,
                  "swing_allocation_pct": 0.0}
        result = simulate_daily_portfolio({"AAPL": daily(closes)}, config)

        self.assertGreater(len(result["trades"]), 0)
        self.assertEqual(result["trades"][0]["pool"], "core")

    def test_symbol_exposure_respects_the_cap(self):
        closes = ([100.0] * 30 + [100.0 + index for index in range(21)]
                  + [120.0 - 0.5 * index for index in range(1, 25)])
        result = simulate_daily_portfolio({"AAPL": daily(closes)}, BASE)

        self.assertGreater(len(result["trades"]), 0)
        trade = result["trades"][0]
        self.assertLessEqual(
            trade["entry_price"] * trade["quantity"], 100_000.0 * 0.05 + 1e-6)

    def test_split_partitions_trades_and_equity(self):
        closes = ([100.0] * 30 + [100.0 + index for index in range(21)]
                  + [120.0 - 0.5 * index for index in range(1, 25)])
        result = simulate_daily_portfolio({"AAPL": daily(closes)}, BASE)

        in_sample = result["in_sample"]
        out_of_sample = result["out_of_sample"]
        self.assertEqual(in_sample["trades"] + out_of_sample["trades"],
                         result["metrics"]["closed_trades"])
        self.assertAlmostEqual(in_sample["equity"]["final"],
                               out_of_sample["equity"]["starting"])
        self.assertIn("significance", out_of_sample)
        self.assertIn("benchmark", out_of_sample)

    def test_no_data_returns_a_stable_shape(self):
        result = simulate_daily_portfolio({}, BASE)
        self.assertEqual(result["status"], "no_data")
        self.assertIn("significance", result)

    def test_invalid_split_fraction_is_rejected(self):
        with self.assertRaises(ValueError):
            simulate_daily_portfolio({"AAPL": daily([100.0] * 30)},
                                     {**BASE, "split_fraction": 0.2})


if __name__ == "__main__":
    unittest.main()
