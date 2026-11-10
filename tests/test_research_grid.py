import unittest
from unittest.mock import patch

from app.services import research_grid


def fake_result(oos_mean, n, ci_low, ci_high, pf=1.2,
                excess_above_zero=True):
    return {
        "out_of_sample": {
            "significance": {
                "n": n, "mean": oos_mean, "ci_low": ci_low, "ci_high": ci_high,
                "ci_above_zero": ci_low is not None and ci_low > 0,
            },
            "metrics": {"profit_factor": pf, "total_return_pct": 0.1,
                        "average_exposure_pct": 0.5},
            "benchmark_relative": {
                "mean_excess_pct": 0.05, "ci_low_pct": 0.01,
                "ci_high_pct": 0.1, "ci_above_zero": excess_above_zero,
            },
        },
        "in_sample": {"significance": {"n": 40}},
        "metrics": {"closed_trades": 50},
        "period": {"symbols": 1},
    }


BASE = {"starting_equity": 100_000.0, "cost_bps_per_side": 5.0,
        "split_fraction": 0.7}


class VariantTests(unittest.TestCase):
    def test_matrix_expands_every_dimension(self):
        variants = research_grid.build_variants(
            [0.001, 0.002], [55.0, 65.0], [(5, 20), (10, 40)], ["signal", "trailing"])
        self.assertEqual(len(variants), 2 * 2 * 2 * 2)
        self.assertEqual(variants[0]["fast_bars"], 5)
        self.assertEqual(variants[-1]["exit_style"], "trailing")

    def test_premises_expand_the_matrix(self):
        variants = research_grid.build_variants(
            [0.001], [55.0], [(5, 20)], ["signal"],
            ["sma_crossover", "opening_range_breakout", "gap_up"])
        self.assertEqual(len(variants), 3)
        self.assertEqual([item["premise"] for item in variants],
                         ["sma_crossover", "opening_range_breakout", "gap_up"])

    def test_label_carries_the_premise(self):
        label = research_grid.variant_label({
            "fast_bars": 5, "slow_bars": 20, "min_spread_pct": 0.001,
            "min_buy_rsi": 55.0, "exit_style": "signal",
            "premise": "opening_range_breakout",
        })
        self.assertTrue(label.startswith("opening_range_breakout|"))

    def test_label_carries_the_distinguishing_parameters(self):
        label = research_grid.variant_label({
            "fast_bars": 5, "slow_bars": 20, "min_spread_pct": 0.0015,
            "min_buy_rsi": 65.0, "exit_style": "protective_only",
        })
        self.assertIn("5/20", label)
        self.assertIn("s0.0015", label)
        self.assertIn("r65", label)
        self.assertIn("protective_only", label)


