import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.research import evaluate_ohlc_walk_forward, positions_for, simulate_ohlc


class WalkForwardResearchTests(unittest.TestCase):
    @staticmethod
    def bars(closes):
        return [{"open": close, "high": close, "low": close, "close": close}
                for close in closes]

    def test_intraday_walk_forward_applies_live_buy_filter(self):
        closes = [100.0] * 20 + [100.02, 100.0, 100.0, 100.0, 100.0]
        plain = positions_for(closes, "intraday")
        filtered = positions_for(closes, "intraday", min_spread_pct=0.001)

        self.assertTrue(plain[20])
        self.assertFalse(filtered[20])

    def test_filtered_entry_can_confirm_on_a_later_bar(self):
        closes = [100.0] * 20 + [100.02, 100.01, 100.0, 101.0, 102.0, 103.0]
        filtered = positions_for(closes, "intraday", min_spread_pct=0.001,
                                 min_buy_rsi=55)

        self.assertFalse(filtered[20])
        self.assertTrue(filtered[-1])

    def test_walk_forward_reports_costs_filter_and_chronological_split(self):
        closes = [
            100 + (index % 80 if index % 80 < 40 else 80 - index % 80) * 0.1
            for index in range(250)
        ]
        bars = self.bars(closes)
        result = evaluate_ohlc_walk_forward(
            bars, "intraday", cost_bps=5, train_fraction=0.7,
            min_spread_pct=0.001, min_buy_rsi=55,
        )
        without_costs = evaluate_ohlc_walk_forward(
            bars, "intraday", cost_bps=0, train_fraction=0.7,
            min_spread_pct=0.001, min_buy_rsi=55,
        )

        self.assertEqual(result["cost_bps_per_side"], 5)
        self.assertEqual(result["train_fraction"], 0.7)
        self.assertEqual(
            result["buy_confirmation_filter"],
            {"min_spread_pct": 0.001, "min_rsi_14": 55},
        )
        self.assertGreater(result["out_of_sample"]["observations"], 0)
        self.assertLess(
            result["out_of_sample"]["total_return"],
            without_costs["out_of_sample"]["total_return"],
        )

    def test_ohlc_walk_forward_reports_execution_assumptions_and_risk_config(self):
        closes = [100.0] * 20 + [104.0, 104.0] + [100.0] * 30
        bars = self.bars(closes)
        start = datetime(2026, 11, 3, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        for index, bar in enumerate(bars):
            bar["ts"] = start + timedelta(minutes=index)
        bars[21].update({"open": 104, "high": 110, "low": 95})
        result = evaluate_ohlc_walk_forward(
            bars, "intraday", cost_bps=5, train_fraction=0.7,
            stop_loss_pct=0.02, take_profit_pct=0.04,
            intraday_flatten_minutes=10, intraday_entry_cutoff_minutes=30,
        )

        self.assertEqual(result["execution_model"]["signal_fill"], "next bar open")
        self.assertEqual(result["execution_model"]["same_bar_stop_and_target"], "stop first")
        self.assertEqual(result["risk_and_exit_config"]["stop_loss_pct"], 0.02)
        self.assertEqual(result["signal_periods"], {"fast": 5, "slow": 20})
        self.assertGreater(result["in_sample"]["observations"], 0)
        self.assertGreater(result["entry_count"], 0)

    def test_ohlc_simulator_uses_custom_sma_periods(self):
        closes = [100.0] * 8 + [110.0] * 12 + [110.0, 110.0]
        result = simulate_ohlc(
            self.bars(closes), "intraday", cost_bps=0,
            min_spread_pct=0, min_buy_rsi=0, fast_period=2, slow_period=10,
        )

        self.assertEqual(result["entry_count"], 1)

    def test_intraday_cutoff_and_flatten_are_simulated_from_bar_times(self):
        closes = [100.0] * 19 + [104.0] + [105.0 + index * 0.05 for index in range(21)]
        start = datetime(2026, 11, 3, 15, 10, tzinfo=ZoneInfo("America/New_York"))
        bars = self.bars(closes)
        for index, bar in enumerate(bars):
            bar["ts"] = start + timedelta(minutes=index)
        cutoff_result = simulate_ohlc(
            bars[:21], "intraday", cost_bps=0, intraday_entry_cutoff_minutes=30,
        )
        flatten_result = simulate_ohlc(
            bars, "intraday", cost_bps=0, intraday_flatten_minutes=10,
        )

        self.assertEqual(cutoff_result["entry_count"], 0)
        self.assertEqual(
            flatten_result["trades"][-1]["exit_reason"], "end_of_day_flatten",
        )


if __name__ == "__main__":
    unittest.main()
