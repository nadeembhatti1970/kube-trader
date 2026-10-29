import unittest

from app.services.backtesting import evaluate


class EvaluateTests(unittest.TestCase):
    # Flat, then a marginal bump above the slow average that fades, then a real rally.
    CLOSES = [100.0] * 25 + [100.02, 100.0, 100.0] + [100 + i for i in range(1, 15)]

    def test_defaults_match_plain_crossover_and_trade_noise(self):
        plain = evaluate(self.CLOSES, 5, 20)
        self.assertIsNotNone(plain)

    def test_filter_ignores_marginal_crossover(self):
        noise = [100.0] * 25 + [100.01, 100.0, 100.0, 100.0, 100.0, 100.0]
        filtered = evaluate(noise, 5, 20, min_spread_pct=0.001, min_buy_rsi=55)
        self.assertEqual(filtered["total_return"], 0.0)

    def test_filter_still_takes_a_real_rally(self):
        filtered = evaluate(self.CLOSES, 5, 20, min_spread_pct=0.001, min_buy_rsi=55)
        self.assertGreater(filtered["total_return"], 0.0)

    def test_too_little_history_returns_none(self):
        self.assertIsNone(evaluate([100.0] * 10, 5, 20))


if __name__ == "__main__":
    unittest.main()
