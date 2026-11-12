import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import database
from app.services import execution

NY = ZoneInfo("America/New_York")


class FakeClient:
    def get_clock(self):
        now = datetime.now(timezone.utc)
        return SimpleNamespace(is_open=True, timestamp=now,
                               next_close=now + timedelta(hours=3))

    def get_orders(self, _request):
        return []

    def get_asset(self, _symbol):
        return SimpleNamespace(fractionable=True)

    def submit_order(self, _request):
        return SimpleNamespace(
            id="broker-1", status="accepted", submitted_at=None, filled_at=None,
            filled_qty=None, filled_avg_price=None,
        )


class CoverageCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class CoverageConnection:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args, **kwargs):
        return CoverageCursor(self.rows)


class MinuteBarCoverageTests(unittest.TestCase):
    def rows(self):
        return [
            {"symbol": "AAPL", "session_date": datetime(2026, 11, 6).date(), "bars": 390},
            {"symbol": "MSFT", "session_date": datetime(2026, 11, 6).date(), "bars": 120},
            {"symbol": "MSFT", "session_date": datetime(2026, 11, 7).date(), "bars": 0},
        ]

    def test_flags_thin_sessions_and_ignores_weekends(self):
        with patch.object(database, "connect",
                          return_value=CoverageConnection(self.rows())):
            report = database.minute_bar_coverage(days=7)

        self.assertEqual(report["symbol_sessions_observed"], 2)
        self.assertEqual(report["symbols_observed"], 2)
        self.assertFalse(report["all_sessions_meet_threshold"])
        self.assertEqual(report["sessions_below_threshold"], 1)
        self.assertEqual(report["below_threshold"][0]["symbol"], "MSFT")
        self.assertEqual(report["below_threshold"][0]["coverage_pct"], 30.8)
        self.assertEqual(report["lowest_coverage_pct"], 30.8)


class StopAndWindowTests(unittest.TestCase):
    def test_stop_repair_distance_follows_the_pool_default(self):
        with patch.object(execution.settings, "core_stop_loss_pct", 0.20), \
                patch.object(execution.settings, "stop_loss_pct", 0.02):
            self.assertEqual(execution.default_stop_loss_pct("core"), 0.20)
            self.assertEqual(execution.default_stop_loss_pct("swing"), 0.02)
            self.assertEqual(execution.default_stop_loss_pct("intraday"), 0.02)

    def test_minutes_since_open_handles_pre_open_and_weekends(self):
        open_time = datetime(2026, 11, 6, 9, 35, tzinfo=NY)
        self.assertAlmostEqual(execution.minutes_since_open(open_time), 5.0)
        pre_open = datetime(2026, 11, 6, 9, 20, tzinfo=NY)
        self.assertAlmostEqual(execution.minutes_since_open(pre_open), -10.0)
        weekend = datetime(2026, 11, 7, 9, 35, tzinfo=NY)
        self.assertIsNone(execution.minutes_since_open(weekend))


class PortfolioRiskCapTests(unittest.TestCase):
    def test_caps_quantity_by_remaining_portfolio_risk(self):
        self.assertEqual(
            execution.cap_entry_quantity(100, 100.0, 0.02, 100_000.0, 0.0, 0.02), 100)
        self.assertEqual(
            execution.cap_entry_quantity(100, 100.0, 0.02, 100_000.0, 1_990.0, 0.02), 5)
        self.assertEqual(
            execution.cap_entry_quantity(100, 100.0, 0.02, 100_000.0, 2_500.0, 0.02), 0)

    def test_zero_cap_and_zero_equity_disable_the_check(self):
        self.assertEqual(
            execution.cap_entry_quantity(100, 100.0, 0.02, 100_000.0, 9_999.0, 0.0), 100)
        self.assertEqual(
            execution.cap_entry_quantity(100, 100.0, 0.02, 0.0, 0.0, 0.02), 100)


