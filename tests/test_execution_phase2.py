import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.services import execution


def message(message_id, symbol, action, ts):
    payload = {"symbol": symbol, "action": action, "ts": ts,
               "pool": "intraday", "price": 100.0}
    return message_id, {"data": __import__("json").dumps(payload)}


class DedupeSignalsTests(unittest.TestCase):
    def test_keeps_newest_event_per_symbol_and_orders_sells_first(self):
        messages = [
            message("1-0", "AAPL", "buy", "2026-11-06T15:00:00+00:00"),
            message("2-0", "AAPL", "sell", "2026-11-06T15:05:00+00:00"),
            message("3-0", "MSFT", "buy", "2026-11-06T15:01:00+00:00"),
        ]

        kept, dropped = execution.dedupe_signals(messages)

        self.assertEqual([item[1]["symbol"] for item in kept], ["AAPL", "MSFT"])
        self.assertEqual([item[1]["action"] for item in kept], ["sell", "buy"])
        self.assertEqual(dropped, ["1-0"])

    def test_unreadable_payload_is_dropped(self):
        kept, dropped = execution.dedupe_signals([("9-0", {"data": "not-json"})])
        self.assertEqual(kept, [])
        self.assertEqual(dropped, ["9-0"])


class RiskViewCacheTests(unittest.TestCase):
    def setUp(self):
        execution.invalidate_risk_view()

    def tearDown(self):
        execution.invalidate_risk_view()

    def test_reuses_one_reconciliation_inside_the_ttl(self):
        view = (object(), [], {}, [])
        with patch.object(execution, "reconcile", return_value=view) as reconcile, \
                patch.object(execution.settings, "risk_view_ttl_seconds", 30.0):
            first = execution.cached_risk_view(object())
            second = execution.cached_risk_view(object())

        self.assertIs(first, second)
        self.assertEqual(reconcile.call_count, 1)

    def test_invalidation_forces_a_fresh_reconciliation(self):
        view = (object(), [], {}, [])
        with patch.object(execution, "reconcile", return_value=view) as reconcile:
            execution.cached_risk_view(object())
            execution.invalidate_risk_view()
            execution.cached_risk_view(object())

        self.assertEqual(reconcile.call_count, 2)

    def test_zero_max_age_always_refreshes(self):
        view = (object(), [], {}, [])
        with patch.object(execution, "reconcile", return_value=view) as reconcile:
            execution.cached_risk_view(object())
            execution.cached_risk_view(object(), max_age_seconds=0.0)

        self.assertEqual(reconcile.call_count, 2)


class EntryRequestTests(unittest.TestCase):
    def test_market_entry_is_the_default(self):
        with patch.object(execution.settings, "entry_order_type", "market"):
            request, is_limit = execution.build_entry_request(
                "AAPL", 10, 100.0, 98.0, 104.0, "kt-1", "intraday")

        self.assertFalse(is_limit)
        self.assertEqual(request.type.value, "market")
        self.assertEqual(request.time_in_force.value, "day")

    def test_marketable_limit_offsets_the_reference_price(self):
        with patch.object(execution.settings, "entry_order_type", "marketable_limit"), \
                patch.object(execution.settings, "entry_limit_offset_bps", 10.0):
            request, is_limit = execution.build_entry_request(
                "AAPL", 10, 100.0, 98.0, 104.0, "kt-1", "intraday")

        self.assertTrue(is_limit)
        self.assertEqual(request.type.value, "limit")
        self.assertAlmostEqual(float(request.limit_price), 100.10)
        self.assertEqual(request.client_order_id, "kt-1-l")

    def test_swing_entries_stay_market_gtc_without_a_target(self):
        with patch.object(execution.settings, "entry_order_type", "marketable_limit"):
            request, is_limit = execution.build_entry_request(
                "AAPL", 10, 100.0, 94.0, None, "kt-1", "swing")

        self.assertFalse(is_limit)
        self.assertEqual(request.type.value, "market")
        self.assertEqual(request.time_in_force.value, "gtc")
        self.assertEqual(request.order_class.value, "oto")


