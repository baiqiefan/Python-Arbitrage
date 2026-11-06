"""Compare Kalshi and Polymarket Bitcoin order books and log the results."""
import argparse
import sqlite3
import time
from datetime import datetime, timezone

import requests

from market_data import (
    book_flags,
    coverage,
    directions,
    fetch_kalshi_book,
    fetch_kalshi_markets,
    fetch_poly_book,
    fetch_poly_markets,
    match_markets,
    net_per_contract,
    simulate_fill,
)

DB_PATH = "market_data.db"
SCAN_SECONDS = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    ts TEXT, kalshi_open INTEGER, poly_open INTEGER, shared_times INTEGER,
    matched_pairs INTEGER, closest_gap_minutes REAL, skipped INTEGER
);
CREATE TABLE IF NOT EXISTS quotes (
    ts TEXT, kalshi_ticker TEXT, poly_id TEXT, direction TEXT,
    kalshi_source TEXT, poly_source TEXT,
    kalshi_ask REAL, kalshi_size REAL, poly_ask REAL, poly_size REAL,
    net_per_contract REAL, fillable_contracts INTEGER, leg_gap_seconds REAL, flags TEXT
);
CREATE TABLE IF NOT EXISTS issues (
    ts TEXT, platform TEXT, market_id TEXT, reason TEXT
);
"""


def open_db(path=DB_PATH):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def record_issue(db, now, platform, market_id, reason):
    db.execute("INSERT INTO issues VALUES (?, ?, ?, ?)", (now, platform, market_id, reason))


def scan_pair(db, now, k, p):
    k_book = fetch_kalshi_book(k["ticker"])
    p_book = fetch_poly_book(p["yes_token"])
    leg_gap = abs((p_book["fetched_at"] - k_book["fetched_at"]).total_seconds())

    k_flags = book_flags(k_book)
    p_flags = book_flags(p_book)
    for flag in k_flags:
        record_issue(db, now, "kalshi", k["ticker"], flag)
    for flag in p_flags:
        record_issue(db, now, "polymarket", p["id"], flag)
    flags = ", ".join(k_flags + p_flags)

    print(f"\n{k['ticker']} vs Polymarket {p['id']}  strike {p['strike']:,.0f}")
    print(f"  Settles on: {k['source']} vs {p['source']}")

    for direction, k_asks, p_asks in directions(k_book, p_book):
        if not k_asks or not p_asks:
            print(f"  {direction}: no asks on one side")
            continue
        fill = simulate_fill(k_asks, p_asks, p["fee_rate"], p["min_size"])
        net = net_per_contract(k_asks[0][0], p_asks[0][0], p["fee_rate"])
        contracts = fill["contracts"] if fill else 0
        db.execute(
            "INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, k["ticker"], p["id"], direction, k["source"], p["source"],
             k_asks[0][0], k_asks[0][1], p_asks[0][0], p_asks[0][1], net, contracts, leg_gap, flags),
        )
        print(f"  {direction}: asks {k_asks[0][0]} + {p_asks[0][0]},"
              f" net ${net:.4f} per contract, fillable {contracts}" + (f"  [{flags}]" if flags else ""))


def scan(db, now=None):
    now = now or datetime.now(timezone.utc).isoformat()
    kalshi_markets, kalshi_skipped = fetch_kalshi_markets()
    poly_markets, poly_skipped = fetch_poly_markets()
    skipped = kalshi_skipped + poly_skipped
    for platform, market_id, reason in skipped:
        record_issue(db, now, platform, market_id, reason)

    pairs = match_markets(kalshi_markets, poly_markets)
    seen = coverage(kalshi_markets, poly_markets)

    for k, p in pairs:
        try:
            scan_pair(db, now, k, p)
        except (requests.RequestException, KeyError, TypeError, ValueError) as error:
            record_issue(db, now, "pair", f"{k['ticker']}/{p['id']}", repr(error))
            print(f"\n{k['ticker']} vs Polymarket {p['id']}: book failed, {error}")

    db.execute("INSERT INTO scans VALUES (?, ?, ?, ?, ?, ?, ?)",
               (now, len(kalshi_markets), len(poly_markets), seen["shared_times"],
                len(pairs), seen["closest_gap_minutes"], len(skipped)))
    db.commit()

    gap = seen["closest_gap_minutes"]
    print(f"\n{now}")
    print(f"Open markets: Kalshi {len(kalshi_markets)}, Polymarket {len(poly_markets)}, skipped {len(skipped)}")
    print(f"Shared settlement times: {seen['shared_times']}, matched pairs: {len(pairs)}")
    print("Closest settlement gap at an equal strike:",
          f"{gap:.0f} minutes" if gap is not None else "no equal strikes")


def report(db):
    scans, with_pairs = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(matched_pairs > 0), 0) FROM scans").fetchone()
    print(f"Scans: {scans}  with a matched pair: {with_pairs}")

    quotes, positive, best = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(net_per_contract > 0), 0), MAX(net_per_contract)"
        " FROM quotes WHERE flags = ''").fetchone()
    print(f"Clean quotes: {quotes}  profitable after fees: {positive}")
    if best is not None:
        print(f"Best net per contract: ${best:.4f}")

    print("Data issues:")
    rows = db.execute("SELECT reason, COUNT(*) FROM issues GROUP BY reason ORDER BY 2 DESC").fetchall()
    for reason, count in rows:
        print(f"  {count}  {reason}")
    if not rows:
        print("  none")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help=f"scan every {SCAN_SECONDS} seconds")
    parser.add_argument("--report", action="store_true", help="summarize logged data and exit")
    args = parser.parse_args()

    db = open_db()
    if args.report:
        report(db)
        return

    while True:
        try:
            scan(db)
        except (requests.RequestException, KeyError, TypeError, ValueError) as error:
            db.rollback()
            print("Scan failed:", error)
        if not args.loop:
            return
        time.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    main()