class RunGridTests(unittest.TestCase):
    def variants(self):
        return [
            {"min_spread_pct": 0.001, "min_buy_rsi": 55.0,
             "fast_bars": 5, "slow_bars": 20, "exit_style": "signal"},
            {"min_spread_pct": 0.002, "min_buy_rsi": 65.0,
             "fast_bars": 5, "slow_bars": 20, "exit_style": "signal"},
        ]

    def fake_by_spread(self, _bars, config, **_):
        if config["min_spread_pct"] == 0.001:
            return fake_result(0.20, 40, 0.05, 0.40)
        return fake_result(-0.10, 60, -0.30, 0.10)

    def test_rows_are_ranked_by_out_of_sample_gross(self):
        with patch.object(research_grid, "simulate_portfolio",
                          side_effect=self.fake_by_spread):
            rows = research_grid.run_grid(
                {"AAPL": []}, {}, BASE, self.variants(), min_oos_trades=30)

        self.assertEqual(rows[0]["variant"]["min_spread_pct"], 0.001)
        self.assertAlmostEqual(rows[0]["oos_gross_pct"], 0.30)
        self.assertAlmostEqual(rows[1]["oos_gross_pct"], 0.00)
        self.assertTrue(rows[0]["validated"])
        self.assertFalse(rows[1]["validated"])

    def test_minimum_oos_trade_count_blocks_promotion(self):
        with patch.object(research_grid, "simulate_portfolio",
                          return_value=fake_result(0.20, 10, 0.05, 0.40)):
            rows = research_grid.run_grid(
                {"AAPL": []}, {}, BASE, self.variants()[:1], min_oos_trades=30)

        self.assertFalse(rows[0]["validated"])

    def test_missing_benchmark_edge_blocks_promotion(self):
        with patch.object(research_grid, "simulate_portfolio",
                          return_value=fake_result(0.25, 60, 0.10, 0.40,
                                                   excess_above_zero=False)):
            rows = research_grid.run_grid(
                {"AAPL": []}, {}, BASE, self.variants()[:1], min_oos_trades=30)

        self.assertTrue(rows[0]["oos_ci_low"] > 0)
        self.assertFalse(rows[0]["validated"])

    def test_spread_that_spans_zero_is_not_promoted(self):
        with patch.object(research_grid, "simulate_portfolio",
                          return_value=fake_result(0.05, 100, -0.02, 0.12)):
            rows = research_grid.run_grid(
                {"AAPL": []}, {}, BASE, self.variants()[:1], min_oos_trades=30)

        self.assertFalse(rows[0]["validated"])

    def test_persist_records_the_out_of_sample_verdict(self):
        with patch.object(research_grid, "simulate_portfolio",
                          side_effect=self.fake_by_spread), \
                patch.object(research_grid, "save_strategy_validation") as save:
            rows = research_grid.run_grid(
                {"AAPL": []}, {}, BASE, self.variants()[:1],
                persist=True, min_oos_trades=30)

        save.assert_called_once()
        args = save.call_args.args
        self.assertEqual(args[3], 40)
        self.assertAlmostEqual(args[4], 0.20)
        self.assertTrue(args[7])
        self.assertIn("config_hash", rows[0])
        report = args[8]
        self.assertEqual(report["basis"], "out_of_sample")
        self.assertIn("in_sample", report)
        self.assertIn("out_of_sample", report)

    def test_without_persist_nothing_is_written(self):
        with patch.object(research_grid, "simulate_portfolio",
                          side_effect=self.fake_by_spread), \
                patch.object(research_grid, "save_strategy_validation") as save:
            research_grid.run_grid(
                {"AAPL": []}, {}, BASE, self.variants()[:1], min_oos_trades=30)

        save.assert_not_called()


DAILY_BASE = {
    "starting_equity": 100_000.0,
    "swing_allocation_pct": 0.30, "core_allocation_pct": 0.50,
    "swing_sma": 20, "swing_atr_period": 14, "swing_atr_multiple": 2.0,
    "core_fast": 50, "core_slow": 200, "core_stop_loss_pct": 0.20,
    "cost_bps_per_side": 5.0, "split_fraction": 0.7,
}


class DailyGridTests(unittest.TestCase):
    def test_variants_include_a_baseline_and_each_regime(self):
        variants = research_grid.build_daily_variants([50, 200])
        self.assertEqual([item["label"] for item in variants],
                         ["no-regime", "regime50", "regime200"])
        self.assertFalse(variants[0]["regime_filter_enabled"])
        self.assertTrue(variants[1]["regime_filter_enabled"])

    def test_daily_grid_ranks_and_persists_out_of_sample_verdicts(self):
        with patch.object(research_grid, "simulate_daily_portfolio",
                          return_value=fake_result(0.5, 40, 0.2, 0.9)), \
                patch.object(research_grid, "save_strategy_validation") as save:
            rows = research_grid.run_daily_grid(
                {"AAPL": []}, [], {}, DAILY_BASE,
                research_grid.build_daily_variants([200]),
                persist=True, min_oos_trades=30)

        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["validated"] for row in rows))
        self.assertEqual(save.call_count, 2)
        args = save.call_args.args
        self.assertEqual(args[1], "daily-swing-core-v1")
        self.assertAlmostEqual(args[4], 0.5)


if __name__ == "__main__":
    unittest.main()
