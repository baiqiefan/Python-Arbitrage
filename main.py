"""Compare Kalshi and Polymarket Bitcoin order books and log the results."""
import argparse
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from market_data import (
    book_flags,
    brti_proxy,
    coverage,
    directions,
    fetch_kalshi_book,
    fetch_kalshi_markets,
    fetch_poly_book,
    fetch_poly_markets,
    fetch_spot_prices,
    in_danger_zone,
    kalshi_settlement,
    legs_won,
    match_markets,
    net_per_contract,
    parse_time,
    poly_result,
    simulate_fill,
)

DB_PATH = "market_data.db"
SCAN_SECONDS = 60
# Polymarket can take an hour or more to resolve, so don't ask about every pair every minute.
SETTLE_CHECK_MINUTES = 5
# Scans run about a minute apart; a longer silence means the scanner or network was down.
MAX_SCAN_GAP_MINUTES = 3
# Pairs whose order books are fetched at the same time. Kept low to stay under the sites' rate limits.
BOOK_WORKERS = 5
# The reverse trade buys Polymarket-YES + Kalshi-NO. Binance (Polymarket's price) runs about $100
# above BRTI (Kalshi's), so this trade can win both legs when the strike ends between them.
# The rule was picked from data up to REVERSE_RULE_CHOSEN, so only later data is a fair test of it.
REVERSE_MIN_NET = -0.10
REVERSE_STRIKE_BELOW_BINANCE = (100, 400)
REVERSE_RULE_CHOSEN = "2026-11-11"
SCAN_ERRORS = (requests.RequestException, KeyError, TypeError, ValueError, sqlite3.Error)

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    ts TEXT, kalshi_open INTEGER, poly_open INTEGER, shared_times INTEGER,
    matched_pairs INTEGER, closest_gap_minutes REAL, skipped INTEGER
);
CREATE TABLE IF NOT EXISTS quotes (
    ts TEXT, kalshi_ticker TEXT, poly_id TEXT, direction TEXT,
    kalshi_source TEXT, poly_source TEXT,
    kalshi_ask REAL, kalshi_size REAL, poly_ask REAL, poly_size REAL,
    net_per_contract REAL, fillable_contracts INTEGER, leg_gap_seconds REAL, flags TEXT,
    minutes_left REAL
);
CREATE TABLE IF NOT EXISTS issues (
    ts TEXT, platform TEXT, market_id TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS prices (
    ts TEXT, binance REAL, coinbase REAL, kraken REAL, bitstamp REAL
);
CREATE TABLE IF NOT EXISTS settlements (
    kalshi_ticker TEXT, poly_id TEXT, strike REAL, settles_at TEXT,
    kalshi_source TEXT, poly_source TEXT,
    kalshi_result TEXT, kalshi_value REAL, poly_result TEXT, checked_at TEXT,
    PRIMARY KEY (kalshi_ticker, poly_id)
);
"""


def open_db(path=DB_PATH):
    # Wait for another writer instead of failing after the default 5 seconds.
    db = sqlite3.connect(path, timeout=30)
    db.executescript(SCHEMA)
    # CREATE TABLE IF NOT EXISTS leaves older databases alone, so add newer columns here.
    columns = {row[1] for row in db.execute("PRAGMA table_info(quotes)")}
    if "minutes_left" not in columns:
        db.execute("ALTER TABLE quotes ADD COLUMN minutes_left REAL")
    columns = {row[1] for row in db.execute("PRAGMA table_info(prices)")}
    for name in ("kraken", "bitstamp"):
        if name not in columns:
            db.execute(f"ALTER TABLE prices ADD COLUMN {name} REAL")
    return db


def record_issue(db, now, platform, market_id, reason):
    db.execute("INSERT INTO issues VALUES (?, ?, ?, ?)", (now, platform, market_id, reason))


def different_sources(k, p):
    return k["source"] != p["source"]


QUOTE_COLUMNS = (
    "ts, kalshi_ticker, poly_id, direction, kalshi_source, poly_source, kalshi_ask, kalshi_size,"
    " poly_ask, poly_size, net_per_contract, fillable_contracts, leg_gap_seconds, flags, minutes_left"
)


def fetch_books(k, p):
    return fetch_kalshi_book(k["ticker"]), fetch_poly_book(p["yes_token"])


def scan_pair(db, now, k, p, k_book, p_book):
    minutes_left = (p["settles_at"] - now).total_seconds() / 60
    now = now.isoformat()
    leg_gap = abs((p_book["fetched_at"] - k_book["fetched_at"]).total_seconds())

    k_flags = book_flags(k_book)
    p_flags = book_flags(p_book)
    for flag in k_flags:
        record_issue(db, now, "kalshi", k["ticker"], flag)
    for flag in p_flags:
        record_issue(db, now, "polymarket", p["id"], flag)
    flags = ", ".join(k_flags + p_flags)

    db.execute(
        "INSERT OR IGNORE INTO settlements (kalshi_ticker, poly_id, strike, settles_at, kalshi_source, poly_source)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (k["ticker"], p["id"], p["strike"], p["settles_at"].isoformat(), k["source"], p["source"]),
    )

    print(f"\n{k['ticker']} vs Polymarket {p['id']}  strike {p['strike']:,.0f}")
    print(f"  Settles on: {k['source']} vs {p['source']}"
          + ("  (different prices, so not risk-free)" if different_sources(k, p) else ""))

    for direction, k_asks, p_asks in directions(k_book, p_book):
        if not k_asks or not p_asks:
            # Logged with no prices, so the report can count untradeable pairs.
            db.execute(
                f"INSERT INTO quotes ({QUOTE_COLUMNS})"
                " VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, 0, ?, ?, ?)",
                (now, k["ticker"], p["id"], direction, k["source"], p["source"], leg_gap, flags, minutes_left),
            )
            print(f"  {direction}: no asks on one side")
            continue
        fill = simulate_fill(k_asks, p_asks, p["fee_rate"], p["min_size"])
        net = net_per_contract(k_asks[0][0], p_asks[0][0], p["fee_rate"])
        contracts = fill["contracts"] if fill else 0
        db.execute(
            f"INSERT INTO quotes ({QUOTE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, k["ticker"], p["id"], direction, k["source"], p["source"],
             k_asks[0][0], k_asks[0][1], p_asks[0][0], p_asks[0][1], net, contracts, leg_gap, flags,
             minutes_left),
        )
        print(f"  {direction}: asks {k_asks[0][0]} + {p_asks[0][0]},"
              f" net ${net:.4f} per contract, fillable {contracts}" + (f"  [{flags}]" if flags else ""))


def settle_pairs(db, now):
    """Record both platforms' results for matched pairs whose settlement time has passed."""
    retry_before = (now - timedelta(minutes=SETTLE_CHECK_MINUTES)).isoformat()
    rows = db.execute(
        "SELECT kalshi_ticker, poly_id FROM settlements WHERE poly_result IS NULL AND settles_at <= ?"
        " AND (checked_at IS NULL OR checked_at <= ?)",
        (now.isoformat(), retry_before),
    ).fetchall()
    for ticker, poly_id in rows:
        try:
            k_result, k_value = kalshi_settlement(ticker)
            p_result = poly_result(poly_id) if k_result else None
        except requests.ConnectionError as error:
            # The network is down, so every other lookup would fail too. Try again next scan.
            record_issue(db, now.isoformat(), "settlement", f"{ticker}/{poly_id}", repr(error))
            break
        except (requests.RequestException, KeyError, TypeError, ValueError) as error:
            record_issue(db, now.isoformat(), "settlement", f"{ticker}/{poly_id}", repr(error))
            # Wait the usual interval before retrying, instead of failing every scan.
            db.execute("UPDATE settlements SET checked_at = ? WHERE kalshi_ticker = ? AND poly_id = ?",
                       (now.isoformat(), ticker, poly_id))
            continue
        db.execute(
            "UPDATE settlements SET kalshi_result = ?, kalshi_value = ?, poly_result = ?, checked_at = ?"
            " WHERE kalshi_ticker = ? AND poly_id = ?",
            (k_result, k_value, p_result, now.isoformat(), ticker, poly_id),
        )
        if p_result:
            print(f"Settled {ticker} vs Polymarket {poly_id}: Kalshi {k_result}, Polymarket {p_result}")
    db.commit()


def scan(db, now=None):
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()
    settle_pairs(db, now)

    kalshi_markets, kalshi_skipped = fetch_kalshi_markets()
    poly_markets, poly_skipped = fetch_poly_markets()
    skipped = kalshi_skipped + poly_skipped
    for platform, market_id, reason in skipped:
        record_issue(db, stamp, platform, market_id, reason)

    pairs = match_markets(kalshi_markets, poly_markets)
    seen = coverage(kalshi_markets, poly_markets)
    spot = fetch_spot_prices()
    db.execute("INSERT INTO prices (ts, binance, coinbase, kraken, bitstamp) VALUES (?, ?, ?, ?, ?)",
               (stamp, spot["binance"], spot["coinbase"], spot["kraken"], spot["bitstamp"]))

    # Only the web requests run in threads; the database is written from this thread alone.
    with ThreadPoolExecutor(max_workers=BOOK_WORKERS) as pool:
        books = [pool.submit(fetch_books, k, p) for k, p in pairs]
    for (k, p), fetched in zip(pairs, books):
        try:
            scan_pair(db, now, k, p, *fetched.result())
        except (requests.RequestException, KeyError, TypeError, ValueError) as error:
            record_issue(db, stamp, "pair", f"{k['ticker']}/{p['id']}", repr(error))
            print(f"\n{k['ticker']} vs Polymarket {p['id']}: book failed, {error}")

    db.execute("INSERT INTO scans VALUES (?, ?, ?, ?, ?, ?, ?)",
               (stamp, len(kalshi_markets), len(poly_markets), seen["shared_times"],
                len(pairs), seen["closest_gap_minutes"], len(skipped)))
    db.commit()

    gap = seen["closest_gap_minutes"]
    print(f"\n{stamp}")
    print(f"Open markets: Kalshi {len(kalshi_markets)}, Polymarket {len(poly_markets)}, skipped {len(skipped)}")
    print(f"Shared settlement times: {seen['shared_times']}, matched pairs: {len(pairs)}")
    print("Closest settlement gap at an equal strike:",
          f"{gap:.0f} minutes" if gap is not None else "no equal strikes")
    proxy = brti_proxy(spot)
    if spot["binance"] is not None and proxy is not None:
        print(f"BTC: Binance ${spot['binance']:,.2f}, BRTI stand-in ${proxy:,.2f},"
              f" gap ${spot['binance'] - proxy:+,.2f}")


def one_quote_per_trade(rows):
    """Keep one row per (kalshi_ticker, poly_id, direction), so a gap seen every minute counts once.

    Rows are (kalshi_ticker, poly_id, direction, ts, net_per_contract, kalshi_result, poly_result),
    oldest first.
    """
    trades = {}
    for row in rows:
        trades.setdefault(row[:3], row)
    
    return list(trades.values())

def trade_result(direction, net, kalshi_result, poly_result):
    """(legs won, dollars per contract). Cost including fees is 1 - net, and each winning leg pays $1."""
    won = legs_won(direction, kalshi_result, poly_result)
    return won, won - 1 + net


def split_by_danger_zone(rows):
    """Split settled trades into ones a danger-zone rule would take and ones it would skip.

    Rows are (kalshi_ticker, poly_id, direction, ts, net, kalshi_result, poly_result,
    strike, minutes_left, binance, coinbase, kraken, bitstamp), oldest first, profitable quotes only.
    A trade is taken at its first quote outside the zone. A trade whose quotes were all inside is
    skipped, and judged by its first quote. Quotes without a BRTI stand-in are left out.
    """
    safe, risky = [], []
    for row in rows:
        strike, minutes_left, binance = row[7:10]
        brti = brti_proxy(dict(zip(("coinbase", "kraken", "bitstamp"), row[10:13])))
        if brti is None:
            continue
        (risky if in_danger_zone(strike, binance, brti, minutes_left) else safe).append(row[:7])
    taken = one_quote_per_trade(safe)
    taken_keys = {row[:3] for row in taken}
    skipped = [row for row in one_quote_per_trade(risky) if row[:3] not in taken_keys]
    return taken, skipped


def reverse_trades(rows):
    """Settled trades the reverse rule would have taken, one per pair, at its first matching quote.

    Rows are (kalshi_ticker, poly_id, direction, ts, net, kalshi_result, poly_result, strike, binance),
    oldest first, for the poly_yes_kalshi_no direction.
    """
    low, high = REVERSE_STRIKE_BELOW_BINANCE
    return one_quote_per_trade(
        row[:7] for row in rows if row[4] > REVERSE_MIN_NET and low <= row[8] - row[7] < high)


def gap_runs(points):
    """Find each stretch of back-to-back profitable scans for one trade.

    points is [(datetime, net_per_contract or None)] oldest first. A run ends at a scan that
    isn't profitable (net <= 0 or None), or when scans are over MAX_SCAN_GAP_MINUTES apart.
    Returns [(first_time, last_time)] for each run.
    """
    runs = []
    start = end = None
    for when, net in points:
        if net is not None and net > 0:
            if start is not None and (when - end).total_seconds() / 60 <= MAX_SCAN_GAP_MINUTES:
                end = when
            else:
                if start is not None:
                    runs.append((start, end))
                start = end = when
        elif start is not None:
                runs.append((start, end))
                start = end = None
    if start is not None:
                runs.append((start, end))
    return runs
    


def gap_durations(db):
    """Minutes each profitable gap stayed open. A gap seen in only one scan lasted 0 minutes."""
    points = {}
    for ticker, poly_id, direction, ts, net in db.execute(
            "SELECT kalshi_ticker, poly_id, direction, ts, CASE WHEN flags = '' THEN net_per_contract END"
            " FROM quotes ORDER BY ts"):
        points.setdefault((ticker, poly_id, direction), []).append((parse_time(ts), net))
    return [
        (end - start).total_seconds() / 60
        for trade_points in points.values()
        for start, end in gap_runs(trade_points)
    ]


def report(db):
    scans, with_pairs = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(matched_pairs > 0), 0) FROM scans").fetchone()
    print(f"Scans: {scans}  with a matched pair: {with_pairs}")

    quotes, tradeable, positive, risky, best = db.execute(
        "SELECT COUNT(*),"
        " COALESCE(SUM(kalshi_ask IS NOT NULL AND flags = ''), 0),"
        " COALESCE(SUM(net_per_contract > 0 AND flags = ''), 0),"
        " COALESCE(SUM(net_per_contract > 0 AND flags = '' AND kalshi_source != poly_source), 0),"
        " MAX(CASE WHEN flags = '' THEN net_per_contract END)"
        " FROM quotes").fetchone()
    print(f"Quotes: {quotes}  tradeable: {tradeable}  profitable after fees: {positive}")
    print(f"  of those, settling on different prices (not risk-free): {risky}")
    if best is not None:
        print(f"Best net per contract: ${best:.4f}")

    tracked, settled, split = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(poly_result IS NOT NULL), 0),"
        " COALESCE(SUM(poly_result IS NOT NULL AND kalshi_result != poly_result), 0)"
        " FROM settlements").fetchone()
    print(f"Matched pairs tracked: {tracked}  settled: {settled}  where the platforms disagreed: {split}")

    # Test each profitable quote against what really happened.
    rows = db.execute(
        "SELECT q.kalshi_ticker, q.poly_id, q.direction, q.ts, q.net_per_contract, s.kalshi_result, s.poly_result"
        " FROM quotes q JOIN settlements s ON q.kalshi_ticker = s.kalshi_ticker AND q.poly_id = s.poly_id"
        " WHERE q.net_per_contract > 0 AND q.flags = '' AND s.poly_result IS NOT NULL ORDER BY q.ts").fetchall()
    outcomes = one_quote_per_trade(rows)
    if outcomes:
        legs, results = zip(*(trade_result(row[2], row[4], row[5], row[6]) for row in outcomes))
        print(f"Profitable quotes now settled: {len(outcomes)}  paid once: {legs.count(1)}"
              f"  lost both legs: {legs.count(0)}  won both: {legs.count(2)}")
        print(f"Average real result per contract: ${sum(results) / len(results):.4f}")

    zone_rows = db.execute(
        "SELECT q.kalshi_ticker, q.poly_id, q.direction, q.ts, q.net_per_contract, s.kalshi_result, s.poly_result,"
        " s.strike, q.minutes_left, p.binance, p.coinbase, p.kraken, p.bitstamp"
        " FROM quotes q JOIN settlements s ON q.kalshi_ticker = s.kalshi_ticker AND q.poly_id = s.poly_id"
        " JOIN prices p ON p.ts = q.ts"
        " WHERE q.net_per_contract > 0 AND q.flags = '' AND s.poly_result IS NOT NULL"
        " AND p.binance IS NOT NULL ORDER BY q.ts").fetchall()
    taken, skipped = split_by_danger_zone(zone_rows)
    if taken or skipped:
        print("Danger zone check (settled trades with a BRTI stand-in):")
        for label, group in (("outside zone, would take", taken), ("inside zone, would skip", skipped)):
            if not group:
                print(f"  {label}: 0")
                continue
            legs, results = zip(*(trade_result(row[2], row[4], row[5], row[6]) for row in group))
            print(f"  {label}: {len(group)}  paid once: {legs.count(1)}  lost both legs: {legs.count(0)}"
                  f"  avg ${sum(results) / len(results):+.4f}/contract")

    reverse_rows = db.execute(
        "SELECT q.kalshi_ticker, q.poly_id, q.direction, q.ts, q.net_per_contract, s.kalshi_result, s.poly_result,"
        " s.strike, p.binance"
        " FROM quotes q JOIN settlements s ON q.kalshi_ticker = s.kalshi_ticker AND q.poly_id = s.poly_id"
        " JOIN prices p ON p.ts = q.ts"
        " WHERE q.direction = 'poly_yes_kalshi_no' AND q.net_per_contract IS NOT NULL AND q.flags = ''"
        " AND s.poly_result IS NOT NULL AND p.binance IS NOT NULL ORDER BY q.ts").fetchall()
    reverse = reverse_trades(reverse_rows)
    if reverse:
        print(f"Reverse trade test (Polymarket-YES + Kalshi-NO, strike ${REVERSE_STRIKE_BELOW_BINANCE[0]}"
              f"-{REVERSE_STRIKE_BELOW_BINANCE[1]} below Binance):")
        for label, group in (
                ("data the rule was picked from", [row for row in reverse if row[3] < REVERSE_RULE_CHOSEN]),
                ("new data, the fair test", [row for row in reverse if row[3] >= REVERSE_RULE_CHOSEN])):
            if not group:
                print(f"  {label}: 0")
                continue
            legs, results = zip(*(trade_result(row[2], row[4], row[5], row[6]) for row in group))
            print(f"  {label}: {len(group)}  won both: {legs.count(2)}  lost both: {legs.count(0)}"
                  f"  avg ${sum(results) / len(results):+.4f}/contract")

    durations = sorted(gap_durations(db))
    if durations:
        median = durations[len(durations) // 2]
        lasting = sum(minutes >= 5 for minutes in durations)
        print(f"Profitable gaps: {len(durations)}  median open {median:.0f} min,"
              f" open 5+ min: {lasting}, longest {durations[-1]:.0f} min")

    count, average, low, high = db.execute(
        "SELECT COUNT(*), AVG(binance - coinbase), MIN(binance - coinbase), MAX(binance - coinbase)"
        " FROM prices WHERE binance IS NOT NULL AND coinbase IS NOT NULL").fetchone()
    if count:
        print(f"Binance minus Coinbase over {count} scans: average ${average:+,.2f},"
              f" range ${low:+,.2f} to ${high:+,.2f}")

    gaps = []
    for binance, coinbase, kraken, bitstamp in db.execute(
            "SELECT binance, coinbase, kraken, bitstamp FROM prices WHERE binance IS NOT NULL"):
        proxy = brti_proxy({"coinbase": coinbase, "kraken": kraken, "bitstamp": bitstamp})
        if proxy is not None:
            gaps.append(binance - proxy)
    if gaps:
        print(f"Binance minus BRTI stand-in over {len(gaps)} scans: average ${sum(gaps) / len(gaps):+,.2f},"
              f" range ${min(gaps):+,.2f} to ${max(gaps):+,.2f}")

    print("Data issues:")
    # Error messages include the URL, so group by the error's name, e.g. "ConnectionError".
    groups = {}
    for platform, reason, ts in db.execute("SELECT platform, reason, ts FROM issues ORDER BY ts"):
        key = (platform, reason.split("(")[0])
        count, first, _ = groups.get(key, (0, ts, ts))
        groups[key] = (count + 1, first, ts)
    for (platform, kind), (count, first, last) in sorted(groups.items(), key=lambda item: -item[1][0]):
        print(f"  {count}  {kind} ({platform}), {first[:16]} to {last[:16]}")
    if not groups:
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
        except SCAN_ERRORS as error:
            db.rollback()
            print("Scan failed, will retry:", error)
        if not args.loop:
            return
        time.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    main()
