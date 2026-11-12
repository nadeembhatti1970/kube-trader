import unittest
from datetime import datetime, timedelta, timezone

from app.trade_accounting import (
    allocate_account_fees,
    build_realized_trade_report,
    classify_exit_reason,
    execution_latency,
    pool_session_realized,
)


def fill(**overrides):
    base = {
        "symbol": "AAPL",
        "pool": "intraday",
        "side": "buy",
        "client_order_id": "kt-entry",
        "broker_order_id": "broker-entry",
        "signal_id": "signal-entry",
        "filled_qty": 10,
        "filled_avg_price": 100.0,
        "filled_at": datetime(2026, 11, 6, 15, tzinfo=timezone.utc),
        "order_type": "market",
        "attribution_source": "managed_order_ledger",
        "fill_source": "managed_order_ledger",
    }
    base.update(overrides)
    return base


class ExitReasonTests(unittest.TestCase):
    def test_classifies_bot_and_broker_exits(self):
        self.assertEqual(classify_exit_reason(
            {"signal_id": "eod-flatten", "order_type": "market"}), "eod_flatten")
        self.assertEqual(classify_exit_reason(
            {"signal_id": "s-1", "order_type": "stop"}), "stop")
        self.assertEqual(classify_exit_reason(
            {"signal_id": "s-1", "order_type": "stop",
             "client_order_id": "kt-protect-abc"}), "stop_repair")
        self.assertEqual(classify_exit_reason(
            {"signal_id": "s-1", "order_type": "limit"}), "take_profit")
        self.assertEqual(classify_exit_reason(
            {"signal_id": "s-1", "order_type": "market"}), "signal")
        self.assertEqual(classify_exit_reason(
            {"signal_id": None, "order_type": "market"}), "unknown")


class FeeEstimateTests(unittest.TestCase):
    def test_allocates_account_fees_pro_rata_by_exit_notional(self):
        trades = [
            {"quantity": 10, "exit_fill_price": 100.0,
             "exit_filled_at": datetime(2026, 11, 6, 15, tzinfo=timezone.utc),
             "gross_realized_pnl": 20.0},
            {"quantity": 30, "exit_fill_price": 100.0,
             "exit_filled_at": datetime(2026, 11, 6, 18, tzinfo=timezone.utc),
             "gross_realized_pnl": -40.0},
        ]
        estimate = allocate_account_fees(
            trades, [{"activity_date": "2026-11-06", "amount": -4.0}])

        self.assertTrue(estimate["available"])
        self.assertAlmostEqual(trades[0]["estimated_fees"], 1.0)
        self.assertAlmostEqual(trades[1]["estimated_fees"], 3.0)
        self.assertAlmostEqual(trades[0]["estimated_net_realized_pnl"], 19.0)
        self.assertAlmostEqual(trades[1]["estimated_net_realized_pnl"], -43.0)
        self.assertAlmostEqual(estimate["allocated_amount"], 4.0)
        self.assertEqual(estimate["days_missing_fee_activity"], [])

    def test_missing_fee_day_leaves_the_estimate_unknown(self):
        trades = [
            {"quantity": 5, "exit_fill_price": 50.0,
             "exit_filled_at": datetime(2026, 11, 5, 15, tzinfo=timezone.utc),
             "gross_realized_pnl": 5.0},
        ]
        estimate = allocate_account_fees(
            trades, [{"activity_date": "2026-11-06", "amount": -1.0}])

        self.assertFalse(estimate["available"])
        self.assertIsNone(trades[0]["estimated_fees"])
        self.assertIsNone(trades[0]["estimated_net_realized_pnl"])
        self.assertEqual(estimate["days_missing_fee_activity"], ["2026-11-05"])
        self.assertAlmostEqual(estimate["unallocated_amount"], 1.0)

    def test_strict_per_order_fees_are_not_overwritten(self):
        report = build_realized_trade_report(
            [fill(fees=0.25), fill(
                side="sell", client_order_id="kt-exit", broker_order_id="broker-exit",
                signal_id="signal-exit", filled_avg_price=101.0, fees=0.25,
                filled_at=datetime(2026, 11, 6, 16, tzinfo=timezone.utc))],
            account_fees=[{"activity_date": "2026-11-06", "amount": -2.0}],
        )

        trade = report["trades"][0]
        self.assertAlmostEqual(trade["fees"], 0.5)
        self.assertAlmostEqual(trade["net_realized_pnl"], 9.5)
        self.assertAlmostEqual(trade["estimated_fees"], 2.0)
        self.assertAlmostEqual(trade["estimated_net_realized_pnl"], 8.0)
        self.assertTrue(report["fees_complete"])
        self.assertTrue(report["profitability_evidence"]["estimated_net_available"])


class ReportEnrichmentTests(unittest.TestCase):
    def test_report_exposes_exit_reason_latency_and_coverage(self):
        start = datetime(2026, 11, 6, 15, tzinfo=timezone.utc)
        report = build_realized_trade_report(
            [
                fill(signal_timestamp=start - timedelta(seconds=40)),
                fill(
                    side="sell", client_order_id="kt-exit",
                    broker_order_id="broker-exit", signal_id="signal-exit",
                    filled_avg_price=101.0, order_type="stop",
                    signal_timestamp=start + timedelta(seconds=60),
                    filled_at=start + timedelta(minutes=5),
                ),
            ],
            latency_warn_seconds=30,
            bar_coverage={"all_sessions_meet_threshold": False},
        )

        self.assertEqual(report["trades"][0]["exit_reason"], "stop")
        self.assertEqual(
            report["summary_by_exit_reason"]["stop"]["closed_trade_slices"], 1)
        self.assertEqual(report["execution_latency"]["entry"]["count"], 1)
        self.assertAlmostEqual(report["execution_latency"]["entry"]["p50_seconds"], 40.0)
        self.assertEqual(report["execution_latency"]["entry"]["over_warn_pct"], 100.0)
        self.assertFalse(report["bar_coverage"]["all_sessions_meet_threshold"])


class PoolSessionRealizedTests(unittest.TestCase):
    def test_counts_only_the_pool_and_session(self):
        ny = timezone(timedelta(hours=-5))
        trades = [
            {"pool": "intraday", "gross_realized_pnl": -100.0,
             "exit_filled_at": datetime(2026, 11, 6, 15, tzinfo=timezone.utc)},
            {"pool": "intraday", "gross_realized_pnl": 30.0,
             "exit_filled_at": datetime(2026, 11, 5, 15, tzinfo=timezone.utc)},
            {"pool": "core", "gross_realized_pnl": -500.0,
             "exit_filled_at": datetime(2026, 11, 6, 15, tzinfo=timezone.utc)},
        ]
        total = pool_session_realized(
            trades, "intraday", datetime(2026, 11, 6).date(), ny)

        self.assertAlmostEqual(total, -100.0)


class LatencyTests(unittest.TestCase):
    def test_empty_latency_has_no_percentiles(self):
        summary = execution_latency([{"entry_signal_to_fill_seconds": None}])
        self.assertEqual(summary["entry"]["count"], 0)
        self.assertIsNone(summary["entry"]["p90_seconds"])

    def test_latency_reports_the_bar_close_basis(self):
        summary = execution_latency([{"entry_signal_to_fill_seconds": 3.0}])
        self.assertIn("signal_timestamp", summary["basis"])
        self.assertAlmostEqual(summary["entry"]["p50_seconds"], 3.0)


if __name__ == "__main__":
    unittest.main()
