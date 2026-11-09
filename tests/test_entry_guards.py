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

    def run_entry(self, **overrides):
        defaults = {
            "intraday_entry_start_minutes": 0,
            "intraday_entry_cutoff_minutes": 0,
            "intraday_daily_loss_limit_pct": 0.0,
            "max_entries_per_5min": 0,
            "max_open_positions": 20,
            "symbol_loss_cooldown_seconds": 0,
            "symbol_daily_loss_limit": 0.0,
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
            FakeClient(), self.event, "signal-1", object(), [], self.risk)

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


if __name__ == "__main__":
    unittest.main()
