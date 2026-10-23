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

    @patch("app.services.execution.managed_order_id", return_value="entry-order")
    def test_cancels_attached_exit_before_closing_when_no_closing_orders_remain(self, _):
        client = FakeTradingClient(open_orders=[])

        handled = execution.close_managed_position(client, "AAPL", "intraday", [self.position])

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

        handled = execution.close_managed_position(client, "AAPL", "intraday", [self.position])

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

        handled = execution.close_managed_position(client, "AAPL", "intraday", [self.position])

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

        handled = execution.close_managed_position(client, "AAPL", "intraday", [self.position])

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
            execution.close_managed_position(client, "AAPL", "intraday", [self.position])

        self.assertEqual(client.closed_symbols, [])


class RefreshTrackedOrderStatusesTests(unittest.TestCase):
    @patch("app.services.execution.update_order")
    @patch("app.services.execution.tracked_open_orders")
    def test_logs_and_persists_changed_broker_status(self, tracked_orders, update_order):
        tracked_orders.return_value = [{
            "client_order_id": "client-1",
            "broker_order_id": "broker-1",
            "signal_id": "signal-1",
            "symbol": "AAPL",
            "pool": "intraday",
            "status": "accepted",
        }]

        class StatusClient:
            def get_order_by_id(self, order_id):
                self.order_id = order_id
                return SimpleNamespace(
                    id=order_id,
                    status="filled",
                    filled_qty="3",
                    filled_avg_price="190.25",
                )

        client = StatusClient()
        with self.assertLogs("execution", level="INFO") as captured:
            execution.refresh_tracked_order_statuses(client)

        update_order.assert_called_once_with("client-1", "broker-1", "filled")
        self.assertIn("signal_id=signal-1", captured.output[0])
        self.assertIn("broker_order_id=broker-1", captured.output[0])
        self.assertIn("status=filled", captured.output[0])
        self.assertIn("filled_avg_price=190.25", captured.output[0])

    @patch("app.services.execution.update_order")
    @patch("app.services.execution.tracked_open_orders")
    def test_resolves_submission_with_unknown_outcome_by_client_order_id(
        self, tracked_orders, update_order
    ):
        tracked_orders.return_value = [{
            "client_order_id": "client-1",
            "broker_order_id": None,
            "signal_id": "signal-1",
            "symbol": "AAPL",
            "pool": "intraday",
            "status": "submission_unknown",
        }]

        class StatusClient:
            def get_order_by_client_id(self, client_order_id):
                self.client_order_id = client_order_id
                return SimpleNamespace(id="broker-1", status="rejected")

        client = StatusClient()
        with self.assertLogs("execution", level="INFO") as captured:
            execution.refresh_tracked_order_statuses(client)

        update_order.assert_called_once_with("client-1", "broker-1", "rejected")
        self.assertIn("status=rejected", captured.output[0])
        self.assertEqual(client.client_order_id, "client-1")

    @patch("app.services.execution.update_order")
    @patch("app.services.execution.tracked_open_orders")
    def test_does_not_log_unchanged_broker_status(self, tracked_orders, update_order):
        tracked_orders.return_value = [{
            "client_order_id": "client-1",
            "broker_order_id": "broker-1",
            "signal_id": "signal-1",
            "symbol": "AAPL",
            "pool": "intraday",
            "status": "accepted",
        }]

        class StatusClient:
            def get_order_by_id(self, order_id):
                return SimpleNamespace(id=order_id, status="accepted")

        with self.assertNoLogs("execution", level="INFO"):
            execution.refresh_tracked_order_statuses(StatusClient())

        update_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
