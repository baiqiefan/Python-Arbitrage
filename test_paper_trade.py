import unittest
from datetime import datetime, timezone
from unittest import mock

import market_data as md
import paper_trade as pt

SETTLES = datetime(2026, 11, 5, 17, tzinfo=timezone.utc)
KALSHI = {"ticker": "KXBTCD-26NOV0512-T81999.99", "strike": 81999.99, "settles_at": SETTLES,
          "source": "CF BRTI 60s average"}
POLY = {"id": "1", "strike": 82000.0, "settles_at": SETTLES, "source": "Binance BTC/USDT 1m close",
        "yes_token": "y", "no_token": "n", "fee_rate": 0.07, "min_size": 5}


class PnlTests(unittest.TestCase):
    def trade(self, kalshi_result, poly_result):
        return {"direction": "kalshi_yes_poly_no", "contracts": 10, "kalshi_cost": 4.0, "poly_cost": 4.0,
                "kalshi_fee": 0.0, "poly_fee": 0.0, "kalshi_result": kalshi_result, "poly_result": poly_result}

    def test_agreeing_platforms_pay_once(self):
        self.assertAlmostEqual(pt.trade_pnl(self.trade("yes", "yes")), 2.0)
        self.assertAlmostEqual(pt.trade_pnl(self.trade("no", "no")), 2.0)

    def test_split_settlement_can_lose_everything(self):
        self.assertAlmostEqual(pt.trade_pnl(self.trade("no", "yes")), -8.0)
        self.assertAlmostEqual(pt.trade_pnl(self.trade("yes", "no")), 12.0)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.db = pt.open_db(":memory:")
        patches = [
            mock.patch.object(pt, "fetch_kalshi_markets", return_value=([KALSHI], [])),
            mock.patch.object(pt, "fetch_poly_markets", return_value=([POLY], [])),
            mock.patch.object(pt, "fetch_kalshi_book", return_value=md.make_book([(0.30, 50)], [(0.60, 50)])),
            mock.patch.object(pt, "fetch_poly_book", return_value=md.make_book([(0.60, 50)], [(0.10, 50)])),
            mock.patch.object(pt, "get_json", return_value={"price": "90000"}),
            mock.patch.object(pt, "book_flags", return_value=[]),
            mock.patch.object(pt, "should_take_trade", return_value=True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def settle(self):
        with mock.patch.object(pt, "kalshi_result", return_value="yes"), \
                mock.patch.object(pt, "poly_result", return_value="yes"):
            pt.settle_trades(self.db, now=datetime(2026, 11, 5, 18, tzinfo=timezone.utc))

    def test_scan_opens_one_trade_and_never_repeats_it(self):
        pt.scan(self.db, now="t1")
        pt.scan(self.db, now="t2")
        trades = self.db.execute("SELECT * FROM trades").fetchall()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["direction"], "kalshi_yes_poly_no")
        self.assertEqual(trades[0]["contracts"], 50)

    def test_settlement_records_profit(self):
        pt.scan(self.db, now="t1")
        self.settle()
        trade = self.db.execute("SELECT * FROM trades").fetchone()
        self.assertEqual(trade["kalshi_result"], "yes")
        self.assertGreater(trade["pnl"], 0)

    def test_report_counts_only_real_disagreements(self):
        pt.scan(self.db, now="t1")
        self.settle()
        with mock.patch("builtins.print") as printed:
            pt.report(self.db)
        printed.assert_any_call("Settled where the two platforms disagreed: 0")


if __name__ == "__main__":
    unittest.main()
