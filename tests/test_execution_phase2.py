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


class CoreIndexSleeveTests(unittest.TestCase):
    class Client:
        def __init__(self, is_open=True):
            self.is_open = is_open
            self.requests = []

        def get_clock(self):
            return SimpleNamespace(is_open=self.is_open)

        def submit_order(self, request):
            self.requests.append(request)
            return SimpleNamespace(
                id="core-1", status="accepted", submitted_at=None, filled_at=None,
                filled_qty=None, filled_avg_price=None)

    def run_sleeve(self, equity, cash, positions, is_open=True, **overrides):
        values = {
            "core_index_enabled": True,
            "core_index_pct": 0.30,
            "core_index_symbol": "SPY",
            "core_index_rebalance_band_pct": 0.05,
            "core_index_min_trade_notional": 500.0,
        }
        values.update(overrides)
        client = self.Client(is_open)
        with patch.multiple(execution.settings, **values), \
                patch.object(execution, "reserve_order", return_value=True), \
                patch.object(execution, "update_order"):
            trades = execution.rebalance_core_index(
                client, SimpleNamespace(equity=str(equity), cash=str(cash)),
                positions)
        return client, trades

    def test_buys_toward_the_target_with_a_notional_order(self):
        client, trades = self.run_sleeve(100_000, 100_000, [])

        self.assertEqual(len(client.requests), 1)
        request = client.requests[0]
        self.assertEqual(request.side.value, "buy")
        self.assertAlmostEqual(float(request.notional), 30_000.0)
        self.assertEqual(request.time_in_force.value, "day")
        self.assertEqual(trades[0]["side"], "buy")

    def test_no_trade_inside_the_rebalance_band(self):
        position = SimpleNamespace(
            symbol="SPY", market_value="29500", qty="10", current_price="295")
        client, trades = self.run_sleeve(100_000, 0, [position])

        self.assertEqual(client.requests, [])
        self.assertEqual(trades, [])

    def test_sells_the_excess_above_target(self):
        position = SimpleNamespace(
            symbol="SPY", market_value="34000", qty="10", current_price="400")
        client, trades = self.run_sleeve(100_000, 0, [position])

        self.assertEqual(len(client.requests), 1)
        request = client.requests[0]
        self.assertEqual(request.side.value, "sell")
        self.assertAlmostEqual(float(request.qty), 10.0)
        self.assertEqual(trades[0]["side"], "sell")

    def test_disabled_or_closed_market_does_nothing(self):
        client, trades = self.run_sleeve(
            100_000, 100_000, [], core_index_enabled=False)
        self.assertEqual(client.requests, [])
        self.assertEqual(trades, [])

        client, trades = self.run_sleeve(100_000, 100_000, [], is_open=False)
        self.assertEqual(client.requests, [])
        self.assertEqual(trades, [])


class CapitalUtilizationTests(unittest.TestCase):
    def test_reports_invested_cash_and_pool_use(self):
        positions = [SimpleNamespace(market_value="20000")]
        allocations = {
            "core": {"exposure": 20000, "target_equity": 50000, "remaining": 30000},
        }

        result = execution.capital_utilization(
            100_000.0, 60_000.0, positions, allocations)

        self.assertEqual(result["invested"], 20000.0)
        self.assertAlmostEqual(result["utilization_pct"], 20.0)
        self.assertAlmostEqual(result["cash_pct"], 60.0)
        self.assertAlmostEqual(result["pools"]["core"]["utilization_pct"], 40.0)

    def test_empty_portfolio_is_zero_utilization(self):
        result = execution.capital_utilization(100_000.0, 100_000.0, [], {})
        self.assertEqual(result["invested"], 0.0)
        self.assertAlmostEqual(result["utilization_pct"], 0.0)
        self.assertAlmostEqual(result["cash_pct"], 100.0)


class ReconcileWarningThrottleTests(unittest.TestCase):
    def setUp(self):
        execution.WARNING_LOG.clear()

    def tearDown(self):
        execution.WARNING_LOG.clear()

    def test_repeats_only_after_the_interval(self):
        with patch("app.services.execution.time.monotonic",
                   side_effect=[0, 30, 100]):
            with self.assertLogs("execution", level="WARNING"):
                execution.log_reconciliation_warnings(["issue"], repeat_seconds=60)
            with self.assertNoLogs("execution", level="WARNING"):
                execution.log_reconciliation_warnings(["issue"], repeat_seconds=60)
            with self.assertLogs("execution", level="WARNING"):
                execution.log_reconciliation_warnings(["issue"], repeat_seconds=60)

    def test_a_warning_that_clears_logs_again(self):
        with patch("app.services.execution.time.monotonic", side_effect=[0, 1, 2]):
            with self.assertLogs("execution", level="WARNING"):
                execution.log_reconciliation_warnings(["issue"], repeat_seconds=60)
            with self.assertNoLogs("execution", level="WARNING"):
                execution.log_reconciliation_warnings([], repeat_seconds=60)
            with self.assertLogs("execution", level="WARNING"):
                execution.log_reconciliation_warnings(["issue"], repeat_seconds=60)

    def test_zero_interval_logs_each_cycle(self):
        with patch("app.services.execution.time.monotonic", side_effect=[0, 1]):
            with self.assertLogs("execution", level="WARNING"):
                execution.log_reconciliation_warnings(["issue"], repeat_seconds=0)
            with self.assertLogs("execution", level="WARNING"):
                execution.log_reconciliation_warnings(["issue"], repeat_seconds=0)


