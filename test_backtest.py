import unittest

import backtest

HOUR = 1_793_955_600  # 2026-11-06 09:00 UTC


def row(strike, no_ask=0.10, mid=0.95, binance=75900.0, kalshi_result="no", binance_close=75880.0, settles=HOUR):
    return (settles, strike, no_ask, mid, binance, 0.07, kalshi_result, binance_close)


class PolySlugTests(unittest.TestCase):
    def test_morning_hour(self):
        self.assertEqual(backtest.poly_slug("KXBTCD-26NOV0604-T75599.99", 75600),
                         "bitcoin-above-75600-on-november-6-2026-4am-et")

    def test_noon_and_midnight(self):
        self.assertTrue(backtest.poly_slug("KXBTCD-26SEP1212-T77199.99", 77200).endswith("september-12-2026-12pm-et"))
        self.assertTrue(backtest.poly_slug("KXBTCD-26SEP1200-T77199.99", 77200).endswith("september-12-2026-12am-et"))


class LastKnownTests(unittest.TestCase):
    def test_uses_latest_point_at_or_before_the_minute(self):
        points = [(60, 0.1), (120, 0.2), (180, 0.3)]
        self.assertEqual(backtest.last_known(points, 130), 0.2)
        self.assertEqual(backtest.last_known(points, 120), 0.2)
        self.assertIsNone(backtest.last_known(points, 30))


class ReverseBacktestTests(unittest.TestCase):
    def test_wins_both_legs_when_the_strike_ends_between_the_two_prices(self):
        # Kalshi settled no (BRTI below 75,700) and Binance closed above it.
        (trade,) = backtest.reverse_backtest([row(75700)], half_spread=0.02)
        self.assertEqual(trade[:3], (HOUR, 75700, 2))
        self.assertAlmostEqual(trade[3], 2 - 1 + backtest.net_per_contract(0.10, 0.97, 0.07))

    def test_one_leg_pays_when_both_prices_end_above_the_strike(self):
        (trade,) = backtest.reverse_backtest([row(75700, kalshi_result="yes")], half_spread=0.02)
        self.assertEqual(trade[2], 1)
        self.assertLess(trade[3], 0)

    def test_takes_each_market_once_at_its_first_matching_minute(self):
        rows = [row(75700, no_ask=0.40),   # too expensive
                row(75700, no_ask=0.10),   # taken
                row(75700, no_ask=0.05)]
        trades = backtest.reverse_backtest(rows, half_spread=0.02)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0][3], 2 - 1 + backtest.net_per_contract(0.10, 0.97, 0.07))

    def test_skips_strikes_outside_the_rule_and_empty_books(self):
        rows = [row(75850),            # only $50 below Binance
                row(75400),            # $500 below Binance
                row(75700, mid=0.5)]   # empty Polymarket book
        self.assertEqual(backtest.reverse_backtest(rows, half_spread=0.02), [])


class SummarizeTests(unittest.TestCase):
    def test_counts_days_and_wins(self):
        trades = [(HOUR, 75700, 2, 0.9), (HOUR, 75500, 1, -0.1), (HOUR + 86400, 75700, 1, -0.1)]
        text = backtest.summarize(trades)
        self.assertIn("3 trades over 2 days", text)
        self.assertIn("won both: 1 (33%)", text)


if __name__ == "__main__":
    unittest.main()
