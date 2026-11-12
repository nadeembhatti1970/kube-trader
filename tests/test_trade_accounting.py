import unittest
from datetime import datetime, timedelta, timezone

from app.trade_accounting import build_realized_trade_report


class RealizedTradeReportTests(unittest.TestCase):
    def test_cohort_counts_one_entry_order_for_multiple_exit_slices(self):
        start = datetime(2026, 11, 6, 15, tzinfo=timezone.utc)
        common = {"symbol": "AAPL", "pool": "intraday", "signal_id": "signal",
                  "attribution_source": "managed_order_ledger", "fees": 0.0}
        fills = [
            {**common, "client_order_id": "entry", "broker_order_id": "entry-id",
             "side": "buy", "filled_qty": 2, "filled_avg_price": 100, "filled_at": start,
             "strategy_config": {"app_version": "0.1.32", "strategy": "example"}},
            {**common, "client_order_id": "exit-1", "broker_order_id": "exit-id-1",
             "side": "sell", "filled_qty": 1, "filled_avg_price": 101,
             "filled_at": start + timedelta(minutes=1)},
            {**common, "client_order_id": "exit-2", "broker_order_id": "exit-id-2",
             "side": "sell", "filled_qty": 1, "filled_avg_price": 99,
             "filled_at": start + timedelta(minutes=2)},
        ]
        cohort = build_realized_trade_report(fills)["entry_month_version_cohorts"][0]
        self.assertEqual(cohort["closed_trade_slices"], 2)
        self.assertEqual(cohort["distinct_entry_order_ids"], 1)
        self.assertEqual(cohort["entry_month_utc"], "2026-11")
        self.assertEqual(cohort["entry_app_version"], "0.1.32")
        self.assertEqual(cohort["slices_with_complete_fees"], 2)
        self.assertEqual(cohort["net_realized_pnl"], 0)

    def test_matches_partial_exit_fifo_and_reports_gross_pnl(self):
        start = datetime(2026, 10, 23, 14, tzinfo=timezone.utc)
        fills = [
            {
                "client_order_id": "entry-1",
                "broker_order_id": "broker-entry-1",
                "signal_id": "signal-entry-1",
                "symbol": "AAPL",
                "side": "buy",
                "pool": "intraday",
                "filled_qty": 3,
                "filled_avg_price": 100,
                "reference_price": 99,
                "signal_timestamp": start - timedelta(seconds=5),
                "submitted_at": start,
                "filled_at": start + timedelta(seconds=2),
            },
            {
                "client_order_id": "entry-2",
                "broker_order_id": "broker-entry-2",
                "signal_id": "signal-entry-2",
                "symbol": "AAPL",
                "side": "buy",
                "pool": "intraday",
                "filled_qty": 2,
                "filled_avg_price": 102,
                "reference_price": 102,
                "signal_timestamp": start + timedelta(seconds=10),
                "submitted_at": start + timedelta(seconds=11),
                "filled_at": start + timedelta(seconds=12),
            },
            {
                "client_order_id": "exit-1",
                "broker_order_id": "broker-exit-1",
                "signal_id": "signal-exit-1",
                "symbol": "AAPL",
                "side": "sell",
                "pool": "intraday",
                "filled_qty": 4,
                "filled_avg_price": 110,
                "reference_price": 111,
                "signal_timestamp": start + timedelta(minutes=5),
                "submitted_at": start + timedelta(minutes=5, seconds=1),
                "filled_at": start + timedelta(minutes=5, seconds=2),
            },
        ]

        report = build_realized_trade_report(fills)

        self.assertEqual(len(report["trades"]), 2)
        self.assertEqual(
            [trade["quantity"] for trade in report["trades"]],
            [3.0, 1.0],
        )
        self.assertEqual(
            [trade["gross_realized_pnl"] for trade in report["trades"]],
            [30.0, 8.0],
        )
        self.assertAlmostEqual(
            report["trades"][0]["entry_adverse_slippage_bps"], 101.01010101,
        )
        self.assertAlmostEqual(report["trades"][0]["exit_adverse_slippage_bps"], 90.09009009)
        self.assertEqual(report["open_managed_quantities"], [{
            "symbol": "AAPL",
            "pool": "intraday",
            "open_quantity": 1.0,
        }])
        self.assertEqual(report["summary_by_pool"]["intraday"]["gross_realized_pnl"], 38.0)
        self.assertEqual(report["summary_by_pool"]["intraday"]["win_rate_pct"], 100.0)
        cohort = report["entry_month_version_cohorts"][0]
        self.assertEqual(cohort["entry_month_utc"], "2026-10")
        self.assertEqual(cohort["closed_trade_slices"], 2)
        self.assertEqual(cohort["distinct_entry_order_ids"], 2)
        self.assertEqual(cohort["slices_with_complete_fees"], 0)
        self.assertIsNone(cohort["net_realized_pnl"])
        self.assertEqual(
            report["summary_by_entry_version"]["unknown"]["closed_trade_slices"], 2,
        )
        self.assertEqual(report["data_quality"]["slices_with_entry_strategy_config"], 0)
        self.assertFalse(report["fees_complete"])
        self.assertIsNone(report["summary_by_pool"]["intraday"]["net_realized_pnl"])
        self.assertEqual(report["profitability_evidence"]["status"], "incomplete")
        self.assertFalse(report["profitability_evidence"]["net_pnl_measurable"])
        self.assertIn(
            "complete_order_linked_fees",
            report["profitability_evidence"]["unmet_requirements"],
        )
        self.assertAlmostEqual(
            report["summary_by_symbol"]["AAPL"]["average_holding_seconds"],
            (300.0 + 290.0) / 2,
        )

    def test_reports_net_pnl_fees_order_types_and_grouped_metrics(self):
        start = datetime(2026, 10, 23, 14, tzinfo=timezone.utc)
        fills = [
            {
                "client_order_id": "entry",
                "broker_order_id": "broker-entry",
                "signal_id": "entry-signal",
                "symbol": "AAPL",
                "side": "buy",
                "pool": "swing",
                "filled_qty": 2,
                "filled_avg_price": 100,
                "reference_price": 99,
                "reference_timestamp": start,
                "signal_timestamp": start,
                "submitted_at": start,
                "filled_at": start + timedelta(seconds=1),
                "order_type": "market",
                "fees": 0.10,
                "strategy_config": {
                    "strategy": "swing-sma-v1",
                    "app_version": "0.1.24",
                    "signal_min_spread_pct": 0.001,
                    "signal_min_buy_rsi": 55,
                    "signal_values": {"fast_sma": 101, "slow_sma": 100, "rsi_14": 61},
                },
                "attribution_source": "managed_order_ledger",
                "fill_source": "managed_order_ledger",
            },
            {
                "client_order_id": "exit",
                "broker_order_id": "broker-exit",
                "signal_id": "exit-signal",
                "symbol": "AAPL",
                "side": "sell",
                "pool": "swing",
                "filled_qty": 2,
                "filled_avg_price": 110,
                "reference_price": 111,
                "reference_timestamp": start + timedelta(minutes=1),
                "signal_timestamp": start + timedelta(minutes=1),
                "submitted_at": start + timedelta(minutes=1),
                "filled_at": start + timedelta(minutes=1, seconds=2),
                "order_type": "stop",
                "fees": 0.20,
                "strategy_config": {
                    "strategy": "swing-sma-v1",
                    "app_version": "0.1.24",
                    "signal_min_spread_pct": 0.001,
                    "signal_min_buy_rsi": 55,
                },
                "attribution_source": "managed_parent_order",
                "fill_source": "broker_activity",
            },
        ]

        report = build_realized_trade_report(fills)
        trade = report["trades"][0]

        self.assertEqual(trade["entry_order_type"], "market")
        self.assertEqual(trade["exit_order_type"], "stop")
        self.assertEqual(trade["entry_attribution_source"], "managed_order_ledger")
        self.assertEqual(trade["exit_attribution_source"], "managed_parent_order")
        self.assertEqual(trade["entry_fill_source"], "managed_order_ledger")
        self.assertEqual(trade["exit_fill_source"], "broker_activity")
        self.assertEqual(trade["entry_strategy_config"]["app_version"], "0.1.24")
        self.assertEqual(
            trade["entry_strategy_config"]["signal_values"]["rsi_14"], 61,
        )
        self.assertEqual(trade["exit_strategy_config"]["app_version"], "0.1.24")
        self.assertEqual(trade["entry_reference_timestamp"], start)
        self.assertAlmostEqual(trade["entry_signal_to_fill_seconds"], 1)
        self.assertAlmostEqual(trade["fees"], 0.30)
        self.assertAlmostEqual(trade["net_realized_pnl"], 19.70)
        self.assertTrue(report["fees_complete"])
        cohort = report["entry_month_version_cohorts"][0]
        self.assertEqual(cohort["entry_app_version"], "0.1.24")
        self.assertEqual(cohort["distinct_entry_order_ids"], 1)
        self.assertEqual(cohort["slices_with_complete_fees"], 1)
        self.assertAlmostEqual(cohort["net_realized_pnl"], 19.70)
        self.assertEqual(report["profitability_evidence"]["status"], "measurable")
        self.assertTrue(report["profitability_evidence"]["net_pnl_measurable"])
        self.assertTrue(
            report["profitability_evidence"]["strategy_attribution_complete"],
        )
        self.assertEqual(
            report["fee_data"]["slices_with_complete_entry_and_exit_fees"], 1,
        )
        self.assertEqual(report["data_quality"]["slices_missing_entry_strategy_config"], 0)
        self.assertAlmostEqual(
            report["summary_by_strategy"]["swing-sma-v1"]["net_realized_pnl"], 19.70,
        )
        self.assertAlmostEqual(
            report["summary_by_strategy_symbol_order_type"]
            ["swing-sma-v1|AAPL|market|stop"]["gross_realized_pnl"],
            20,
        )
        self.assertAlmostEqual(
            report["summary_by_pool"]["swing"]["net_realized_pnl"], 19.70,
        )
        self.assertEqual(len(report["summary_by_entry_config"]), 1)
        self.assertEqual(report["summary_by_entry_version"]["0.1.24"]["closed_trade_slices"], 1)
        cohort = next(iter(report["summary_by_entry_config"].values()))
        self.assertEqual(cohort["gross_realized_pnl"], 20)
        fills[1]["attribution_source"] = None
        incomplete_attribution = build_realized_trade_report(fills)
        self.assertFalse(
            incomplete_attribution["profitability_evidence"]
            ["strategy_attribution_complete"],
        )

    def test_reports_sell_fills_without_a_managed_entry_as_unmatched(self):
        report = build_realized_trade_report([{
            "client_order_id": "external-exit",
            "broker_order_id": "broker-exit",
            "signal_id": "signal-exit",
            "symbol": "MSFT",
            "side": "sell",
            "pool": "core",
            "filled_qty": 2,
            "filled_avg_price": 400,
            "filled_at": datetime(2026, 10, 23, 14, tzinfo=timezone.utc),
        }])

        self.assertEqual(report["trades"], [])
        self.assertEqual(report["unmatched_exit_fills"][0]["unmatched_quantity"], 2.0)


