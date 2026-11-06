import sqlite3
import unittest
from unittest.mock import patch

from app.database import managed_fills


class ManagedFillFeeTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE orders (
                client_order_id TEXT, broker_order_id TEXT, signal_id TEXT, symbol TEXT,
                side TEXT, pool TEXT, qty REAL, status TEXT, parent_broker_order_id TEXT,
                signal_timestamp TEXT, reference_price REAL, submitted_at TEXT, filled_at TEXT,
                filled_qty REAL, filled_avg_price REAL, reference_timestamp TEXT,
                order_type TEXT, fees REAL, strategy_config TEXT
            );
            CREATE TABLE broker_activities (
                activity_type TEXT, order_id TEXT, symbol TEXT, side TEXT,
                qty REAL, price REAL, activity_at TEXT, fees REAL
            );
            CREATE TABLE broker_order_types (broker_order_id TEXT, order_type TEXT);
            CREATE TABLE broker_order_metadata (
                broker_order_id TEXT, client_order_id TEXT, parent_broker_order_id TEXT,
                order_type TEXT
            );
        """)
        self.db.execute("""
            INSERT INTO orders(client_order_id, broker_order_id, signal_id, symbol, side,
                               pool, qty, status, filled_qty, filled_avg_price, fees)
            VALUES ('entry', 'broker-1', 'signal', 'AAPL', 'buy', 'intraday', 10,
                    'filled', 10, 100, NULL)
        """)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def add_activity(self, qty, fee, symbol="AAPL", side="buy", order="broker-1",
                     activity_type="FILL"):
        self.db.execute("""
            INSERT INTO broker_activities VALUES (?, ?, ?, ?, ?, 100, '2026-11-06', ?)
        """, (activity_type, order, symbol, side, qty, fee))

    def fills(self):
        with patch("app.database.connect", return_value=self.db):
            return [dict(row) for row in managed_fills()]

    def test_complete_partial_fills_supply_fee_without_duplicate_order(self):
        self.add_activity(4, 0.04)
        self.add_activity(6, 0.06)
        fills = self.fills()
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0]["fees"], 0.10)
        self.assertEqual(fills[0]["fill_source"], "managed_order_ledger")

    def test_incomplete_quantity_or_missing_activity_fee_remains_unknown(self):
        self.add_activity(4, 0.04)
        self.assertIsNone(self.fills()[0]["fees"])
        self.add_activity(6, None)
        self.assertIsNone(self.fills()[0]["fees"])

    def test_recorded_order_fee_takes_precedence(self):
        self.db.execute("UPDATE orders SET fees = 0.3")
        self.add_activity(10, 0.5)
        self.assertEqual(self.fills()[0]["fees"], 0.3)

    def test_partial_recorded_order_fee_does_not_cover_larger_activity_fill(self):
        self.db.execute("UPDATE orders SET filled_qty = 4, filled_avg_price = NULL, fees = 0.04")
        self.add_activity(10, 0.10)
        fills = self.fills()
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["fill_source"], "broker_activity")
        self.assertAlmostEqual(fills[0]["fees"], 0.10)
        self.db.execute("UPDATE broker_activities SET fees = NULL")
        self.assertIsNone(self.fills()[0]["fees"])

    def test_unrelated_activity_and_account_fee_are_not_applied(self):
        self.add_activity(10, 1, symbol="MSFT")
        self.add_activity(10, 1, side="sell")
        self.add_activity(10, 1, activity_type="FEE")
        fills = [row for row in self.fills() if row["client_order_id"] == "entry"]
        self.assertEqual(len(fills), 1)
        self.assertIsNone(fills[0]["fees"])


if __name__ == "__main__":
    unittest.main()
