import unittest
from datetime import datetime, timedelta, timezone

from app.trade_accounting import build_realized_trade_report


class RealizedTradeReportTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
