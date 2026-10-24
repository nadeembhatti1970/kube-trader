from datetime import datetime
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

from app.extended_hours import build_sample_rows, session_for, spread_bps

NY = ZoneInfo("America/New_York")


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=NY)


class SessionTests(unittest.TestCase):
    def test_sessions(self):
        # 2026-10-26 is a Monday.
        self.assertEqual(session_for(at(25, 20, 30)), "overnight")   # Sunday evening
        self.assertEqual(session_for(at(26, 2)), "overnight")
        self.assertEqual(session_for(at(26, 4)), "premarket")
        self.assertEqual(session_for(at(26, 9, 29)), "premarket")
        self.assertEqual(session_for(at(26, 9, 30)), "regular")
        self.assertEqual(session_for(at(26, 16)), "afterhours")
        self.assertEqual(session_for(at(26, 20)), "overnight")
        self.assertEqual(session_for(at(30, 2)), "overnight")         # Friday early morning
        self.assertEqual(session_for(at(30, 21)), "closed")           # Friday evening
        self.assertEqual(session_for(at(31, 12)), "closed")           # Saturday
        self.assertEqual(session_for(at(25, 12)), "closed")           # Sunday daytime


class SampleRowTests(unittest.TestCase):
    def test_spread_bps(self):
        self.assertAlmostEqual(spread_bps(99.9, 100.1), 20.0, places=1)
        self.assertIsNone(spread_bps(0, 100))
        self.assertIsNone(spread_bps(101, 100))
        self.assertIsNone(spread_bps(None, 100))

    def test_rows_include_symbols_without_quotes(self):
        quote = SimpleNamespace(bid_price=99.9, ask_price=100.1, bid_size=1, ask_size=2,
                                timestamp=at(26, 5))
        rows = build_sample_rows(
            ["AAPL", "XOM"], {"AAPL": quote},
            {"AAPL": {"attributes": ["overnight_tradable"]}},
            at(26, 5), "premarket", "iex")
        self.assertEqual([row["symbol"] for row in rows], ["AAPL", "XOM"])
        self.assertTrue(rows[0]["overnight_tradable"])
        self.assertFalse(rows[0]["overnight_halted"])
        self.assertIsNone(rows[1]["overnight_tradable"])
        self.assertIsNone(rows[1]["bid"])
        self.assertIsNone(rows[1]["spread_bps"])


if __name__ == "__main__":
    unittest.main()
