import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import main
import market_data as md

SETTLES = datetime(2026, 11, 5, 17, tzinfo=timezone.utc)
BEFORE = SETTLES - timedelta(minutes=10)
AFTER = SETTLES + timedelta(minutes=10)
KALSHI = {"ticker": "KXBTCD-26NOV0512-T81999.99", "strike": 81999.99, "settles_at": SETTLES,
          "source": "CF BRTI 60s average"}
POLY = {"id": "1", "strike": 82000.0, "settles_at": SETTLES, "source": "Binance BTC/USDT 1m close",
        "yes_token": "y", "no_token": "n", "fee_rate": 0.07, "min_size": 5}


def kalshi_raw(**overrides):
    return {"ticker": "KXBTCD-26NOV0517-T87749.99", "strike_type": "greater", "floor_strike": 87749.99,
            "close_time": "2026-11-05T22:00:00Z",
            "rules_primary": "If the simple average of the sixty seconds of CF Benchmarks' Bitcoin"
                             " Real-Time Index (BRTI) before 5 PM EST is above 87749.99", **overrides}


def poly_raw(**overrides):
    return {"id": "6389963", "outcomes": '["Yes", "No"]', "clobTokenIds": '["111", "222"]',
            "groupItemTitle": "82,000", "endDate": "2026-11-05T17:00:00Z",
            "description": 'the Binance 1 minute candle for BTC/USDT closing at 12:00',
            "feeSchedule": {"rate": 0.07}, "orderMinSize": 5, **overrides}


class NormalizeTests(unittest.TestCase):
    def test_kalshi_market(self):
        market = md.normalize_kalshi_market(kalshi_raw())
        self.assertEqual(market["strike"], 87749.99)
        self.assertEqual(market["source"], "CF BRTI 60s average")

    def test_kalshi_rejects_other_strike_types(self):
        with self.assertRaises(ValueError):
            md.normalize_kalshi_market(kalshi_raw(strike_type="less"))

    def test_poly_market(self):
        market = md.normalize_poly_market(poly_raw())
        self.assertEqual((market["strike"], market["yes_token"], market["no_token"]), (82000.0, "111", "222"))
        self.assertEqual(market["source"], "Binance BTC/USDT 1m close")

    def test_poly_rejects_swapped_outcomes(self):
        with self.assertRaises(ValueError):
            md.normalize_poly_market(poly_raw(outcomes='["No", "Yes"]'))

    def test_hourly_source(self):
        self.assertEqual(md.settlement_source('Binance BTC/USDT "1h": the 1 hour candle'), "Binance BTC/USDT 1h close")

    def test_one_bad_market_does_not_stop_the_rest(self):
        good = kalshi_raw()
        bad = kalshi_raw(ticker="BAD", floor_strike=None)
        with mock.patch.object(md, "get_json", return_value={"markets": [good, bad], "cursor": ""}):
            markets, skipped = md.fetch_kalshi_markets()
        self.assertEqual(len(markets), 1)
        self.assertEqual(skipped[0][:2], ("kalshi", "BAD"))


class BookTests(unittest.TestCase):
    def test_kalshi_asks_come_from_opposite_bids(self):
        # Shape and values from a live KXBTCD book: quoted asks were yes 0.98, no 0.04.
        book = md.kalshi_book_from({"orderbook_fp": {"no_dollars": [["0.0100", "1451.76"], ["0.0200", "725.38"]],
                                                     "yes_dollars": [["0.9500", "1329.27"], ["0.9600", "747.62"]]}})
        self.assertEqual(book["yes_asks"][0], (0.98, 725.38))
        self.assertEqual(book["no_asks"][0], (0.04, 747.62))

    def test_kalshi_empty_side_is_null(self):
        book = md.kalshi_book_from({"orderbook_fp": {"no_dollars": None, "yes_dollars": None}})
        self.assertEqual((book["yes_asks"], book["no_asks"]), ([], []))

    def test_poly_book_uses_best_prices_despite_worst_first_order(self):
        # Polymarket lists asks worst-first and bids lowest-first.
        book = md.poly_book_from({
            "asks": [{"price": "0.999", "size": "6035"}, {"price": "0.08", "size": "29.24"}],
            "bids": [{"price": "0.001", "size": "18488"}, {"price": "0.079", "size": "70.7"}],
        })
        self.assertEqual(book["yes_bids"][0], (0.079, 70.7))
        self.assertEqual(book["no_bids"][0], (0.92, 29.24))
        self.assertEqual(book["yes_asks"][0], (0.08, 29.24))
        self.assertEqual(book["no_asks"][0], (0.921, 70.7))