class TradeAttributionTests(unittest.TestCase):
    def test_unattributed_exit_is_not_matched_to_a_prior_pool(self):
        start = datetime(2026, 10, 23, 14, tzinfo=timezone.utc)
        report = build_realized_trade_report([
            {
                "client_order_id": "managed-entry",
                "broker_order_id": "broker-entry",
                "signal_id": "entry-signal",
                "symbol": "AAPL",
                "side": "buy",
                "pool": "intraday",
                "filled_qty": 2,
                "filled_avg_price": 100,
                "filled_at": start,
                "attribution_source": "managed_order_ledger",
                "fill_source": "managed_order_ledger",
            },
            {
                "client_order_id": "broker-exit",
                "broker_order_id": "broker-exit",
                "signal_id": None,
                "symbol": "AAPL",
                "side": "sell",
                "pool": "unattributed",
                "filled_qty": 2,
                "filled_avg_price": 110,
                "filled_at": start + timedelta(minutes=1),
                "attribution_source": "unattributed",
                "fill_source": "broker_activity",
            },
        ])

        self.assertEqual(report["trades"], [])
        self.assertEqual(report["unmatched_exit_fills"][0]["pool"], "unattributed")
        self.assertEqual(
            report["unmatched_exit_fills"][0]["attribution_source"], "unattributed",
        )
        self.assertEqual(report["open_managed_quantities"][0]["pool"], "intraday")


