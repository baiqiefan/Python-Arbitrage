import unittest
from datetime import datetime, timezone
from unittest import mock

import market_data as md
import sports


def kalshi_market(code, yes_ask, yes_ask_size, no_ask, yes_bid_size):
    return {"ticker": f"KXNBAGAME-26NOV11BOSTOR-{code}", "yes_ask_dollars": str(yes_ask),
            "yes_ask_size_fp": str(yes_ask_size), "no_ask_dollars": str(no_ask), "yes_bid_size_fp": str(yes_bid_size)}


BOS = kalshi_market("BOS", 0.72, 100, 0.29, 50)
TOR = kalshi_market("TOR", 0.29, 80, 0.72, 40)


class KalshiGamesTests(unittest.TestCase):
    def test_builds_polymarket_slugs_in_both_team_orders(self):
        event = {"event_ticker": "KXNBAGAME-26NOV11BOSTOR", "markets": [BOS, TOR]}
        with mock.patch.object(sports, "get_json", return_value={"events": [event, {"event_ticker": "X-26NOV11A", "markets": [BOS]}]}):
            games, count = sports.kalshi_games("nba", "KXNBAGAME")
        self.assertEqual(count, 2)
        self.assertEqual(games, {"nba-bos-tor-2026-11-11": (BOS, TOR), "nba-tor-bos-2026-11-11": (TOR, BOS)})


class KalshiAskTests(unittest.TestCase):
    def test_takes_the_cheaper_of_yes_on_the_team_and_no_on_the_other(self):
        cheap_no = kalshi_market("TOR", 0.29, 80, 0.70, 40)
        self.assertEqual(sports.kalshi_ask(BOS, cheap_no), (0.70, 40.0))
        self.assertEqual(sports.kalshi_ask(BOS, TOR), (0.72, 100.0))  # same price: the larger offer

    def test_no_quote(self):
        empty = kalshi_market("BOS", 1.0, 0, 1.0, 0)
        self.assertIsNone(sports.kalshi_ask(empty, empty))


class CoversTests(unittest.TestCase):
    def test_prices_each_team_on_kalshi_against_the_other_on_polymarket(self):
        # Polymarket's first outcome is Boston: bids 0.70 (so Toronto asks 0.30), Toronto bids 0.27 (Boston asks 0.73).
        book = md.make_book([(0.70, 200)], [(0.27, 300)])
        priced = sports.covers((BOS, TOR), book, 0.05)
        self.assertEqual([row[0] for row in priced], ["BOS", "TOR"])
        self.assertEqual(priced[0][1:5], (0.72, 100.0, 0.30, 200))
        self.assertAlmostEqual(priced[0][5], md.net_per_contract(0.72, 0.30, 0.05))
        self.assertEqual(priced[1][1:5], (0.29, 80.0, 0.73, 300))

    def test_skips_a_side_with_no_polymarket_ask(self):
        book = md.make_book([], [(0.27, 300)])
        self.assertEqual([row[0] for row in sports.covers((BOS, TOR), book, 0.05)], ["TOR"])


class GameStartTests(unittest.TestCase):
    def test_reads_both_time_formats(self):
        start = datetime(2026, 11, 15, 18, tzinfo=timezone.utc)
        self.assertEqual(sports.game_start({"gameStartTime": "2026-11-15 18:00:00+00"}), start)
        self.assertEqual(sports.game_start({"endDate": "2026-11-15T18:00:00Z"}), start)


if __name__ == "__main__":
    unittest.main()