class ProtectionModelTests(unittest.TestCase):
    def test_unmanaged_position_is_labelled(self):
        model = execution.protection_model(
            None, False, [{"time_in_force": "gtc"}], [])
        self.assertEqual(model["model"], "unmanaged")
        self.assertTrue(model["stop_present"])
        self.assertEqual(model["target_management"], "none")

    def test_fractional_intraday_reports_software_target(self):
        model = execution.protection_model(
            "intraday", True, [{"time_in_force": "day"}], [])
        self.assertEqual(model["model"],
                         "fractional_day_stop_plus_software_target")
        self.assertEqual(model["target_management"], "software_reconciliation")
        self.assertEqual(model["stop_time_in_force"], "day")

    def test_fractional_intraday_without_a_stop_is_flagged(self):
        model = execution.protection_model("intraday", True, [], [])
        self.assertEqual(model["model"], "fractional_missing_stop")
        self.assertFalse(model["stop_present"])

    def test_whole_share_intraday_uses_the_broker_bracket(self):
        model = execution.protection_model(
            "intraday", False, [{"time_in_force": "day"}],
            [{"order_type": "limit"}])
        self.assertEqual(model["model"], "whole_share_day_bracket")
        self.assertEqual(model["target_management"], "broker_oco")

    def test_passive_core_sleeve_has_no_protective_model(self):
        model = execution.protection_model("core_index", False, [], [])
        self.assertEqual(model["model"], "passive_core")
        self.assertFalse(model["stop_present"])
        self.assertEqual(model["target_management"], "none")

    def test_swing_core_reports_the_gtc_oto_stop(self):
        model = execution.protection_model(
            "core", False, [{"time_in_force": "gtc"}], [])
        self.assertEqual(model["model"], "gtc_oto_stop")
        self.assertEqual(model["target_management"], "trend_exit")

    def test_missing_multi_day_stop_is_flagged(self):
        model = execution.protection_model("swing", False, [], [])
        self.assertEqual(model["model"], "missing_stop")


class FractionalExecutionTests(unittest.TestCase):
    def test_cap_entry_quantity_returns_a_fraction(self):
        self.assertEqual(
            execution.cap_entry_quantity(
                100, 100.0, 0.02, 100_000.0, 1_990.0, 0.02, fractional=True),
            5.0,
        )

    def test_fractional_intraday_entry_is_a_simple_day_order(self):
        with patch.object(execution.settings, "entry_order_type", "market"):
            request, is_limit = execution.build_entry_request(
                "AAPL", 2.5, 100.0, 98.0, 104.0, "kt-1", "intraday",
                fractional=True)

        self.assertFalse(is_limit)
        self.assertEqual(request.type.value, "market")
        self.assertEqual(request.time_in_force.value, "day")
        self.assertIsNone(request.order_class)
        self.assertEqual(request.qty, 2.5)

    def test_whole_share_intraday_entry_keeps_the_bracket(self):
        with patch.object(execution.settings, "entry_order_type", "market"):
            request, _ = execution.build_entry_request(
                "AAPL", 10, 100.0, 98.0, 104.0, "kt-1", "intraday")

        self.assertEqual(request.order_class.value, "bracket")

    def test_fractional_stop_is_submitted_after_a_fill(self):
        class StopClient:
            def __init__(self):
                self.requests = []

            def get_order_by_id(self, order_id):
                return SimpleNamespace(
                    id=order_id, status="filled", filled_qty="2.5")

            def submit_order(self, request):
                self.requests.append(request)
                return SimpleNamespace(id="stop-1", status="new")

        client = StopClient()
        with patch("app.services.execution.time.monotonic", side_effect=[0, 0]):
            execution.ensure_fractional_stop(
                client, SimpleNamespace(id="entry-1"), "AAPL", 98.0)

        self.assertEqual(len(client.requests), 1)
        stop = client.requests[0]
        self.assertEqual(stop.type.value, "stop")
        self.assertEqual(stop.time_in_force.value, "day")
        self.assertAlmostEqual(float(stop.qty), 2.5)
        self.assertAlmostEqual(float(stop.stop_price), 98.0)

    def test_software_take_profit_closes_only_at_or_above_target(self):
        position = SimpleNamespace(symbol="AAPL", current_price="105.0")
        with patch.object(execution, "managed_pool_assignments",
                          return_value={"AAPL": "intraday"}), \
                patch.object(execution, "managed_order_take_profit_price",
                             return_value=104.0), \
                patch.object(execution, "close_managed_position",
                             return_value=True) as close:
            closed = execution.check_software_take_profits(object(), [position])

        self.assertEqual(closed, ["AAPL"])
        close.assert_called_once()

        position.current_price = "103.0"
        with patch.object(execution, "managed_pool_assignments",
                          return_value={"AAPL": "intraday"}), \
                patch.object(execution, "managed_order_take_profit_price",
                             return_value=104.0), \
                patch.object(execution, "close_managed_position") as close:
            closed = execution.check_software_take_profits(object(), [position])

        self.assertEqual(closed, [])
        close.assert_not_called()


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