class PlaceEntryGuardTests(unittest.TestCase):
    def setUp(self):
        # Module-level caches must not leak between cases.
        execution.FRACTIONABLE.clear()
        execution.TRADE_HISTORY["value"] = None
        execution.TRADE_HISTORY["captured_at"] = 0.0
        self.event = {
            "action": "buy",
            "symbol": "AAPL",
            "pool": "intraday",
            "price": 100.0,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self.risk = {
            "halted": False, "equity": 100_000.0, "buying_power": 100_000.0,
            "open_risk": 0.0,
            "reconciliation": {"allocations": {"intraday": {"remaining": 20_000.0}}},
        }

    def run_entry(self, client=None, **overrides):
        defaults = {
            "intraday_entry_start_minutes": 0,
            "intraday_entry_cutoff_minutes": 0,
            "intraday_daily_loss_limit_pct": 0.0,
            "max_entries_per_5min": 0,
            "max_open_positions": 20,
            "symbol_loss_cooldown_seconds": 0,
            "symbol_daily_loss_limit": 0.0,
            "min_entry_notional": 100.0,
        }
        defaults.update(overrides)
        patches = [patch.object(execution.settings, key, value)
                   for key, value in defaults.items()]
        patches.append(patch("app.services.execution.reserve_order", return_value=True))
        patches.append(patch("app.services.execution.update_order"))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return execution.place_entry(
            client or FakeClient(), self.event, "signal-1", object(), [],
            self.risk)

    def test_burst_limit_blocks_the_entry_without_reserving_an_order(self):
        with patch("app.services.execution.entry_count_since", return_value=3), \
                self.assertLogs("execution", level="INFO") as captured:
            self.run_entry(max_entries_per_5min=3)

        self.assertIn("entries already opened in the last 5 minutes", captured.output[0])

    def test_entry_start_window_blocks_early_intraday_entries(self):
        with patch("app.services.execution.minutes_since_open", return_value=2.0), \
                self.assertLogs("execution", level="INFO") as captured:
            self.run_entry(intraday_entry_start_minutes=5)

        self.assertIn("minutes since the open", captured.output[0])

    def test_pool_session_loss_limit_blocks_further_entries(self):
        exit_at = datetime.now(NY).replace(hour=10, minute=0, second=0, microsecond=0)
        trades = [{"pool": "intraday", "gross_realized_pnl": -600.0,
                   "exit_filled_at": exit_at}]
        with patch("app.services.execution.managed_fills", return_value=[]), \
                patch("app.services.execution.build_realized_trade_report",
                      return_value={"trades": trades}), \
                self.assertLogs("execution", level="WARNING") as captured:
            self.run_entry(intraday_daily_loss_limit_pct=0.005)

        self.assertIn("session loss limit", captured.output[0])

    def test_non_fractionable_asset_falls_back_to_whole_shares(self):
        class RecordingClient(FakeClient):
            def __init__(self, fractionable):
                self.fractionable = fractionable
                self.requests = []

            def get_asset(self, _symbol):
                return SimpleNamespace(fractionable=self.fractionable)

            def submit_order(self, request):
                self.requests.append(request)
                return super().submit_order(request)

        client = RecordingClient(False)
        with patch("app.services.execution.entry_count_since", return_value=0):
            self.run_entry(client=client)

        self.assertEqual(len(client.requests), 1)
        request = client.requests[0]
        self.assertEqual(request.order_class.value, "bracket")
        self.assertTrue(float(request.qty).is_integer())

    def test_fractionable_asset_uses_a_fractional_simple_entry(self):
        class RecordingClient(FakeClient):
            def __init__(self):
                self.requests = []

            def get_asset(self, _symbol):
                return SimpleNamespace(fractionable=True)

            def submit_order(self, request):
                self.requests.append(request)
                return super().submit_order(request)

        client = RecordingClient()
        self.event["price"] = 300.0
        with patch("app.services.execution.entry_count_since", return_value=0), \
                patch("app.services.execution.ensure_fractional_stop"):
            self.run_entry(client=client)

        self.assertEqual(len(client.requests), 1)
        request = client.requests[0]
        self.assertIsNone(request.order_class)
        self.assertFalse(float(request.qty).is_integer())

    def test_residual_quantity_below_half_the_intended_size_is_skipped(self):
        class RecordingClient(FakeClient):
            def __init__(self):
                self.requests = []

            def submit_order(self, request):
                self.requests.append(request)
                return super().submit_order(request)

        client = RecordingClient()
        # A $100 pool remainder would buy one share of a $100 stock while the
        # risk and symbol caps intend 50 shares: a residual, not a position.
        self.risk["reconciliation"]["allocations"]["intraday"]["remaining"] = 100.0
        with patch("app.services.execution.entry_count_since", return_value=0), \
                self.assertLogs("execution", level="INFO") as captured:
            self.run_entry(client=client)

        self.assertEqual(client.requests, [])
        self.assertIn("below half the intended", "".join(captured.output))

    def test_entry_below_the_minimum_notional_is_skipped(self):
        class RecordingClient(FakeClient):
            def __init__(self):
                self.requests = []

            def submit_order(self, request):
                self.requests.append(request)
                return super().submit_order(request)

        client = RecordingClient()
        # A small account caps the intended size at half a share, so the
        # residual rule passes and only the notional floor blocks the entry.
        self.risk["equity"] = 1_000.0
        self.risk["buying_power"] = 1_000.0
        self.risk["reconciliation"]["allocations"]["intraday"]["remaining"] = 45.0
        with patch("app.services.execution.entry_count_since", return_value=0), \
                self.assertLogs("execution", level="INFO") as captured:
            self.run_entry(client=client)

        self.assertEqual(client.requests, [])
        self.assertIn("below MIN_ENTRY_NOTIONAL", "".join(captured.output))

    def test_low_bar_coverage_blocks_the_entry(self):
        with patch("app.services.execution.minutes_since_open", return_value=100.0), \
                patch("app.services.execution.symbol_session_bar_count", return_value=10), \
                self.assertLogs("execution", level="WARNING") as captured:
            self.run_entry(min_entry_bar_coverage_pct=80.0)

        self.assertIn("expected session bars", captured.output[0])

    def test_unvalidated_configuration_blocks_when_required(self):
        with patch("app.services.execution.latest_strategy_validation", return_value=None), \
                self.assertLogs("execution", level="WARNING") as captured:
            self.run_entry(require_validated_strategy=True)

        self.assertIn("no validated out-of-sample record", captured.output[0])

    def test_validated_configuration_allows_the_entry(self):
        with patch("app.services.execution.latest_strategy_validation",
                   return_value={"validated": True}), \
                patch("app.services.execution.entry_count_since", return_value=0), \
                self.assertLogs("execution", level="WARNING") as captured:
            self.run_entry(require_validated_strategy=True)

        self.assertNotIn("no validated out-of-sample record", "".join(captured.output))

    def test_pool_loss_inside_the_limit_does_not_block(self):
        exit_at = datetime.now(NY).replace(hour=10, minute=0, second=0, microsecond=0)
        trades = [{"pool": "intraday", "gross_realized_pnl": -100.0,
                   "exit_filled_at": exit_at}]
        with patch("app.services.execution.managed_fills", return_value=[]), \
                patch("app.services.execution.build_realized_trade_report",
                      return_value={"trades": trades}), \
                patch("app.services.execution.entry_count_since", return_value=0), \
                self.assertLogs("execution", level="WARNING") as captured:
            self.run_entry(intraday_daily_loss_limit_pct=0.005)

        self.assertNotIn("session loss limit", "".join(captured.output))

    def test_fractionability_lookup_is_cached_per_symbol(self):
        class CountingClient(FakeClient):
            def __init__(self):
                self.asset_calls = 0

            def get_asset(self, _symbol):
                self.asset_calls += 1
                return SimpleNamespace(fractionable=True)

        client = CountingClient()
        with patch("app.services.execution.entry_count_since", return_value=0), \
                patch("app.services.execution.ensure_fractional_stop"):
            self.run_entry(client=client)
            self.run_entry(client=client)

        self.assertEqual(client.asset_calls, 1)

    def test_realized_trade_history_is_reused_within_its_ttl(self):
        calls = []

        def report(*_args, **_kwargs):
            calls.append(1)
            return {"trades": []}

        with patch("app.services.execution.managed_fills", return_value=[]), \
                patch("app.services.execution.build_realized_trade_report", side_effect=report):
            execution.session_trades()
            execution.session_trades()

        self.assertEqual(len(calls), 1)


class ReconcileCostTests(unittest.TestCase):
    def setUp(self):
        execution.SESSION_BASELINE["session_date"] = None
        execution.SESSION_BASELINE["value"] = None
        execution.SNAPSHOT["captured_at"] = 0.0

    def test_session_baseline_is_read_once_per_session(self):
        class History:
            equity = [100.0, 101.0]

        calls = []

        class Client:
            def get_portfolio_history(self, _request):
                calls.append(1)
                return History()

        today = datetime.now(NY).date()
        first = execution.session_baseline(Client(), today)
        second = execution.session_baseline(Client(), today)

        self.assertAlmostEqual(first, 100.0)
        self.assertAlmostEqual(second, 100.0)
        self.assertEqual(len(calls), 1)

    def test_session_baseline_refreshes_for_a_new_session(self):
        class History:
            equity = [100.0]

        class Client:
            def get_portfolio_history(self, _request):
                return History()

        today = datetime.now(NY).date()
        execution.session_baseline(Client(), today)
        execution.session_baseline(Client(), today + timedelta(days=1))

        self.assertEqual(execution.SESSION_BASELINE["session_date"], today + timedelta(days=1))

    def test_snapshot_is_written_once_per_window(self):
        self.assertTrue(execution.snapshot_due())
        self.assertFalse(execution.snapshot_due())
        execution.SNAPSHOT["captured_at"] -= execution.SNAPSHOT_MIN_SECONDS
        self.assertTrue(execution.snapshot_due())


if __name__ == "__main__":
    unittest.main()
