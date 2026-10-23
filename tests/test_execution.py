from types import SimpleNamespace
import unittest
from unittest.mock import patch

from alpaca.trading.enums import OrderSide

from app.services import execution


class FakeTradingClient:
    def __init__(self, open_orders):
        self.open_orders = open_orders
        self.cancelled_order_ids = []
        self.closed_symbols = []

    def get_order_by_id(self, order_id):
        child = SimpleNamespace(
            id="exit-order",
            symbol="AAPL",
            side=OrderSide.SELL,
            status="new",
        )
        return SimpleNamespace(id=order_id, legs=[child])

    def cancel_order_by_id(self, order_id):
        self.cancelled_order_ids.append(order_id)

    def get_orders(self, _request):
        return self.open_orders

    def close_position(self, symbol):
        self.closed_symbols.append(symbol)
        return SimpleNamespace(
            id="close-order",
            client_order_id="close-client-order",
            status="accepted",
            qty="3",
            submitted_at=None,
            filled_at=None,
            filled_qty=None,
            filled_avg_price=None,
        )


class PendingCancelTradingClient(FakeTradingClient):
    def __init__(self, open_orders):
        super().__init__(open_orders)
        self.order_responses = [open_orders, []]

    def cancel_order_by_id(self, order_id):
        self.cancelled_order_ids.append(order_id)
        raise execution.APIError('{"code":42210000,"message":"order pending cancel"}')

    def get_orders(self, _request):
        return self.order_responses.pop(0)


class CloseManagedPositionTests(unittest.TestCase):
    def setUp(self):
        self.position = SimpleNamespace(symbol="AAPL", qty="3")
        self.signal_event = {"ts": "2026-10-23T14:00:00+00:00", "price": 100}
        patcher = patch("app.services.execution.sync_managed_orders")
        self.sync_managed_orders = patcher.start()
        self.addCleanup(patcher.stop)

    @patch("app.services.execution.managed_order_id", return_value="entry-order")
    def test_cancels_attached_exit_before_closing_when_no_closing_orders_remain(self, _):
        client = FakeTradingClient(open_orders=[])

        handled = execution.close_managed_position(
            client, "AAPL", "intraday", [self.position], "signal-1", self.signal_event,
        )

        self.assertTrue(handled)
        self.assertEqual(client.cancelled_order_ids, ["exit-order"])
        self.assertEqual(client.closed_symbols, ["AAPL"])

    @patch("app.services.execution.managed_order_id", return_value="entry-order")
    @patch("app.services.execution.time.sleep")
    @patch("app.services.execution.time.monotonic", side_effect=[0, 0])
    def test_waits_for_already_pending_cancel_before_closing(self, _, __, ___):
        open_exit = SimpleNamespace(
            id="exit-order",
            symbol="AAPL",
            side=OrderSide.SELL,
            status="new",
        )
        client = PendingCancelTradingClient([open_exit])

        handled = execution.close_managed_position(
            client, "AAPL", "intraday", [self.position], "signal-1", self.signal_event,
        )

        self.assertTrue(handled)
        self.assertEqual(client.cancelled_order_ids, ["exit-order"])
        self.assertEqual(client.closed_symbols, ["AAPL"])

    @patch("app.services.execution.managed_order_id", return_value="entry-order")
    @patch("app.services.execution.time.sleep")
    @patch("app.services.execution.time.monotonic", side_effect=[0, 0, 6])
    def test_defers_close_while_open_exit_order_still_reserves_position(
        self, _, __, ___
    ):
        open_exit = SimpleNamespace(
            id="exit-order",
            symbol="AAPL",
            side=OrderSide.SELL,
            status="new",
        )
        client = FakeTradingClient(open_orders=[open_exit])

        handled = execution.close_managed_position(
            client, "AAPL", "intraday", [self.position], "signal-1", self.signal_event,
        )

        self.assertFalse(handled)
        self.assertEqual(client.cancelled_order_ids, ["exit-order"])
        self.assertEqual(client.closed_symbols, [])

    @patch("app.services.execution.managed_order_id", return_value=None)
    @patch("app.services.execution.time.sleep")
    @patch("app.services.execution.time.monotonic", side_effect=[0, 0, 6])
    def test_does_not_cancel_or_close_while_an_unrelated_closing_order_is_open(
        self, _, __, ___
    ):
        unrelated_exit = SimpleNamespace(
            id="manual-order",
            symbol="AAPL",
            side=OrderSide.SELL,
            status="new",
        )
        client = FakeTradingClient(open_orders=[unrelated_exit])

        handled = execution.close_managed_position(
            client, "AAPL", "intraday", [self.position], "signal-1", self.signal_event,
        )

        self.assertFalse(handled)
        self.assertEqual(client.cancelled_order_ids, [])
        self.assertEqual(client.closed_symbols, [])

    @patch("app.services.execution.managed_order_id", return_value="entry-order")
    def test_propagates_unrelated_cancellation_errors(self, _):
        class FailingTradingClient(FakeTradingClient):
            def cancel_order_by_id(self, order_id):
                raise execution.APIError('{"code":50000000,"message":"broker unavailable"}')

        client = FailingTradingClient(open_orders=[])

        with self.assertRaises(execution.APIError):
            execution.close_managed_position(
                client, "AAPL", "intraday", [self.position], "signal-1", self.signal_event,
            )

        self.assertEqual(client.closed_symbols, [])


