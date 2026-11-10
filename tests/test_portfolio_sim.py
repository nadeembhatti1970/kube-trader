import unittest
from datetime import datetime, timedelta, timezone

from app.portfolio_sim import simulate_portfolio
from app.validation import (
    block_bootstrap_ci,
    minimum_detectable_edge,
    significance_summary,
    validation_config_key,
    validation_verdict,
)

# 14:30 UTC is 09:30 in New York on 6 November 2026.
START = datetime(2026, 11, 6, 14, 30, tzinfo=timezone.utc)


def bars_from(closes, start=START, spread=0.001):
    bars = []
    for index in range(len(closes)):
        close = closes[index]
        bars.append({
            "ts": start + timedelta(minutes=index),
            "open": close,
            "high": close * (1 + spread),
            "low": close * (1 - spread),
            "close": close,
        })
    return bars


def breakout_path():
    return ([100.0] * 30 + [100.0 + index for index in range(1, 11)]
            + [110.0] * 10 + [110.0 - index for index in range(1, 21)] + [90.0] * 10)


BASE = {
    "starting_equity": 100_000.0,
    "intraday_entry_start_minutes": 0,
    "intraday_entry_cutoff_minutes": 0,
    "intraday_flatten_minutes": 0,
    "cost_bps_per_side": 0.0,
}


