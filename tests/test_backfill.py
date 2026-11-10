import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.services import backfill


class BarsToRowsTests(unittest.TestCase):
    def test_converts_barset_to_rows(self):
        moment = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)
        barset = SimpleNamespace(data={"aapl": [SimpleNamespace(
            timestamp=moment, open="1.0", high="2.0", low="0.5", close="1.5",
            volume="100")]})

        rows = backfill.bars_to_rows(barset)

        self.assertEqual(rows, [{
            "symbol": "AAPL", "ts": moment, "open": 1.0, "high": 2.0,
            "low": 0.5, "close": 1.5, "volume": 100,
        }])

    def test_missing_data_is_empty(self):
        self.assertEqual(backfill.bars_to_rows(SimpleNamespace(data=None)), [])


class DailyBarsToRowsTests(unittest.TestCase):
    def test_converts_barset_to_daily_rows(self):
        moment = datetime(2026, 11, 6, 21, 0, tzinfo=timezone.utc)
        barset = SimpleNamespace(data={"aapl": [SimpleNamespace(
            timestamp=moment, open="1.0", high="2.0", low="0.5", close="1.5",
            volume="100")]})

        rows = backfill.daily_bars_to_rows(barset)

        self.assertEqual(rows, [{
            "symbol": "AAPL", "day": moment.date(), "open": 1.0, "high": 2.0,
            "low": 0.5, "close": 1.5, "volume": 100,
        }])


class ChunkedTests(unittest.TestCase):
    def test_chunks_with_a_tail(self):
        self.assertEqual(list(backfill.chunked([1, 2, 3, 4, 5], 2)),
                         [[1, 2], [3, 4], [5]])

    def test_non_positive_size_still_advances(self):
        self.assertEqual(list(backfill.chunked([1, 2, 3], 0)), [[1], [2], [3]])


class SymbolResolutionTests(unittest.TestCase):
    def test_explicit_symbols_are_normalized_and_deduplicated(self):
        with patch.object(backfill, "latest_universe_snapshot") as snapshot:
            result = backfill.resolve_symbols(" aapl, MSFT,aapl ", 10)

        snapshot.assert_not_called()
        self.assertEqual(result, ["AAPL", "MSFT"])

    def test_universe_defaults_to_selected_symbols(self):
        with patch.object(backfill, "latest_universe_snapshot", return_value=[
                {"symbol": "AAPL", "selected": True},
                {"symbol": "MSFT", "selected": False}]):
            self.assertEqual(backfill.resolve_symbols(None, 10), ["AAPL"])

    def test_empty_selection_raises(self):
        with patch.object(backfill, "latest_universe_snapshot", return_value=[]):
            with self.assertRaises(ValueError):
                backfill.resolve_symbols(None, 10)

    def test_limit_is_applied(self):
        with patch.object(backfill, "latest_universe_snapshot", return_value=[
                {"symbol": symbol, "selected": True}
                for symbol in ("AAPL", "MSFT", "NVDA")]):
            self.assertEqual(backfill.resolve_symbols(None, 2), ["AAPL", "MSFT"])


class BackfillRunTests(unittest.TestCase):
    def test_requests_are_chunked_and_rows_are_stored(self):
        instances = []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.requests = []
                instances.append(self)

            def get_stock_bars(self, request):
                self.requests.append(request)
                return SimpleNamespace(data={})

        with patch.object(backfill, "StockHistoricalDataClient", FakeClient), \
                patch.object(backfill, "save_bars", return_value=0) as save, \
                patch.object(backfill.settings, "alpaca_api_key", "key"), \
                patch.object(backfill.settings, "alpaca_secret_key", "secret"):
            stored = backfill.backfill(
                30, ["AAPL", "MSFT", "NVDA"], backfill.DataFeed.IEX, batch_symbols=2)

        self.assertEqual(stored, 0)
        self.assertEqual(len(instances[0].requests), 2)
        self.assertEqual(
            list(instances[0].requests[0].symbol_or_symbols), ["AAPL", "MSFT"])
        self.assertEqual(save.call_count, 2)

    def test_daily_timeframe_writes_daily_bars(self):
        instances = []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.requests = []
                instances.append(self)

            def get_stock_bars(self, request):
                self.requests.append(request)
                return SimpleNamespace(data={})

        with patch.object(backfill, "StockHistoricalDataClient", FakeClient), \
                patch.object(backfill, "save_daily_bars", return_value=0) as save_daily, \
                patch.object(backfill, "save_bars") as save_minute, \
                patch.object(backfill.settings, "alpaca_api_key", "key"), \
                patch.object(backfill.settings, "alpaca_secret_key", "secret"):
            stored = backfill.backfill(
                1000, ["AAPL"], backfill.DataFeed.IEX, timeframe="day")

        self.assertEqual(stored, 0)
        save_daily.assert_called_once()
        save_minute.assert_not_called()
        self.assertEqual(len(instances[0].requests), 1)

    def test_unknown_timeframe_is_rejected(self):
        with self.assertRaises(ValueError):
            backfill.backfill(30, ["AAPL"], backfill.DataFeed.IEX, timeframe="hour")

    def test_missing_credentials_fail_loudly(self):
        with patch.object(backfill.settings, "alpaca_api_key", ""), \
                patch.object(backfill.settings, "alpaca_secret_key", ""):
            with self.assertRaises(RuntimeError):
                backfill.backfill(30, ["AAPL"], backfill.DataFeed.IEX)


if __name__ == "__main__":
    unittest.main()
