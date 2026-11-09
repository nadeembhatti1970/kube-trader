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

    def test_report_includes_fee_estimate_latency_coverage_and_benchmark(self):
        with patch.object(main, "managed_fills", return_value=[]), \
                patch.object(main, "latest_account_snapshot", return_value=None), \
                patch.object(main, "account_fee_activities", return_value=[
                    {"activity_date": "2026-11-06", "amount": -1.0}]), \
                patch.object(main, "minute_bar_coverage", return_value={
                    "all_sessions_meet_threshold": True}), \
                patch.object(main, "benchmark_return", return_value={
                    "symbol": "SPY", "return_pct": 1.0}):
            report = main.trade_report(limit=100)

        self.assertEqual(
            report["fee_estimate"]["basis"],
            "account_fee_activity_prorated_by_exit_notional_utc_day",
        )
        self.assertIn("entry", report["execution_latency"])
        self.assertTrue(report["bar_coverage"]["all_sessions_meet_threshold"])
        self.assertEqual(report["benchmark"]["symbol"], "SPY")
        self.assertEqual(report["report_metadata"]["alpaca_data_feed"],
                         main.settings.alpaca_data_feed)
        self.assertEqual(
            report["report_metadata"]["execution_latency_warn_seconds"],
            main.settings.execution_latency_warn_seconds,
        )

    def test_adopt_endpoint_requires_a_current_position(self):
        snapshot = {"positions": [{"symbol": "COST"}]}
        client = TestClient(main.app)
        with patch.object(main, "latest_account_snapshot", return_value=snapshot), \
                patch.object(main, "save_position_assignment") as save:
            adopted = client.post("/admin/positions/cost/adopt", params={"pool": "core"})
            missing = client.post("/admin/positions/JNJ/adopt", params={"pool": "core"})
            invalid = client.post("/admin/positions/COST/adopt", params={"pool": "everything"})

        self.assertEqual(adopted.status_code, 200)
        self.assertEqual(adopted.json()["pool"], "core")
        save.assert_called_once_with("COST", "core", None)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(invalid.status_code, 400)

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