class SignalBarLatencyTests(unittest.TestCase):
    def fills(self, bar_seconds):
        start = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
        config = {"app_version": "0.1.46", "strategy": "sma-crossover-confirmed-v1"}
        if bar_seconds is not None:
            config = {**config, "signal_bar_seconds": bar_seconds}
        common = {"symbol": "AAPL", "pool": "intraday", "fees": 0.0,
                  "attribution_source": "managed_order_ledger"}
        return [
            {**common, "client_order_id": "entry", "broker_order_id": "entry-id",
             "signal_id": "signal", "side": "buy", "filled_qty": 1,
             "filled_avg_price": 100, "strategy_config": config,
             "signal_timestamp": start,
             "filled_at": start + timedelta(seconds=60 + 12)},
            {**common, "client_order_id": "exit", "broker_order_id": "exit-id",
             "signal_id": "signal-exit", "side": "sell", "filled_qty": 1,
             "filled_avg_price": 101, "strategy_config": config,
             "signal_timestamp": start + timedelta(minutes=5),
             "filled_at": start + timedelta(minutes=5, seconds=70)},
        ]

    def test_latency_is_measured_from_the_bar_close(self):
        trade = build_realized_trade_report(self.fills(60))["trades"][0]

        self.assertAlmostEqual(trade["entry_signal_to_fill_seconds"], 12.0)
        self.assertEqual(trade["entry_signal_bar_seconds"], 60.0)

    def test_legacy_orders_without_a_bar_duration_keep_the_open_basis(self):
        trade = build_realized_trade_report(self.fills(None))["trades"][0]

        self.assertAlmostEqual(trade["entry_signal_to_fill_seconds"], 72.0)
        self.assertEqual(trade["entry_signal_bar_seconds"], 0.0)


if __name__ == "__main__":
    unittest.main()