class FakeLimitClient:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.cancelled = []
        self.submitted = []

    def get_order_by_id(self, order_id):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        filled = 4.0 if status == "partially_filled" else 0.0
        return SimpleNamespace(id=order_id, status=status, filled_qty=filled)

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(order_id)

    def submit_order(self, request):
        self.submitted.append(request)
        return SimpleNamespace(id="fallback", status="accepted", filled_qty=None)


class LimitFallbackTests(unittest.TestCase):
    def test_filled_limit_is_returned_without_a_fallback(self):
        client = FakeLimitClient(["new", "filled"])
        order = SimpleNamespace(id="limit-1", status="new", filled_qty=None)
        with patch("app.services.execution.time.sleep"), \
                patch("app.services.execution.time.monotonic", side_effect=[0, 0, 0, 99]):
            result = execution.await_fill_or_fallback(
                client, order, "AAPL", 10, 98.0, 104.0, "intraday", "kt-1")

        self.assertEqual(result.status, "filled")
        self.assertEqual(client.cancelled, [])
        self.assertEqual(client.submitted, [])

    def test_partial_fill_keeps_the_day_order_working(self):
        client = FakeLimitClient(["new", "partially_filled"])
        order = SimpleNamespace(id="limit-1", status="new", filled_qty=None)
        with patch("app.services.execution.time.sleep"), \
                patch("app.services.execution.time.monotonic", side_effect=[0, 0, 0, 99]):
            result = execution.await_fill_or_fallback(
                client, order, "AAPL", 10, 98.0, 104.0, "intraday", "kt-1")

        self.assertEqual(result.status, "partially_filled")
        self.assertEqual(client.submitted, [])

    def test_unfilled_limit_is_cancelled_then_marketed(self):
        client = FakeLimitClient(["canceled"])
        order = SimpleNamespace(id="limit-1", status="new", filled_qty=None)
        with patch.object(execution.settings, "limit_order_timeout_seconds", 0.0):
            result = execution.await_fill_or_fallback(
                client, order, "AAPL", 10, 98.0, 104.0, "intraday", "kt-1")

        self.assertEqual(client.cancelled, ["limit-1"])
        self.assertEqual(len(client.submitted), 1)
        self.assertEqual(client.submitted[0].type.value, "market")
        self.assertEqual(result.id, "fallback")

    def test_order_still_live_after_cancel_is_not_duplicated(self):
        client = FakeLimitClient(["new"])
        order = SimpleNamespace(id="limit-1", status="new", filled_qty=None)
        with patch.object(execution.settings, "limit_order_timeout_seconds", 0.0):
            result = execution.await_fill_or_fallback(
                client, order, "AAPL", 10, 98.0, 104.0, "intraday", "kt-1")

        self.assertEqual(client.cancelled, ["limit-1"])
        self.assertEqual(client.submitted, [])
        self.assertEqual(result.id, "limit-1")


class ProcessBatchTests(unittest.TestCase):
    def test_acknowledges_dropped_events_and_handles_the_rest(self):
        class FakeRedis:
            def __init__(self):
                self.acked = []

            def xack(self, _stream, _group, message_id):
                self.acked.append(message_id)

        redis_client = FakeRedis()
        messages = [
            message("1-0", "AAPL", "buy", "2026-11-06T15:00:00+00:00"),
            message("2-0", "AAPL", "sell", "2026-11-06T15:05:00+00:00"),
            message("3-0", "MSFT", "buy", "2026-11-06T15:01:00+00:00"),
        ]
        with patch.object(execution.settings, "execution_max_workers", 2), \
                patch.object(execution, "handle_signal", return_value=True) as handle:
            execution.process_batch(object(), redis_client, messages)

        self.assertEqual(sorted(redis_client.acked), ["1-0", "2-0", "3-0"])
        self.assertEqual(handle.call_count, 2)


if __name__ == "__main__":
    unittest.main()