class RefreshTrackedOrderStatusesTests(unittest.TestCase):
    def setUp(self):
        self.root = {
            "client_order_id": "client-1",
            "broker_order_id": "broker-1",
            "signal_id": "signal-1",
            "symbol": "AAPL",
            "pool": "intraday",
            "status": "accepted",
            "signal_timestamp": None,
            "reference_price": 190.0,
        }
        self.existing = {
            **self.root,
            "side": "buy",
            "filled_qty": 3,
            "filled_avg_price": 190.25,
        }
        self.broker_order = SimpleNamespace(
            id="broker-1",
            client_order_id="client-1",
            symbol="AAPL",
            side=OrderSide.BUY,
            qty="3",
            status="filled",
            submitted_at=None,
            filled_at=None,
            filled_qty="3",
            filled_avg_price="190.25",
            legs=[],
        )

    @patch("app.services.execution.sync_managed_orders")
    @patch("app.services.execution.managed_orders")
    @patch("app.services.execution.managed_order_roots")
    def test_logs_and_persists_changed_broker_status(
        self, roots, existing_orders, sync_order
    ):
        roots.return_value = [self.root]
        existing_orders.return_value = [self.existing]

        class StatusClient:
            def get_orders(self, _request):
                return [self.broker_order]

        client = StatusClient()
        client.broker_order = self.broker_order
        with self.assertLogs("execution", level="INFO") as captured:
            execution.refresh_tracked_order_statuses(client)

        sync_order.assert_called_once()
        record = sync_order.call_args.args[0][0]
        self.assertEqual(record["status"], "filled")
        self.assertEqual(record["filled_avg_price"], 190.25)
        self.assertIn("signal_id=signal-1", captured.output[0])
        self.assertIn("broker_order_id=broker-1", captured.output[0])
        self.assertIn("status=filled", captured.output[0])
        self.assertIn("filled_avg_price=190.25", captured.output[0])

    @patch("app.services.execution.sync_managed_orders")
    @patch("app.services.execution.managed_orders")
    @patch("app.services.execution.managed_order_roots")
    def test_resolves_submission_with_unknown_outcome_by_client_order_id(
        self, roots, existing_orders, sync_order
    ):
        root = {**self.root, "broker_order_id": None, "status": "submission_unknown"}
        existing = {**self.existing, "broker_order_id": None,
                    "status": "submission_unknown"}
        roots.return_value = [root]
        existing_orders.return_value = [existing]
        rejected_order = SimpleNamespace(
            **{**vars(self.broker_order), "status": "rejected", "filled_qty": None,
               "filled_avg_price": None}
        )

        class StatusClient:
            def get_orders(self, _request):
                return [rejected_order]

        client = StatusClient()
        with self.assertLogs("execution", level="INFO") as captured:
            execution.refresh_tracked_order_statuses(client)

        sync_order.assert_called_once()
        record = sync_order.call_args.args[0][0]
        self.assertEqual(record["status"], "rejected")
        self.assertEqual(record["broker_order_id"], "broker-1")
        self.assertIn("status=rejected", captured.output[0])

    @patch("app.services.execution.sync_managed_orders")
    @patch("app.services.execution.managed_orders")
    @patch("app.services.execution.managed_order_roots")
    def test_does_not_log_unchanged_broker_status(self, roots, existing_orders, sync_order):
        roots.return_value = [self.root]
        existing_orders.return_value = [self.existing]

        class StatusClient:
            def get_orders(inner_self, _request):
                return [SimpleNamespace(**{**vars(self.broker_order), "status": "accepted"})]

        with self.assertNoLogs("execution", level="INFO"):
            execution.refresh_tracked_order_statuses(StatusClient())

        sync_order.assert_called_once()

    @patch("app.services.execution.sync_managed_orders")
    @patch("app.services.execution.managed_orders")
    @patch("app.services.execution.managed_order_roots")
    def test_logs_and_persists_partial_fill_progress(self, roots, existing_orders, sync_orders):
        roots.return_value = [self.root]
        existing_orders.return_value = [{
            **self.existing,
            "status": "partially_filled",
            "filled_qty": 1,
            "filled_avg_price": 190,
        }]
        partially_filled = SimpleNamespace(
            **{
                **vars(self.broker_order),
                "status": "partially_filled",
                "filled_qty": "2",
                "filled_avg_price": "190.25",
            }
        )

        class StatusClient:
            def get_orders(self, _request):
                return [partially_filled]

        with self.assertLogs("execution", level="INFO") as captured:
            execution.refresh_tracked_order_statuses(StatusClient())

        record = sync_orders.call_args.args[0][0]
        self.assertEqual(record["filled_qty"], 2)
        self.assertIn("filled_qty=2", captured.output[0])

    @patch("app.services.execution.sync_managed_orders")
    @patch("app.services.execution.managed_orders", return_value=[])
    @patch("app.services.execution.managed_order_roots")
    def test_syncs_nested_child_orders_and_open_orders(self, roots, _existing, sync_orders):
        open_root_metadata = {
            **self.root,
            "client_order_id": "open-client",
            "broker_order_id": "open-broker",
            "signal_id": "open-signal",
        }
        roots.return_value = [self.root, open_root_metadata]
        child = SimpleNamespace(
            id="child-order",
            client_order_id="child-client",
            symbol="AAPL",
            side=OrderSide.SELL,
            qty="3",
            status="filled",
            submitted_at=None,
            filled_at=None,
            filled_qty="3",
            filled_avg_price="195",
            legs=[],
        )
        root = SimpleNamespace(**{**vars(self.broker_order), "legs": [child]})
        open_root = SimpleNamespace(
            id="open-broker",
            client_order_id="open-client",
            symbol="AAPL",
            side=OrderSide.BUY,
            qty="3",
            status="accepted",
            submitted_at=None,
            filled_at=None,
            filled_qty=None,
            filled_avg_price=None,
            legs=[],
        )

        class StatusClient:
            def get_orders(self, _request):
                return [root]

        execution.refresh_tracked_order_statuses(StatusClient(), open_orders=[open_root])

        records = sync_orders.call_args.args[0]
        self.assertEqual({record["broker_order_id"] for record in records},
                         {"broker-1", "child-order", "open-broker"})
        child_record = next(record for record in records
                            if record["broker_order_id"] == "child-order")
        self.assertEqual(child_record["parent_broker_order_id"], "broker-1")
        self.assertEqual(child_record["side"], "sell")


if __name__ == "__main__":
    unittest.main()
