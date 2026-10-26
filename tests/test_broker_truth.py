import unittest
from datetime import datetime, timezone

from app.export_names import export_filename
from app.trade_accounting import reconcile_open_quantities


class ReconcileOpenQuantitiesTests(unittest.TestCase):
    def test_broker_is_truth(self):
        ledger = [
            {"symbol": "CRM", "pool": "intraday", "open_quantity": 60},
            {"symbol": "GONE", "pool": "intraday", "open_quantity": 5},
            {"symbol": "OK", "pool": "intraday", "open_quantity": 3},
        ]
        broker = [{"symbol": "CRM", "qty": "19"}, {"symbol": "JNJ", "qty": "4"},
                  {"symbol": "OK", "qty": "3"}]
        result = reconcile_open_quantities(ledger, broker)
        statuses = {d["symbol"]: d["status"] for d in result["discrepancies"]}
        self.assertEqual(statuses, {"CRM": "ledger_exceeds_broker",
                                    "GONE": "missing_at_broker",
                                    "JNJ": "untracked_at_broker"})
        quantities = {(o["symbol"], o["pool"]): o["open_quantity"]
                      for o in result["open_quantities"]}
        self.assertEqual(quantities, {("CRM", "intraday"): 19, ("JNJ", "unattributed"): 4,
                                      ("OK", "intraday"): 3})


class ExportFilenameTests(unittest.TestCase):
    NOW = datetime(2026, 10, 23, 21, 12, 42, tzinfo=timezone.utc)

    def test_names(self):
        self.assertEqual(export_filename("/account/reconciliation", self.NOW),
                         "reconciliation_20261023T211242Z.json")
        self.assertEqual(export_filename("/account/broker", self.NOW),
                         "broker_20261023T211242Z.json")
        self.assertEqual(export_filename("/trades", self.NOW), "trades_20261023T211242Z.json")
        self.assertEqual(export_filename("/signals/AAPL", self.NOW),
                         "signals_AAPL_20261023T211242Z.json")

    def test_excluded(self):
        self.assertIsNone(export_filename("/healthz", self.NOW))
        self.assertIsNone(export_filename("/", self.NOW))


class SnapshotSerializationTests(unittest.TestCase):
    def test_uuid_in_account_is_serialized(self):
        import uuid
        from unittest.mock import MagicMock, patch
        from app import database
        db = MagicMock()
        with patch.object(database, "connect") as connect:
            connect.return_value.__enter__.return_value = db
            database.save_account_snapshot(1.0, 1.0, [], {}, alpaca_account={"id": uuid.uuid4()})
        db.execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class SymbolEntryBlockTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        self.tz = ZoneInfo("America/New_York")
        self.now = datetime(2026, 10, 23, 12, 0, tzinfo=self.tz)
        self.trades = [
            {"symbol": "AMAT", "exit_filled_at": "2026-10-23T15:55:00+00:00",
             "gross_realized_pnl": -10.0},
            {"symbol": "AMAT", "exit_filled_at": "2026-10-23T14:00:00+00:00",
             "gross_realized_pnl": -5.0},
            {"symbol": "XOM", "exit_filled_at": "2026-10-23T15:55:00+00:00",
             "gross_realized_pnl": 20.0},
        ]

    def block(self, symbol, cooldown=0, limit=0.0):
        from app.trade_accounting import symbol_entry_block
        return symbol_entry_block(self.trades, symbol, self.now, self.now.date(),
                                  cooldown, limit, self.tz)

    def test_disabled_by_default(self):
        self.assertIsNone(self.block("AMAT"))

    def test_cooldown_after_recent_loss_only(self):
        self.assertIn("cooling down", self.block("AMAT", cooldown=900))
        self.assertIsNone(self.block("AMAT", cooldown=60))
        self.assertIsNone(self.block("XOM", cooldown=900))

    def test_accepts_datetime_exit_times(self):
        from datetime import datetime, timezone
        self.trades[0]["exit_filled_at"] = datetime(2026, 10, 23, 15, 55, tzinfo=timezone.utc)
        self.assertIn("cooling down", self.block("AMAT", cooldown=900))

    def test_daily_loss_limit(self):
        self.assertIn("loss limit", self.block("AMAT", limit=15))
        self.assertIsNone(self.block("AMAT", limit=16))
        self.assertIsNone(self.block("XOM", limit=1))
