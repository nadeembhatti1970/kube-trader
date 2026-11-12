import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app.services import signals
from app.settings import Settings, strategy_config


class RegimeFilterTests(unittest.TestCase):
    def bars(self, rising=True, count=30):
        result = []
        for index in range(count):
            close = 100.0 + index if rising else 200.0 - index
            result.append({"time": f"2026-01-{index + 1:02d}", "close": close})
        return result

    def run_daily(self, bars, state, enabled=True, sma=20):
        with patch.object(signals.settings, "regime_filter_enabled", enabled), \
                patch.object(signals.settings, "regime_symbol", "SPY"), \
                patch.object(signals.settings, "regime_sma", sma), \
                patch.object(signals, "daily_bars_for_signal", return_value=bars), \
                patch.object(signals, "daily_strategy_state", return_value=state), \
                patch.object(signals, "horizon_strategy_state", return_value=None), \
                patch.object(signals, "save_horizon_strategy_state"):
            return signals.daily_signals(
                "AAPL", 100.0, "2026-01-30T15:00:00+00:00")

    def test_risk_off_blocks_new_daily_buys(self):
        result = self.run_daily(self.bars(rising=False), (False, True, 0.02))
        self.assertEqual(result, [])

    def test_risk_on_allows_daily_buys(self):
        result = self.run_daily(self.bars(rising=True), (False, True, 0.02))
        self.assertEqual([item["action"] for item in result], ["buy", "buy"])

    def test_disabled_filter_allows_buys(self):
        result = self.run_daily(
            self.bars(rising=False), (False, True, 0.02), enabled=False)
        self.assertEqual([item["action"] for item in result], ["buy", "buy"])

    def test_risk_off_still_allows_daily_sells(self):
        result = self.run_daily(self.bars(rising=False), (True, False, 0.02))
        self.assertEqual([item["action"] for item in result], ["sell", "sell"])

    def test_short_regime_history_fails_closed(self):
        result = self.run_daily(self.bars(rising=True, count=10), (False, True, 0.02))
        self.assertEqual(result, [])


class EntryConfirmedTests(unittest.TestCase):
    def confirmed(self, fast, slow, rsi_value, spread=0.001, min_rsi=55.0):
        with patch.object(signals.settings, "signal_min_spread_pct", spread), \
                patch.object(signals.settings, "signal_min_buy_rsi", min_rsi):
            return signals.entry_confirmed(fast, slow, rsi_value)

    def test_accepts_wide_spread_with_momentum(self):
        self.assertTrue(self.confirmed(100.2, 100.0, 60))

    def test_rejects_marginal_crossover(self):
        self.assertFalse(self.confirmed(100.05, 100.0, 60))

    def test_rejects_weak_momentum(self):
        self.assertFalse(self.confirmed(100.2, 100.0, 50))

    def test_zero_thresholds_restore_plain_crossover(self):
        self.assertTrue(self.confirmed(100.01, 100.0, 10, spread=0.0, min_rsi=0.0))

    def test_strategy_provenance_uses_daily_strategy_and_signal_values(self):
        config = strategy_config(
            Settings(_env_file=None),
            {
                "pool": "core",
                "fast_sma": 101.5,
                "slow_sma": 99.5,
                "strategy_bar_date": "2026-11-05",
            },
        )

        self.assertEqual(config["strategy"], "core-sma50-200-crossover-v1")
        self.assertEqual(config["strategy_pool"], "core")
        self.assertEqual(config["signal_frequency"], "daily")
        self.assertEqual(
            (config["signal_fast_bars"], config["signal_slow_bars"]), (50, 200),
        )
        self.assertEqual(config["signal_values"]["fast_sma"], 101.5)
        self.assertEqual(config["signal_values"]["slow_sma"], 99.5)

    def test_swing_strategy_provenance_captures_atr_and_signal_price(self):
        config = strategy_config(
            Settings(_env_file=None),
            {
                "pool": "swing",
                "signal_price": 105.0,
                "signal_reference_sma": 102.0,
                "atr_fraction": 0.025,
                "strategy_bar_date": "2026-11-05",
            },
        )

        self.assertEqual(config["strategy"], "swing-close-vs-sma20-v1")
        self.assertEqual(
            (config["signal_fast_bars"], config["signal_slow_bars"]), (1, 20),
        )
        self.assertEqual(config["signal_values"]["atr_fraction"], 0.025)
        self.assertEqual(config["signal_values"]["signal_price"], 105.0)

    def test_daily_signals_emit_pool_specific_indicator_values(self):
        closes = [100.0 + index * 0.1 for index in range(201)]
        bars = [
            {
                "time": (datetime(2026, 1, 1) + timedelta(days=index)).date().isoformat(),
                "close": close,
                "high": close + 1,
                "low": close - 1,
            }
            for index, close in enumerate(closes)
        ]
        with patch.object(signals, "daily_bars_for_signal", return_value=bars), \
                patch.object(signals, "daily_strategy_state",
                             side_effect=[(False, True, 0.025), (False, True, 0.0)]), \
                patch.object(signals, "horizon_strategy_state", return_value=None), \
                patch.object(signals, "save_horizon_strategy_state"):
            events = signals.daily_signals("AAPL", closes[-1], "2026-11-05T15:00:00Z")

        by_pool = {event["pool"]: event for event in events}
        self.assertAlmostEqual(by_pool["swing"]["signal_price"], closes[-1])
        self.assertAlmostEqual(
            by_pool["swing"]["signal_reference_sma"], sum(closes[-20:]) / 20,
        )
        self.assertEqual(by_pool["swing"]["atr_fraction"], 0.025)
        self.assertAlmostEqual(by_pool["core"]["fast_sma"], sum(closes[-50:]) / 50)
        self.assertAlmostEqual(by_pool["core"]["slow_sma"], sum(closes[-200:]) / 200)


class SignalBatchTests(unittest.TestCase):
    def test_bars_are_grouped_by_symbol_in_arrival_order(self):
        messages = [
            ("1-0", {"data": '{"symbol": "AAPL", "ts": "t1"}'}),
            ("2-0", {"data": '{"symbol": "MSFT", "ts": "t2"}'}),
            ("3-0", {"data": '{"symbol": "AAPL", "ts": "t3"}'}),
        ]
        grouped = signals.group_by_symbol(messages)

        self.assertEqual(list(grouped), ["AAPL", "MSFT"])
        self.assertEqual([mid for mid, _ in grouped["AAPL"]], ["1-0", "3-0"])

    def test_signals_carry_the_bar_length_for_latency_measurement(self):
        bars = [{"time": f"2026-01-{i + 1:02d}", "close": 100.0 + i} for i in range(25)]
        with patch.object(signals, "daily_bars_for_signal", return_value=bars), \
                patch.object(signals, "daily_strategy_state", side_effect=[(False, True, 0.02),
                                                                         (False, True, 0.0)]), \
                patch.object(signals, "horizon_strategy_state", return_value=None), \
                patch.object(signals, "save_horizon_strategy_state"):
            events = signals.daily_signals("AAPL", bars[-1]["close"], "2026-11-05T15:00:00Z")

        self.assertTrue(events)
        for event in events:
            self.assertEqual(event["signal_bar_seconds"], signals.SIGNAL_BAR_SECONDS)


if __name__ == "__main__":
    unittest.main()