class FlagTests(unittest.TestCase):
    def test_healthy_book_has_no_flags(self):
        # Live Polymarket sample: best bids added up to 0.996.
        self.assertEqual(md.book_flags(md.make_book([(0.061, 10)], [(0.935, 10)])), [])

    def test_exactly_one_dollar_is_not_flagged(self):
        self.assertEqual(md.book_flags(md.make_book([(0.40, 10)], [(0.60, 10)])), [])

    def test_crossed_book(self):
        self.assertEqual(md.book_flags(md.make_book([(0.55, 10)], [(0.50, 10)])), ["crossed book"])

    def test_empty_sides_are_normal(self):
        self.assertEqual(md.book_flags(md.make_book([], [])), [])
        self.assertEqual(md.book_flags(md.make_book([(0.40, 10)], [])), [])


class SettlementTests(unittest.TestCase):
    def test_legs_won(self):
        self.assertEqual(md.legs_won("kalshi_yes_poly_no", "yes", "yes"), 1)
        self.assertEqual(md.legs_won("kalshi_yes_poly_no", "no", "yes"), 0)
        self.assertEqual(md.legs_won("poly_yes_kalshi_no", "no", "yes"), 2)

    def test_kalshi_settlement_before_and_after(self):
        with mock.patch.object(md, "get_json", return_value={"market": {"result": "", "expiration_value": ""}}):
            self.assertEqual(md.kalshi_settlement("T"), (None, None))
        with mock.patch.object(md, "get_json", return_value={"market": {"result": "no", "expiration_value": "80329.27"}}):
            self.assertEqual(md.kalshi_settlement("T"), ("no", 80329.27))

    def test_poly_result_waits_for_resolution(self):
        pending = {"closed": True, "umaResolutionStatus": "proposed", "outcomePrices": '["1", "0"]'}
        with mock.patch.object(md, "get_json", return_value=pending):
            self.assertIsNone(md.poly_result("1"))
        with mock.patch.object(md, "get_json", return_value=dict(pending, umaResolutionStatus="resolved")):
            self.assertEqual(md.poly_result("1"), "yes")


class FillTests(unittest.TestCase):
    def test_no_fill_when_fees_remove_the_gap(self):
        # 0.49 + 0.50 is under $1, but about 3.5 cents of fees make it a loss.
        self.assertIsNone(md.simulate_fill([(0.49, 100)], [(0.50, 100)], 0.07))

    def test_stops_at_the_level_that_is_no_longer_profitable(self):
        fill = md.simulate_fill([(0.40, 10), (0.60, 100)], [(0.40, 100)], 0.07)
        self.assertEqual(fill["contracts"], 10)
        self.assertAlmostEqual(fill["kalshi_cost"], 4.0)

    def test_respects_max_contracts_and_min_size(self):
        self.assertEqual(md.simulate_fill([(0.4, 500)], [(0.4, 500)], 0.07)["contracts"], 100)
        self.assertIsNone(md.simulate_fill([(0.4, 3)], [(0.4, 500)], 0.07, min_size=5))

    def test_kalshi_fee_matches_published_example(self):
        # 100 contracts at $0.55 costs $1.74.
        fill = md.simulate_fill([(0.55, 100)], [(0.05, 100)], 0.07)
        self.assertEqual(fill["kalshi_fee"], 1.74)


