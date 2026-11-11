"""Scan game-winner markets that Kalshi and Polymarket both list, and log what covering a game costs.

Both sites settle on who wins the game, so unlike the Bitcoin pairs there is no gap between two
price sources. Holding one team on Kalshi and the other on Polymarket pays $1 whoever wins.
Never places an order.
"""
import argparse
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from main import MAX_SCAN_GAP_MINUTES, gap_runs
from market_data import GAMMA_URL, KALSHI_URL, fetch_poly_book, get_json, net_per_contract, parse_time

DB_PATH = "sports.db"
SCAN_SECONDS = 60
BOOK_WORKERS = 5
# (Polymarket's league name in its slugs, Kalshi's series of game-winner markets)
LEAGUES = (("nba", "KXNBAGAME"), ("nfl", "KXNFLGAME"), ("nhl", "KXNHLGAME"))
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
SLUGS_PER_REQUEST = 20
SCAN_ERRORS = (requests.RequestException, KeyError, IndexError, TypeError, ValueError, sqlite3.Error)

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (ts TEXT, kalshi_games INTEGER, matched INTEGER);
CREATE TABLE IF NOT EXISTS quotes (
    ts TEXT, league TEXT, game TEXT, kalshi_team TEXT, starts_at TEXT, in_play INTEGER,
    kalshi_ask REAL, kalshi_size REAL, poly_ask REAL, poly_size REAL, net_per_contract REAL
);
"""


def kalshi_games(league, series):
    """Return {Polymarket slug: (Kalshi market for the slug's first team, market for its second team)}.

    Kalshi names a game KXNBAGAME-26NOV11BOSTOR with one market per team (...-BOS and ...-TOR).
    Polymarket calls it nba-bos-tor-2026-11-11. The team order isn't known, so both orders are returned.
    """
    events = get_json(f"{KALSHI_URL}/events", series_ticker=series, status="open",
                      with_nested_markets="true", limit=200)["events"]
    games = {}
    for event in events:
        markets = event.get("markets") or []
        if len(markets) != 2:
            continue
        stamp = event["event_ticker"].split("-")[1]
        date = f"20{stamp[:2]}-{MONTHS.index(stamp[2:5]) + 1:02d}-{stamp[5:7]}"
        first, second = markets
        for a, b in ((first, second), (second, first)):
            codes = [market["ticker"].split("-")[-1].lower() for market in (a, b)]
            games[f"{league}-{codes[0]}-{codes[1]}-{date}"] = (a, b)
    return games, len(events)


def poly_games(slugs):
    """Return the open Polymarket game markets among slugs, as {slug: market}."""
    found = {}
    for start in range(0, len(slugs), SLUGS_PER_REQUEST):
        for market in get_json(f"{GAMMA_URL}/markets", slug=slugs[start:start + SLUGS_PER_REQUEST],
                               closed="false", limit=100):
            found[market["slug"]] = market
    return found


def kalshi_ask(team_market, other_market):
    """Cheapest way to back a team on Kalshi: YES on its market, or NO on the other team's market.

    Returns (price, size), or None when neither is quoted.
    """
    offers = [(float(team_market["yes_ask_dollars"]), float(team_market["yes_ask_size_fp"])),
              # A NO ask is the other side of a YES bid, so it has that bid's size.
              (float(other_market["no_ask_dollars"]), float(other_market["yes_bid_size_fp"]))]
    offers = [offer for offer in offers if 0 < offer[0] < 1 and offer[1] > 0]
    # At the same price, take the offer with more size.
    return min(offers, key=lambda offer: (offer[0], -offer[1])) if offers else None


def covers(kalshi_pair, poly_book, poly_fee_rate):
    """Price both ways to hold every outcome: one team on Kalshi, the other team on Polymarket.

    kalshi_pair is Kalshi's two markets in the order of Polymarket's outcomes. poly_book is the
    book of Polymarket's first outcome, so its NO side is the second team.
    Returns [(Kalshi team code, kalshi ask, kalshi size, poly ask, poly size, net per contract)].
    """
    priced = []
    for index, poly_asks in ((0, poly_book["no_asks"]), (1, poly_book["yes_asks"])):
        kalshi = kalshi_ask(kalshi_pair[index], kalshi_pair[1 - index])
        if kalshi is None or not poly_asks:
            continue
        poly_price, poly_size = poly_asks[0]
        priced.append((kalshi_pair[index]["ticker"].split("-")[-1], kalshi[0], kalshi[1], poly_price, poly_size,
                       net_per_contract(kalshi[0], poly_price, poly_fee_rate)))
    return priced


def game_start(market):
    """Polymarket writes the start as "2026-11-15 18:00:00+00"; endDate holds the same time in ISO form."""
    text = market.get("gameStartTime")
    if not text:
        return parse_time(market["endDate"])
    return parse_time(text.replace(" ", "T") + (":00" if text.endswith("+00") else ""))


def scan(db, now=None):
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()
    kalshi, kalshi_count = {}, 0
    for league, series in LEAGUES:
        games, count = kalshi_games(league, series)
        kalshi.update(games)
        kalshi_count += count
    poly = poly_games(list(kalshi))

    # Kalshi's prices came with its market list, so only Polymarket's books are still to fetch.
    with ThreadPoolExecutor(max_workers=BOOK_WORKERS) as pool:
        books = [pool.submit(fetch_poly_book, json.loads(market["clobTokenIds"])[0]) for market in poly.values()]
    best = None
    for (slug, market), book in zip(poly.items(), books):
        try:
            priced = covers(kalshi[slug], book.result(), (market.get("feeSchedule") or {}).get("rate", 0))
        except SCAN_ERRORS as error:
            print(f"{slug}: skipped, {error}")
            continue
        starts_at = game_start(market)
        for team, k_ask, k_size, p_ask, p_size, net in priced:
            db.execute("INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (stamp, slug.split("-")[0], slug, team, starts_at.isoformat(), int(now >= starts_at),
                        k_ask, k_size, p_ask, p_size, net))
            if best is None or net > best[0]:
                best = (net, slug, team, k_ask, p_ask)
    db.execute("INSERT INTO scans VALUES (?, ?, ?)", (stamp, kalshi_count, len(poly)))
    db.commit()
    print(f"{stamp[:19]}  Kalshi games {kalshi_count}, matched on Polymarket {len(poly)}", end="")
    if best:
        print(f", best cover {best[1]} (Kalshi {best[2]} {best[3]:.2f} + Polymarket {best[4]:.3f}): ${best[0]:+.4f}")
    else:
        print()


def report(db):
    scans, first, last = db.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM scans").fetchone()
    if not scans:
        print("No scans yet")
        return
    games = db.execute("SELECT COUNT(DISTINCT game) FROM quotes").fetchone()[0]
    print(f"Scans: {scans}, {first[:16]} to {last[:16]}  games seen: {games}")
    for label, in_play in (("before the game", 0), ("during the game", 1)):
        quotes, positive, positive_games, best, typical = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(net_per_contract > 0), 0),"
            " COUNT(DISTINCT CASE WHEN net_per_contract > 0 THEN game END), MAX(net_per_contract),"
            " AVG(net_per_contract) FROM quotes WHERE in_play = ?", (in_play,)).fetchone()
        if not quotes:
            print(f"Quotes {label}: 0")
            continue
        print(f"Quotes {label}: {quotes}  average ${typical:+.4f}  profitable after fees: {positive}"
              f" in {positive_games} games  best ${best:+.4f}")

    points, sizes = {}, []
    for game, team, ts, net, k_size, p_size in db.execute(
            "SELECT game, kalshi_team, ts, net_per_contract, kalshi_size, poly_size FROM quotes ORDER BY ts"):
        points.setdefault((game, team), []).append((parse_time(ts), net))
        if net > 0:
            sizes.append(min(k_size, p_size) * net)
    durations = sorted((end - start).total_seconds() / 60
                       for trade_points in points.values() for start, end in gap_runs(trade_points))
    if durations:
        sizes.sort()
        print(f"Profitable gaps: {len(durations)}  median open {durations[len(durations) // 2]:.0f} min,"
              f" longest {durations[-1]:.0f} min (a gap seen in one scan counts as 0;"
              f" over {MAX_SCAN_GAP_MINUTES} min between scans ends a gap)")
        print(f"Dollars available per profitable quote at the best price: median ${sizes[len(sizes) // 2]:.2f},"
              f" largest ${sizes[-1]:.2f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help=f"scan every {SCAN_SECONDS} seconds")
    parser.add_argument("--report", action="store_true", help="summarize logged data and exit")
    args = parser.parse_args()

    db = sqlite3.connect(DB_PATH, timeout=30)
    db.executescript(SCHEMA)
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