class PortfolioSimulationTests(unittest.TestCase):
    def test_single_symbol_take_profit_and_portfolio_metrics(self):
        result = simulate_portfolio({"AAPL": bars_from(breakout_path())}, BASE)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["metrics"]["closed_trades"], 1)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "take_profit")
        self.assertAlmostEqual(trade["gross_return_pct"], 4.0, places=6)
        self.assertLessEqual(trade["entry_price"] * trade["quantity"], 5_000.0 + 1e-6)
        for key in ("total_return_pct", "max_drawdown_pct", "sharpe_annualized",
                    "average_exposure_pct", "turnover_ratio", "cost_drag_pct"):
            self.assertIn(key, result["metrics"])
        self.assertGreater(result["metrics"]["average_exposure_pct"], 0)

    def test_stop_exit_fills_at_the_protective_price(self):
        closes = [100.0] * 30 + [100.0 + index for index in range(1, 11)]
        bars = bars_from(closes)
        bars.append({"ts": bars[-1]["ts"] + timedelta(minutes=1), "open": 108.0,
                     "high": 108.0, "low": 90.0, "close": 95.0})
        result = simulate_portfolio({"AAPL": bars}, {**BASE, "take_profit_pct": 0.20})

        self.assertEqual(len(result["trades"]), 1)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "stop")
        self.assertAlmostEqual(trade["exit_price"], round(trade["entry_price"] * 0.98, 2))
        self.assertLess(trade["gross_return_pct"], 0)

    def test_pool_budget_caps_total_exposure(self):
        config = {**BASE, "max_symbol_exposure_pct": 0.25, "max_sector_exposure_pct": 1.0}
        symbols = {symbol: bars_from(breakout_path()) for symbol in ("AAPL", "MSFT", "NVDA")}
        result = simulate_portfolio(symbols, config)

        self.assertGreater(len(result["trades"]), 0)
        self.assertLessEqual(result["metrics"]["average_exposure_pct"], 20.0 + 1e-6)

    def test_end_of_day_flatten_closes_open_intraday_positions(self):
        closes = ([100.0] * 30 + [100.0 + index for index in range(1, 11)]
                  + [110.0 + 0.05 * index for index in range(60)])
        result = simulate_portfolio(
            {"AAPL": bars_from(closes, start=datetime(2026, 11, 6, 19, 30,
                                                      tzinfo=timezone.utc))},
            {**BASE, "intraday_flatten_minutes": 10,
             "take_profit_pct": 0.50, "stop_loss_pct": 0.20},
        )

        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual(result["trades"][0]["exit_reason"], "eod_flatten")

    def test_entry_cutoff_prevents_late_entries(self):
        # 18:30 UTC is 13:30 ET: the crossover lands inside the last 30 minutes.
        closes = [100.0] * 120 + [100.0 + index for index in range(1, 11)] + [110.0] * 20
        result = simulate_portfolio(
            {"AAPL": bars_from(closes, start=datetime(2026, 11, 6, 18, 30,
                                                      tzinfo=timezone.utc))},
            {**BASE, "intraday_entry_cutoff_minutes": 30},
        )

        self.assertEqual(result["trades"], [])

    def test_no_data_returns_a_stable_shape(self):
        result = simulate_portfolio({}, BASE)
        self.assertEqual(result["status"], "no_data")
        self.assertEqual(result["trades"], [])
        self.assertIn("significance", result)

    def test_protective_only_style_holds_through_a_reverse_crossover(self):
        closes = ([100.0] * 30 + [100.0 + index for index in range(1, 11)]
                  + [110.0] * 360)
        config = {**BASE, "take_profit_pct": 0.50, "stop_loss_pct": 0.20,
                  "intraday_flatten_minutes": 10}

        with_signal = simulate_portfolio({"AAPL": bars_from(closes)}, config)
        protective = simulate_portfolio(
            {"AAPL": bars_from(closes)}, {**config, "exit_style": "protective_only"})

        self.assertEqual(with_signal["trades"][0]["exit_reason"], "signal")
        self.assertEqual(len(protective["trades"]), 1)
        self.assertEqual(protective["trades"][0]["exit_reason"], "eod_flatten")

    def test_trailing_style_ratchets_the_stop(self):
        closes = [100.0] * 30 + [100.0 + index for index in range(1, 11)]
        bars = bars_from(closes)
        bars.append({"ts": bars[-1]["ts"] + timedelta(minutes=1), "open": 108.0,
                     "high": 108.0, "low": 100.0, "close": 100.0})
        result = simulate_portfolio(
            {"AAPL": bars},
            {**BASE, "exit_style": "trailing", "trailing_stop_pct": 0.02,
             "take_profit_pct": 0.50, "stop_loss_pct": 0.20},
        )

        self.assertEqual(len(result["trades"]), 1)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "trailing_stop")
        self.assertAlmostEqual(trade["exit_price"], round(110.0 * 1.001 * 0.98, 2), places=2)

    def test_resampling_reduces_bars(self):
        result = simulate_portfolio(
            {"AAPL": bars_from(breakout_path())}, {**BASE, "resample_minutes": 5})
        expected = -(-len(breakout_path()) // 5)
        self.assertEqual(result["period"]["bars"], expected)

    def test_split_reports_in_sample_and_out_of_sample_sections(self):
        result = simulate_portfolio({"AAPL": bars_from(breakout_path())}, BASE)

        self.assertEqual(result["split_fraction"], 0.7)
        in_sample = result["in_sample"]
        out_of_sample = result["out_of_sample"]
        self.assertEqual(in_sample["trades"] + out_of_sample["trades"],
                         result["metrics"]["closed_trades"])
        self.assertAlmostEqual(in_sample["equity"]["final"],
                               out_of_sample["equity"]["starting"])
        self.assertIn("significance", in_sample)
        self.assertIn("significance", out_of_sample)
        self.assertIn("benchmark", in_sample)
        self.assertIn("benchmark", out_of_sample)
        self.assertEqual(in_sample["trades"], 1)
        self.assertEqual(out_of_sample["trades"], 0)

    def test_split_fraction_outside_the_supported_range_is_rejected(self):
        for value in (0.4, 0.95):
            with self.assertRaises(ValueError):
                simulate_portfolio({"AAPL": bars_from(breakout_path())},
                                   {**BASE, "split_fraction": value})

    def test_opening_range_breakout_enters_after_the_range(self):
        window = [100.5] * 15
        after = [101.5 + 0.01 * index for index in range(366)]
        result = simulate_portfolio(
            {"AAPL": bars_from(window + after)},
            {"starting_equity": 100_000.0, "intraday_entry_start_minutes": 0, "intraday_entry_cutoff_minutes": 0,
             "intraday_flatten_minutes": 10, "take_profit_pct": 0.5,
             "stop_loss_pct": 0.2, "cost_bps_per_side": 0.0,
             "premise": "opening_range_breakout", "opening_range_minutes": 15},
        )

        self.assertEqual(len(result["trades"]), 1)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "eod_flatten")
        entry_hour = datetime.fromisoformat(trade["entry_ts"]).astimezone(
            timezone(timedelta(hours=-5)))
        self.assertGreaterEqual(entry_hour.hour * 60 + entry_hour.minute, 9 * 60 + 45)

    def test_gap_up_enters_on_the_opening_gap(self):
        day_one = bars_from([100.0] * 20, start=datetime(
            2026, 11, 6, 14, 30, tzinfo=timezone.utc))
        day_two = bars_from([101.5 + 0.01 * index for index in range(381)],
                            start=datetime(2026, 11, 9, 14, 30, tzinfo=timezone.utc))
        day_two[0]["open"] = 101.0  # 1% above the prior close
        result = simulate_portfolio(
            {"AAPL": day_one + day_two},
            {"starting_equity": 100_000.0, "intraday_entry_start_minutes": 0, "intraday_entry_cutoff_minutes": 0,
             "intraday_flatten_minutes": 10, "take_profit_pct": 0.5,
             "stop_loss_pct": 0.2, "cost_bps_per_side": 0.0,
             "premise": "gap_up", "gap_up_pct": 0.005},
        )

        self.assertEqual(len(result["trades"]), 1)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "eod_flatten")
        self.assertTrue(trade["entry_ts"].startswith("2026-11-09"))

    def test_gap_premise_ignores_the_post_open_guard(self):
        day_one = bars_from([100.0] * 20, start=datetime(
            2026, 11, 6, 14, 30, tzinfo=timezone.utc))
        day_two = bars_from([101.5 + 0.01 * index for index in range(381)],
                            start=datetime(2026, 11, 9, 14, 30, tzinfo=timezone.utc))
        day_two[0]["open"] = 101.0
        result = simulate_portfolio(
            {"AAPL": day_one + day_two},
            {"starting_equity": 100_000.0, "intraday_entry_start_minutes": 5, "intraday_entry_cutoff_minutes": 0,
             "intraday_flatten_minutes": 10, "take_profit_pct": 0.5,
             "stop_loss_pct": 0.2, "cost_bps_per_side": 0.0,
             "premise": "gap_up", "gap_up_pct": 0.005},
        )

        self.assertEqual(len(result["trades"]), 1)
        self.assertTrue(result["trades"][0]["entry_ts"].startswith("2026-11-09"))

    def test_starting_equity_is_required(self):
        with self.assertRaises(ValueError):
            simulate_portfolio({"AAPL": bars_from(breakout_path())},
                               {"cost_bps_per_side": 0.0})

    def test_invalid_premise_is_rejected(self):
        with self.assertRaises(ValueError):
            simulate_portfolio({"AAPL": bars_from(breakout_path())},
                               {**BASE, "premise": "magic"})

    def test_invalid_exit_style_is_rejected(self):
        with self.assertRaises(ValueError):
            simulate_portfolio({"AAPL": bars_from(breakout_path())},
                               {**BASE, "exit_style": "magic"})

    def test_trailing_requires_a_trail_distance(self):
        with self.assertRaises(ValueError):
            simulate_portfolio({"AAPL": bars_from(breakout_path())},
                               {**BASE, "exit_style": "trailing"})

    def test_reports_benchmark_relative_excess_for_full_and_oos(self):
        result = simulate_portfolio({"AAPL": bars_from(breakout_path())}, BASE)

        self.assertIn("ci_above_zero", result["benchmark_relative"])
        self.assertGreater(result["benchmark_relative"]["periods"], 0)
        self.assertIn("benchmark_relative", result["out_of_sample"])

    def test_benchmark_reports_exposure_matched_return(self):
        result = simulate_portfolio({"AAPL": bars_from(breakout_path())}, BASE)
        benchmark = result["benchmark"]
        exposure = result["metrics"]["average_exposure_pct"]
        self.assertAlmostEqual(
            benchmark["return_at_average_exposure_pct"],
            benchmark["return_pct"] * exposure / 100.0,
            places=9,
        )
        self.assertIsNotNone(benchmark["excess_return_at_exposure_pct"])


