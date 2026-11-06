import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

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
        self.assertIn("managed ledger", metadata["note"])

    def test_strategy_provenance_records_effective_risk_controls(self):
        configured = Settings(
            _env_file=None,
            app_version="0.1.25",
            max_trade_risk_pct=0.02,
            symbol_loss_cooldown_seconds=1800,
            symbol_daily_loss_limit=50,
        )

        provenance = strategy_config(configured)

        self.assertFalse(provenance["risk_controls"]["new_entries_enabled"])
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

    def test_research_candidate_does_not_change_baseline(self):
        bars = [{"open": 100.0 + index / 10, "high": 100.0 + index / 10,
                 "low": 100.0 + index / 10, "close": 100.0 + index / 10}
                for index in range(250)]
        for index, bar in enumerate(bars):
            bar["ts"] = f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00"
        with patch.object(main, "recent_bars", return_value=bars), \
                patch.object(main, "get_daily_bars", return_value=bars), \
                patch.object(main.settings, "intraday_flatten_minutes", 0), \
                patch.object(main.settings, "intraday_entry_cutoff_minutes", 0):
            client = TestClient(main.app)
            baseline = client.get("/research/backtests/AAPL").json()
            response = client.get(
                "/research/backtests/AAPL",
                params={"candidate_min_spread_pct": 0.02, "candidate_min_buy_rsi": 65},
            )
        self.assertEqual(response.status_code, 200)
        candidate = response.json()
        self.assertEqual(candidate["strategies"], baseline["strategies"])
        self.assertEqual(
            candidate["intraday_candidate"]["result"]["buy_confirmation_filter"],
            {"min_spread_pct": 0.02, "min_rsi_14": 65},
        )


if __name__ == "__main__":
    unittest.main()
