"""Simulated Kalshi vs Polymarket trades. Never places a real order.

Parked until main.py has collected data: should_take_trade has no rule yet.
"""
import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone

import requests

from market_data import (
    GAMMA_URL,
    KALSHI_URL,
    book_flags,
    directions,
    fetch_kalshi_book,
    fetch_kalshi_markets,
    fetch_poly_book,
    fetch_poly_markets,
    get_json,
    match_markets,
    parse_time,
    simulate_fill,
)

SPOT_URL = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"

DB_PATH = "paper_trades.db"
SCAN_SECONDS = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    opened_at TEXT, kalshi_ticker TEXT, poly_id TEXT, direction TEXT,
    strike REAL, settles_at TEXT, spot REAL, contracts INTEGER,
    kalshi_cost REAL, poly_cost REAL, kalshi_fee REAL, poly_fee REAL,
    kalshi_result TEXT, poly_result TEXT, pnl REAL,
    UNIQUE (kalshi_ticker, poly_id, direction)
);
"""


def should_take_trade(net_per_contract, contracts, spot_distance_pct):
    """Decide whether a simulated fill is worth recording as a paper trade."""
    raise NotImplementedError("Choose this rule once main.py has collected data")


def open_trade(db, now, k, p, direction, fill, spot):
    cursor = db.execute(
        "INSERT OR IGNORE INTO trades (opened_at, kalshi_ticker, poly_id, direction, strike,"
        " settles_at, spot, contracts, kalshi_cost, poly_cost, kalshi_fee, poly_fee)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now, k["ticker"], p["id"], direction, p["strike"], p["settles_at"].isoformat(), spot,
         fill["contracts"], fill["kalshi_cost"], fill["poly_cost"], fill["kalshi_fee"], fill["poly_fee"]),
    )
    return cursor.rowcount


def scan(db, now=None):
    now = now or datetime.now(timezone.utc).isoformat()
    kalshi_markets, _ = fetch_kalshi_markets()
    poly_markets, _ = fetch_poly_markets()
    pairs = match_markets(kalshi_markets, poly_markets)
    spot = float(get_json(SPOT_URL)["price"]) if pairs else None
    opened = 0

    for k, p in pairs:
        k_book = fetch_kalshi_book(k["ticker"])
        p_book = fetch_poly_book(p["yes_token"])
        if book_flags(k_book) or book_flags(p_book):
            continue
        for direction, k_asks, p_asks in directions(k_book, p_book):
            fill = simulate_fill(k_asks, p_asks, p["fee_rate"], p["min_size"])
            if not fill:
                continue
            distance = abs(spot - p["strike"]) / spot * 100
            if should_take_trade(fill["net_per_contract"], fill["contracts"], distance):
                new = open_trade(db, now, k, p, direction, fill, spot)
                opened += new
                if new:
                    print(f"  PAPER TRADE {k['ticker']} {direction}: {fill['contracts']} contracts,"
                          f" expected ${fill['net_per_contract'] * fill['contracts']:.2f}")

    db.commit()
    print(f"{now}  matched pairs {len(pairs)}  opened {opened}")


def kalshi_result(ticker):
    return get_json(f"{KALSHI_URL}/markets/{ticker}")["market"]["result"] or None


def poly_result(market_id):
    market = get_json(f"{GAMMA_URL}/markets/{market_id}")
    if not market["closed"] or market.get("umaResolutionStatus") != "resolved":
        return None
    prices = [float(price) for price in json.loads(market["outcomePrices"])]
    if prices == [1, 0]:
        return "yes"
    if prices == [0, 1]:
        return "no"
    return None


def trade_pnl(trade):
    """Each leg pays $1 per contract if its side won. A $0 or $2 payout means the sources split."""
    kalshi_side, poly_side = ("yes", "no") if trade["direction"] == "kalshi_yes_poly_no" else ("no", "yes")
    wins = (trade["kalshi_result"] == kalshi_side) + (trade["poly_result"] == poly_side)
    costs = trade["kalshi_cost"] + trade["poly_cost"] + trade["kalshi_fee"] + trade["poly_fee"]
    return wins * trade["contracts"] - costs


def settle_trades(db, now=None):
    now = now or datetime.now(timezone.utc)
    rows = db.execute("SELECT * FROM trades WHERE pnl IS NULL").fetchall()
    for row in rows:
        if parse_time(row["settles_at"]) > now:
            continue
        k_result = kalshi_result(row["kalshi_ticker"])
        p_result = poly_result(row["poly_id"])
        if not k_result or not p_result:
            continue
        trade = dict(row, kalshi_result=k_result, poly_result=p_result)
        db.execute(
            "UPDATE trades SET kalshi_result = ?, poly_result = ?, pnl = ? WHERE id = ?",
            (k_result, p_result, trade_pnl(trade), row["id"]),
        )
    db.commit()


def report(db):
    trades = db.execute("SELECT * FROM trades").fetchall()
    settled = [trade for trade in trades if trade["pnl"] is not None]
    # Both results describe the same question, so different answers mean the sources disagreed.
    split = [trade for trade in settled if trade["kalshi_result"] != trade["poly_result"]]
    print(f"Trades: {len(trades)}  settled: {len(settled)}  open: {len(trades) - len(settled)}")
    print(f"Settled where the two platforms disagreed: {len(split)}")
    print(f"Total profit: ${sum(trade['pnl'] for trade in settled):.2f}")


def open_db(path=DB_PATH):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument("--report", action="store_true", help="print results and exit")
    args = parser.parse_args()

    db = open_db()
    while True:
        try:
            settle_trades(db)
            if args.report:
                report(db)
                return
            scan(db)
        except (requests.RequestException, KeyError, ValueError) as error:
            db.rollback()
            print("Scan failed, will retry:", error)
        if args.once:
            return
        time.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    main()
