import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.alpaca_reporting import (
    fetch_account_activities,
    serialize_account,
    serialize_order,
    serialize_position,
)
from app import main


class AlpacaReportingTests(unittest.TestCase):
    def test_serializes_alpaca_models_to_json_values(self):
        created_at = datetime(2025, 1, 2, tzinfo=timezone.utc)
        account = SimpleNamespace(
            status=SimpleNamespace(value="ACTIVE"),
            cash=Decimal("125.50"),
            created_at=created_at,
        )
        position = SimpleNamespace(symbol="AAPL", qty=Decimal("2"))
        order = SimpleNamespace(
            id="order-1", parent_order_id="parent-1", legs=[],
        )

        self.assertEqual(
            serialize_account(account),
            {
                "status": "ACTIVE",
                "cash": 125.5,
                "created_at": created_at.isoformat(),
            },
        )
        self.assertEqual(serialize_position(position), {"symbol": "AAPL", "qty": 2.0})
        self.assertEqual(
            serialize_order(order),
            {"id": "order-1", "parent_order_id": "parent-1", "legs": []},
        )

    def test_fetches_all_activity_pages_using_last_activity_id(self):
        client = Mock()
        first_response = Mock()
        first_response.json.return_value = [{"id": "a1"}, {"id": "a2"}]
        second_response = Mock()
        second_response.json.return_value = [{"id": "a3"}]
        client.get.side_effect = [first_response, second_response]

        after = datetime(2025, 1, 1, tzinfo=timezone.utc)
        records = fetch_account_activities(client, "FILL", after, page_size=2)

        self.assertEqual([record["id"] for record in records], ["a1", "a2", "a3"])
        self.assertEqual(client.get.call_count, 2)
        first_params = client.get.call_args_list[0].kwargs["params"]
        next_params = client.get.call_args_list[1].kwargs["params"]
        self.assertNotIn("page_token", first_params)
        self.assertEqual(next_params["page_token"], "a2")
        self.assertEqual(first_params["after"], after.isoformat())

    def test_rejects_repeated_activity_page_token(self):
        client = Mock()
        response = Mock()
        response.json.return_value = [{"id": "a1"}, {"id": "a2"}]
        client.get.return_value = response

        with self.assertRaisesRegex(ValueError, "pagination did not advance"):
            fetch_account_activities(
                client, "FILL", datetime(2025, 1, 1, tzinfo=timezone.utc),
                page_size=2,
            )

        self.assertEqual(client.get.call_count, 2)

    def test_rejects_full_activity_page_without_an_id(self):
        client = Mock()
        response = Mock()
        response.json.return_value = [{"id": "a1"}, {}]
        client.get.return_value = response

        with self.assertRaisesRegex(ValueError, "pagination did not advance"):
            fetch_account_activities(
                client, "FILL", datetime(2025, 1, 1, tzinfo=timezone.utc),
                page_size=2,
            )

    def test_broker_report_includes_account_data_and_truncation_flag(self):
        snapshot = {
            "captured_at": "2025-01-01T00:00:00+00:00",
            "trading_mode": "paper",
            "alpaca_account": {"status": "ACTIVE"},
            "equity": 1000.0,
            "buying_power": 500.0,
            "positions": [{"symbol": "AAPL"}],
            "open_orders": [],
            "orders": [{"id": str(index)} for index in range(500)],
            "reconciliation": {},
        }
        with (
            patch("app.main.latest_account_snapshot", return_value=snapshot),
            patch("app.main.broker_activity_summary", return_value=[]),
            patch("app.main.broker_activity_sync_status", return_value=[]),
            patch("app.main.latest_broker_activities", return_value=[]),
        ):
            report = main.broker_account_report()

        self.assertTrue(report["orders_may_be_truncated"])
        self.assertEqual(report["account"], {"status": "ACTIVE"})
        self.assertEqual(report["activity_ledger"]["total_stored"], 0)


if __name__ == "__main__":
    unittest.main()
