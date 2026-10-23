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
