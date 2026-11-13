import unittest
from unittest.mock import patch

import pandas as pd

from app import market_data
from app.settings import Settings


def frame(index, columns=("Open", "High", "Low", "Close", "Volume")):
    return pd.DataFrame(
        [[100.0, 101.0, 99.0, 100.5, 1_000] for _ in index],
        index=index, columns=list(columns),
    )


class FrameRowsTests(unittest.TestCase):
    def test_parses_a_flat_frame(self):
        index = pd.to_datetime(["2026-11-10", "2026-11-11"])
        rows = market_data._frame_rows(frame(index), 5)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["time"], "2026-11-11")
        self.assertAlmostEqual(rows[-1]["close"], 100.5)
        self.assertEqual(rows[-1]["volume"], 1_000)

    def test_tolerates_duplicated_column_labels(self):
        # Concurrent yfinance calls can return a frame whose flattened columns repeat,
        # which made row["Open"] a Series instead of a scalar.
        index = pd.to_datetime(["2026-11-11"])
        duplicated = pd.concat([frame(index), frame(index)], axis=1)

        rows = market_data._frame_rows(duplicated, 5)

        self.assertEqual(len(rows), 1)
        self.assertIsInstance(rows[0]["open"], float)
        self.assertAlmostEqual(rows[0]["open"], 100.0)

    def test_flattens_a_multi_level_column_index(self):
        index = pd.to_datetime(["2026-11-11"])
        base = frame(index)
        base.columns = pd.MultiIndex.from_product([list(base.columns), ["AAPL"]])

        rows = market_data._frame_rows(base, 5)

        self.assertEqual(rows[0]["close"], 100.5)

    def test_empty_frame_is_an_error(self):
        with self.assertRaises(ValueError):
            market_data._frame_rows(frame(pd.to_datetime([])), 5)


class DownloadTests(unittest.TestCase):
    def test_serializes_the_yfinance_call(self):
        index = pd.to_datetime(["2026-11-11"])
        settings = Settings(market_data_provider="yfinance")
        held = []

        def download(*_args, **_kwargs):
            held.append(market_data.YFINANCE_LOCK.locked())
            return frame(index)

        with patch.object(market_data.yf, "download", side_effect=download):
            rows = market_data.get_daily_bars("BRK.B", 5, settings)

        self.assertEqual(rows[0]["time"], "2026-11-11")
        self.assertEqual(held, [True])


if __name__ == "__main__":
    unittest.main()
