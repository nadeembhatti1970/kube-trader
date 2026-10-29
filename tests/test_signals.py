import unittest
from unittest.mock import patch

from app.services import signals


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


if __name__ == "__main__":
    unittest.main()