class SignificanceTests(unittest.TestCase):
    def test_zero_mean_sample_has_a_ci_covering_zero(self):
        values = [-1.0, 1.0] * 40
        summary = significance_summary(values)
        self.assertFalse(summary["ci_excludes_zero"])
        self.assertAlmostEqual(summary["mean"], 0.0)

    def test_strong_positive_sample_excludes_zero(self):
        values = [1.0, 1.2, 0.8, 1.1, 0.9] * 20
        summary = significance_summary(values)
        self.assertTrue(summary["ci_excludes_zero"])
        self.assertTrue(summary["ci_above_zero"])
        self.assertFalse(summary["ci_below_zero"])
        self.assertGreater(summary["ci_low"], 0)

    def test_significantly_negative_sample_is_not_validated(self):
        values = [-1.0, -1.2, -0.8, -1.1, -0.9] * 20
        summary = significance_summary(values)
        self.assertTrue(summary["ci_excludes_zero"])
        self.assertTrue(summary["ci_below_zero"])
        self.assertFalse(summary["ci_above_zero"])
        self.assertFalse(validation_verdict(summary, min_trades=10))

    def test_minimum_detectable_edge_shrinks_with_more_trades(self):
        small = minimum_detectable_edge([1.0, -1.0] * 10)
        large = minimum_detectable_edge([1.0, -1.0] * 100)
        self.assertGreater(small, large)

    def test_tiny_samples_do_not_report_a_zero_width_interval(self):
        summary = significance_summary([1.0, -1.0, 2.0, -2.0])
        self.assertLess(summary["ci_low"], summary["ci_high"])

    def test_bootstrap_is_deterministic_and_reports_its_basis(self):
        values = [0.5, -0.2, 0.8, 0.1, -0.4, 0.3, 0.6, -0.1]
        first = block_bootstrap_ci(values)
        second = block_bootstrap_ci(values)
        self.assertEqual(first, second)
        self.assertEqual(first["block_size"], 5)
        self.assertEqual(first["samples"], 2000)

    def test_validation_requires_trade_count_and_positive_ci(self):
        strong = significance_summary([1.0, 1.1, 0.9, 1.2, 0.95] * 10)
        self.assertTrue(validation_verdict(strong, min_trades=10))
        self.assertFalse(validation_verdict(strong, min_trades=1000))
        weak = significance_summary([1.0, -1.0] * 20)
        self.assertFalse(validation_verdict(weak, min_trades=10))

    def test_benchmark_relative_edge_is_required_when_supplied(self):
        strong = significance_summary([1.0, 1.1, 0.9, 1.2, 0.95] * 10)
        self.assertFalse(validation_verdict(
            strong, min_trades=10, benchmark_relative={"ci_above_zero": False}))
        self.assertTrue(validation_verdict(
            strong, min_trades=10, benchmark_relative={"ci_above_zero": True}))

    def test_config_key_ignores_unrelated_settings(self):
        base = {"strategy": "sma-crossover-confirmed-v1", "signal_fast_bars": 5,
                "signal_slow_bars": 20, "signal_min_spread_pct": 0.001}
        first = validation_config_key({**base, "app_version": "0.1.33"})
        second = validation_config_key({**base, "app_version": "0.2.0"})
        changed = validation_config_key({**base, "signal_fast_bars": 8})
        self.assertEqual(first["key"], second["key"])
        self.assertNotEqual(first["key"], changed["key"])

    def test_missing_simulator_keys_normalise_to_live_defaults(self):
        base = {"strategy": "sma-crossover-confirmed-v1", "signal_fast_bars": 5,
                "signal_slow_bars": 20}
        explicit = {**base, "exit_style": "signal", "min_hold_minutes": 0.0,
                    "exit_confirmation_spread_pct": 0.0, "trailing_stop_pct": 0.0,
                    "resample_minutes": 1}
        self.assertEqual(validation_config_key(base)["key"],
                         validation_config_key(explicit)["key"])

    def test_exit_variants_get_distinct_validation_keys(self):
        base = {"strategy": "sma-crossover-confirmed-v1", "signal_fast_bars": 5,
                "signal_slow_bars": 20, "exit_style": "signal"}
        variants = [
            validation_config_key({**base, "exit_style": "protective_only"})["key"],
            validation_config_key({**base, "exit_style": "trailing",
                                   "trailing_stop_pct": 0.01})["key"],
            validation_config_key({**base, "min_hold_minutes": 15})["key"],
            validation_config_key({**base, "resample_minutes": 5})["key"],
        ]
        self.assertEqual(len(set(variants + [validation_config_key(base)["key"]])), 5)


if __name__ == "__main__":
    unittest.main()