class MatchTests(unittest.TestCase):
    def test_matches_only_same_strike_and_time(self):
        later = dict(POLY, settles_at=datetime(2026, 11, 5, 22, tzinfo=timezone.utc))
        other_strike = dict(POLY, strike=82050.0)
        self.assertEqual(md.match_markets([KALSHI], [POLY, later, other_strike]), [(KALSHI, POLY)])

    def test_coverage_reports_gap_when_nothing_matches(self):
        later = dict(POLY, settles_at=datetime(2026, 11, 5, 22, tzinfo=timezone.utc))
        self.assertEqual(md.coverage([KALSHI], [later]), {"shared_times": 0, "closest_gap_minutes": 300})

    def test_coverage_without_equal_strikes(self):
        self.assertIsNone(md.coverage([KALSHI], [dict(POLY, strike=70000.0)])["closest_gap_minutes"])


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.db = main.open_db(":memory:")
        k_book = md.make_book([(0.30, 50)], [(0.60, 50)])
        p_book = md.make_book([(0.60, 50)], [(0.10, 50)])
        patches = [
            mock.patch.object(main, "fetch_kalshi_markets", return_value=([KALSHI], [("kalshi", "BAD", "bad")])),
            mock.patch.object(main, "fetch_poly_markets", return_value=([POLY], [])),
            mock.patch.object(main, "fetch_kalshi_book", return_value=k_book),
            mock.patch.object(main, "fetch_poly_book", return_value=p_book),
            mock.patch.object(main, "book_flags", return_value=[]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def count(self, table):
        return self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def report_lines(self):
        with mock.patch("builtins.print") as printed:
            main.report(self.db)
        return [call.args[0] for call in printed.call_args_list]

    def test_scan_logs_both_directions_with_sources(self):
        main.scan(self.db, now=BEFORE)
        rows = self.db.execute("SELECT direction, kalshi_source, poly_source FROM quotes ORDER BY direction").fetchall()
        self.assertEqual([row[0] for row in rows], ["kalshi_yes_poly_no", "poly_yes_kalshi_no"])
        self.assertEqual(rows[0][1:], ("CF BRTI 60s average", "Binance BTC/USDT 1m close"))
        self.assertEqual(self.count("scans"), 1)
        self.assertEqual(self.count("issues"), 1)

    def test_flagged_book_is_logged_but_left_out_of_report(self):
        with mock.patch.object(main, "book_flags", return_value=["crossed book"]):
            main.scan(self.db, now=BEFORE)
        self.assertEqual(self.count("quotes"), 2)
        self.assertIn("Quotes: 2  tradeable: 0  profitable after fees: 0", self.report_lines())

    def test_one_failed_book_is_recorded_and_scan_finishes(self):
        with mock.patch.object(main, "fetch_kalshi_book", side_effect=ValueError("bad book")):
            main.scan(self.db, now=BEFORE)
        self.assertEqual(self.count("quotes"), 0)
        self.assertEqual(self.count("scans"), 1)
        self.assertEqual(self.count("issues"), 2)

    def test_quote_without_asks_is_still_logged(self):
        with mock.patch.object(main, "fetch_kalshi_book", return_value=md.make_book([], [(0.60, 50)])):
            main.scan(self.db, now=BEFORE)
        rows = self.db.execute("SELECT direction, kalshi_ask, fillable_contracts FROM quotes ORDER BY direction").fetchall()
        self.assertEqual(rows[1], ("poly_yes_kalshi_no", None, 0))
        self.assertIn("Quotes: 2  tradeable: 1  profitable after fees: 1", self.report_lines())

    def test_report_flags_profit_that_compares_different_prices(self):
        main.scan(self.db, now=BEFORE)
        self.assertIn("  of those, settling on different prices (not risk-free): 1", self.report_lines())

    def test_settlement_is_recorded_once_both_platforms_publish(self):
        main.scan(self.db, now=BEFORE)
        with mock.patch.object(main, "kalshi_settlement", return_value=("yes", 82100.0)), \
                mock.patch.object(main, "poly_result", return_value=None):
            main.settle_pairs(self.db, AFTER)
        self.assertEqual(self.db.execute("SELECT kalshi_result, poly_result FROM settlements").fetchone(), ("yes", None))

        with mock.patch.object(main, "kalshi_settlement", return_value=("yes", 82100.0)), \
                mock.patch.object(main, "poly_result", return_value="no") as poly:
            main.settle_pairs(self.db, AFTER)
            poly.assert_not_called()  # checked less than SETTLE_CHECK_MINUTES ago
            main.settle_pairs(self.db, AFTER + timedelta(minutes=main.SETTLE_CHECK_MINUTES))
        self.assertEqual(self.db.execute("SELECT kalshi_result, kalshi_value, poly_result FROM settlements").fetchone(),
                         ("yes", 82100.0, "no"))

    def test_report_checks_profitable_quotes_against_results(self):
        main.scan(self.db, now=BEFORE)
        # Kalshi YES won and Polymarket NO won: both legs paid, which only a price split allows.
        self.db.execute("UPDATE settlements SET kalshi_result = 'yes', poly_result = 'no'")
        lines = self.report_lines()
        self.assertIn("Matched pairs tracked: 1  settled: 1  where the platforms disagreed: 1", lines)
        self.assertIn("Profitable quotes now settled: 1  paid once: 0  lost both legs: 0  won both: 1", lines)


if __name__ == "__main__":
    unittest.main()
