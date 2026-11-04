import unittest
from unittest.mock import patch

from app import main
from app.settings import Settings, strategy_config


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

    def test_strategy_provenance_records_effective_risk_controls(self):
        configured = Settings(
            _env_file=None,
            app_version="0.1.25",
            max_trade_risk_pct=0.02,
            symbol_loss_cooldown_seconds=1800,
            symbol_daily_loss_limit=50,
        )

        provenance = strategy_config(configured)

        self.assertEqual(
            provenance["risk_controls"]["configured_max_trade_risk_pct"], 0.02,
        )
        self.assertEqual(
            provenance["risk_controls"]["effective_max_trade_risk_pct"], 0.01,
        )
        self.assertEqual(
            provenance["risk_controls"]["symbol_loss_cooldown_seconds"], 1800,
        )
        self.assertEqual(provenance["risk_controls"]["symbol_daily_loss_limit"], 50)


if __name__ == "__main__":
    unittest.main()
