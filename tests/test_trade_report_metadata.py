import unittest
from unittest.mock import patch

from app import main


class TradeReportMetadataTests(unittest.TestCase):
    def test_report_includes_runtime_provenance(self):
        with patch.object(main, "managed_fills", return_value=[]), \
                patch.object(main, "latest_account_snapshot", return_value=None):
            report = main.trade_report(limit=100)

        metadata = report["report_metadata"]
        self.assertEqual(metadata["app_version"], main.settings.app_version)
        self.assertEqual(
            metadata["trading_mode"],
            "paper" if main.settings.alpaca_paper else "live",
        )
        self.assertEqual(
            metadata["current_strategy_config"]["signal_min_spread_pct"],
            main.settings.signal_min_spread_pct,
        )
        self.assertIn("new orders only", metadata["note"])


if __name__ == "__main__":
    unittest.main()
