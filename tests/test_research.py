import unittest

from app.research import evaluate_walk_forward, positions_for


class WalkForwardResearchTests(unittest.TestCase):
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
        result = evaluate_walk_forward(
            closes, "intraday", cost_bps=5, train_fraction=0.7,
            min_spread_pct=0.001, min_buy_rsi=55,
        )
        without_costs = evaluate_walk_forward(
            closes, "intraday", cost_bps=0, train_fraction=0.7,
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


if __name__ == "__main__":
    unittest.main()
