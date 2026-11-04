import unittest

from app.research import simulate_ohlc
from app.services.backtesting import evaluate


class EvaluateTests(unittest.TestCase):
    def bars(self, closes, opens=None, highs=None, lows=None):
        return [
            {
                "open": (opens or closes)[index],
                "high": (highs or closes)[index],
                "low": (lows or closes)[index],
                "close": close,
            }
            for index, close in enumerate(closes)
        ]

    def test_next_bar_open_entry_and_conservative_same_bar_protective_exit(self):
        closes = [100.0] * 20 + [104.0, 104.0]
        opens = [100.0] * 20 + [100.0, 104.0]
        highs = [100.0] * 20 + [104.0, 110.0]
        lows = [100.0] * 20 + [100.0, 95.0]

        result = simulate_ohlc(
            self.bars(closes, opens, highs, lows), "intraday", cost_bps=5,
        )

        self.assertEqual(result["entry_count"], 1)
        self.assertEqual(len(result["trades"]), 1)
        trade = result["trades"][0]
        self.assertEqual(trade["entry_price"], 104)
        self.assertEqual(trade["exit_reason"], "stop")
        self.assertAlmostEqual(trade["exit_price"], 104 * 0.98)
        self.assertLess(result["bar_returns"][-1], 0)

    def test_buy_filter_rejects_marginal_crossover(self):
        closes = [100.0] * 20 + [100.02, 100.0]
        result = simulate_ohlc(
            self.bars(closes), "intraday", cost_bps=0,
            min_spread_pct=0.001, min_buy_rsi=55,
        )

        self.assertEqual(result["entry_count"], 0)

    def test_costs_reduce_rolling_backtest_return(self):
        closes = [100.0] * 20 + [104.0] + [104.0] * 5
        bars = self.bars(closes)
        with_costs = evaluate(bars, 5, 20, cost_bps=5)
        without_costs = evaluate(bars, 5, 20, cost_bps=0)

        self.assertIsNotNone(with_costs)
        self.assertLess(with_costs["total_return"], without_costs["total_return"])

    def test_too_little_history_returns_none(self):
        self.assertIsNone(evaluate(self.bars([100.0] * 10), 5, 20))


if __name__ == "__main__":
    unittest.main()
