"""Test the reverse trade (Polymarket-YES + Kalshi-NO) on hours that have already settled.

Downloads about two months of history once into backtest.db, then replays main.py's reverse rule
minute by minute. It uses quoted prices, so it cannot tell whether both orders would have filled.

Read the averages as too high by about $0.08 per contract. The rule enters at the first minute whose
estimated price qualifies, which favours minutes where the Polymarket midpoint understates the real
ask. On the live scanner's own quotes (2026-11-06 to 11-09) the estimate said $+0.107 per contract
where the real asks gave $+0.025.
"""
import argparse
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from main import REVERSE_MIN_NET, REVERSE_RULE_CHOSEN, REVERSE_STRIKE_BELOW_BINANCE
from market_data import CLOB_URL, GAMMA_URL, KALSHI_URL, get_json, legs_won, net_per_contract

DB_PATH = "backtest.db"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
# Kept low: Kalshi answers 429 (too many requests) when pushed, and the live scanner shares its limit.
HOUR_WORKERS = 3
# Polymarket's history gives the midpoint of bid and ask. Against the live scanner's quotes (--check)
# the real ask was a median $0.023 higher, and $0.04 higher at the 75th percentile.
POLY_HALF_SPREAD = 0.025
POLY_HALF_SPREAD_WIDE = 0.04
# A midpoint this close to 0.50 comes from an empty book (bid 0.01, ask 0.99), not a real price.
EMPTY_BOOK_BAND = 0.006
# The reverse rule was picked from live data starting on this date; earlier history never influenced it.
RULE_DATA_STARTS = "2026-11-06"
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december")

SCHEMA = """
CREATE TABLE IF NOT EXISTS binance (minute INTEGER PRIMARY KEY, close REAL);
CREATE TABLE IF NOT EXISTS hours (settles INTEGER PRIMARY KEY, brti REAL);
CREATE TABLE IF NOT EXISTS legs (
    settles INTEGER, strike INTEGER, kalshi_ticker TEXT, kalshi_result TEXT, poly_fee REAL,
    PRIMARY KEY (settles, strike)
);
CREATE TABLE IF NOT EXISTS quotes (
    settles INTEGER, strike INTEGER, minute INTEGER, kalshi_no_ask REAL, poly_mid REAL,
    PRIMARY KEY (settles, strike, minute)
);
"""


def fetch(url, **params):
    """get_json with retries, because a long download always hits a few timeouts or rate limits."""
    for attempt in range(7):
        try:
            return get_json(url, **params)
        except requests.RequestException:
            if attempt == 6:
                raise
            time.sleep(2 ** attempt)


def poly_slug(kalshi_ticker, strike):
    """Polymarket's name for the hourly market that matches a Kalshi ticker.

    KXBTCD-26NOV0604-T75599.99 settles on 2026-11-06 at 4am ET, which Polymarket calls
    bitcoin-above-75600-on-november-6-2026-4am-et.
    """
    stamp = kalshi_ticker.split("-")[1]
    month = MONTHS[[name[:3].upper() for name in MONTHS].index(stamp[2:5])]
    hour = int(stamp[7:9])
    return (f"bitcoin-above-{strike}-on-{month}-{int(stamp[5:7])}-20{stamp[:2]}"
            f"-{hour % 12 or 12}{'am' if hour < 12 else 'pm'}-et")


def download_binance(db, start, end):
    """Store the close of every 1-minute candle from start to end (epoch seconds)."""
    oldest, latest = db.execute("SELECT MIN(minute), MAX(minute) FROM binance").fetchone()
    # Fetch only what's missing: the stretch before the saved candles and the stretch after them.
    spans = [(start, end)] if oldest is None else [(start, oldest), (latest + 60, end)]
    for start, end in spans:
        while start < end:
            candles = fetch(BINANCE_KLINES_URL, symbol="BTCUSDT", interval="1m", startTime=start * 1000, limit=1000)
            if not candles:
                break
            db.executemany("INSERT OR REPLACE INTO binance VALUES (?, ?)",
                           [(candle[0] // 1000, float(candle[4])) for candle in candles])
            start = candles[-1][0] // 1000 + 60
    db.commit()


def last_known(points, minute):
    """The latest value at or before minute, from [(time, value)] sorted oldest first."""
    value = None
    for when, price in points:
        if when > minute:
            break
        value = price
    return value


def download_hour(settles, binance):
    """Fetch one settled hour. binance is {minute: close} for that hour.

    Returns (brti, legs rows, quotes rows); brti is None when Kalshi has no markets for the hour.
    """
    if not binance:
        raise ValueError(f"no Binance prices saved for the hour ending {settles}")
    markets = fetch(f"{KALSHI_URL}/markets", series_ticker="KXBTCD", status="settled",
                    min_close_ts=settles - 1, max_close_ts=settles + 1, limit=1000).get("markets", [])
    if not markets:
        return None, [], []
    brti = float(markets[0]["expiration_value"])
    # Only strikes the rule could ever pick: within its distance of Binance at some minute.
    near, far = REVERSE_STRIKE_BELOW_BINANCE
    kalshi = {round(market["floor_strike"]): market for market in markets
              if min(binance.values()) - far < market["floor_strike"] <= max(binance.values()) - near}
    if not kalshi:
        return brti, [], []

    slugs = {poly_slug(market["ticker"], strike): strike for strike, market in kalshi.items()}
    poly = {slugs[market["slug"]]: market
            for market in fetch(f"{GAMMA_URL}/markets", slug=list(slugs), closed="true", limit=100)
            if market["slug"] in slugs}
    if not poly:
        return brti, [], []

    tickers = {kalshi[strike]["ticker"]: strike for strike in poly}
    candles = fetch(f"{KALSHI_URL}/markets/candlesticks", market_tickers=",".join(tickers),
                    start_ts=settles - 3600, end_ts=settles, period_interval=1)
    no_asks = {}
    for market in candles.get("markets", []):
        points = [(candle["end_period_ts"], 1 - float(candle["yes_bid"]["close_dollars"]))
                  for candle in market.get("candlesticks", []) if (candle.get("yes_bid") or {}).get("close_dollars")]
        no_asks[tickers[market["market_ticker"]]] = sorted(points)

    legs, quotes = [], []
    for strike, market in poly.items():
        history = fetch(f"{CLOB_URL}/prices-history", market=json.loads(market["clobTokenIds"])[0],
                        startTs=settles - 3600, endTs=settles, fidelity=1).get("history", [])
        mids = sorted((point["t"], point["p"]) for point in history)
        legs.append((settles, strike, kalshi[strike]["ticker"], kalshi[strike]["result"],
                     (market.get("feeSchedule") or {}).get("rate", 0)))
        for minute in range(settles - 3540, settles, 60):
            no_ask, mid = last_known(no_asks.get(strike, []), minute), last_known(mids, minute)
            if no_ask is not None and mid is not None:
                quotes.append((settles, strike, minute, no_ask, mid))
    return brti, legs, quotes


def download(db, days):
    end = int(time.time()) // 3600 * 3600 - 3 * 3600  # leave time for both sites to publish results
    start = end - days * 86400
    download_binance(db, start - 3600, end)
    done = {row[0] for row in db.execute("SELECT settles FROM hours")}
    todo = [settles for settles in range(start, end + 1, 3600) if settles not in done]
    print(f"Downloading {len(todo)} hours ({len(done)} already saved)")

    # A database connection can't be shared between threads, so the prices are read once, here.
    prices = dict(db.execute("SELECT minute, close FROM binance WHERE minute >= ?", (start - 3600,)))

    def work(settles):
        binance = {minute: prices[minute] for minute in range(settles - 3600, settles, 60) if minute in prices}
        try:
            return settles, download_hour(settles, binance)
        except requests.RequestException as error:
            print(f"  skipped one hour, run again to retry it: {error}")
            return settles, None

    with ThreadPoolExecutor(max_workers=HOUR_WORKERS) as pool:
        for count, (settles, result) in enumerate(pool.map(work, todo), 1):
            if result is None:
                continue
            brti, legs, quotes = result
            db.execute("INSERT OR REPLACE INTO hours VALUES (?, ?)", (settles, brti))
            db.executemany("INSERT OR REPLACE INTO legs VALUES (?, ?, ?, ?, ?)", legs)
            db.executemany("INSERT OR REPLACE INTO quotes VALUES (?, ?, ?, ?, ?)", quotes)
            if count % 24 == 0:
                db.commit()
                print(f"  {count}/{len(todo)} hours")
    db.commit()


def reverse_backtest(rows, half_spread=POLY_HALF_SPREAD):
    """Replay the reverse rule: one trade per market, at the first minute that qualifies.

    Rows are (settles, strike, kalshi_no_ask, poly_mid, binance, poly_fee, kalshi_result, binance_close),
    oldest first. Returns [(settles, strike, legs won, dollars per contract)].
    """
    near, far = REVERSE_STRIKE_BELOW_BINANCE
    trades = {}
    for settles, strike, no_ask, mid, binance, poly_fee, kalshi_result, binance_close in rows:
        if (settles, strike) in trades or abs(mid - 0.5) < EMPTY_BOOK_BAND:
            continue
        yes_ask = mid + half_spread
        net = net_per_contract(no_ask, yes_ask, poly_fee)
        if yes_ask < 1 and net > REVERSE_MIN_NET and near <= binance - strike < far:
            won = legs_won("poly_yes_kalshi_no", kalshi_result, "yes" if binance_close > strike else "no")
            trades[settles, strike] = (settles, strike, won, won - 1 + net)
    return list(trades.values())


def summarize(trades):
    """Average result with a margin of error that treats each day, not each trade, as one sample."""
    results = [trade[3] for trade in trades]
    mean = sum(results) / len(results)
    days = {}
    for settles, _, _, result in trades:
        days.setdefault(settles // 86400, []).append(result - mean)
    error = sum(sum(day) ** 2 for day in days.values()) ** 0.5 / len(results)
    won = [trade[2] for trade in trades]
    return (f"{len(trades)} trades over {len(days)} days  won both: {won.count(2)} ({won.count(2) / len(won):.0%})"
            f"  lost both: {won.count(0)}  avg ${mean:+.4f} +/- {error:.4f} per contract")


def quote_rows(db):
    return db.execute(
        "SELECT q.settles, q.strike, q.kalshi_no_ask, q.poly_mid, b.close, l.poly_fee, l.kalshi_result, c.close"
        " FROM quotes q JOIN legs l ON l.settles = q.settles AND l.strike = q.strike"
        " JOIN binance b ON b.minute = q.minute - 60 JOIN binance c ON c.minute = q.settles - 60"
        " WHERE l.kalshi_result IN ('yes', 'no') ORDER BY q.minute").fetchall()


def report(db):
    rows = quote_rows(db)
    first, last, hours = db.execute("SELECT MIN(settles), MAX(settles), COUNT(brti) FROM hours").fetchone()
    day = lambda seconds: datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d")
    print(f"History: {day(first)} to {day(last)}, {hours} settled hours")
    cutoffs = [datetime.fromisoformat(text + "T00:00:00+00:00").timestamp()
               for text in (RULE_DATA_STARTS, REVERSE_RULE_CHOSEN)]
    for spread in (POLY_HALF_SPREAD, POLY_HALF_SPREAD_WIDE):
        trades = reverse_backtest(rows, spread)
        print(f"Polymarket ask = midpoint + ${spread:.3f}:")
        for label, group in (
                (f"before {RULE_DATA_STARTS}, the fair test", [t for t in trades if t[0] < cutoffs[0]]),
                ("days the rule was picked from", [t for t in trades if cutoffs[0] <= t[0] < cutoffs[1]]),
                (f"since {REVERSE_RULE_CHOSEN}", [t for t in trades if t[0] >= cutoffs[1]])):
            print(f"  {label}: {summarize(group) if group else '0 trades'}")


def check(db, live_path="market_data.db"):
    """Compare the downloaded history with what the live scanner recorded for the same markets."""
    live = sqlite3.connect(f"file:{live_path}?mode=ro", uri=True)
    agree = total = 0
    for strike, settles_at, poly_result in live.execute(
            "SELECT strike, settles_at, poly_result FROM settlements"
            " WHERE poly_result IS NOT NULL AND poly_source LIKE '%1h close'"):
        settles = int(datetime.fromisoformat(settles_at).timestamp())
        close = db.execute("SELECT close FROM binance WHERE minute = ?", (settles - 60,)).fetchone()
        if close:
            total += 1
            agree += ("yes" if close[0] > strike else "no") == poly_result
    print(f"Polymarket results rebuilt from Binance candles: {agree} of {total} match the live record")

    poly_gaps, kalshi_gaps = [], []
    for ts, strike, settles_at, kalshi_ask, poly_ask in live.execute(
            "SELECT q.ts, s.strike, s.settles_at, q.kalshi_ask, q.poly_ask FROM quotes q JOIN settlements s"
            " ON q.kalshi_ticker = s.kalshi_ticker AND q.poly_id = s.poly_id"
            " WHERE q.direction = 'poly_yes_kalshi_no' AND q.flags = '' AND q.kalshi_ask IS NOT NULL"):
        minute = int(datetime.fromisoformat(ts).timestamp()) // 60 * 60
        saved = db.execute("SELECT kalshi_no_ask, poly_mid FROM quotes WHERE settles = ? AND strike = ? AND minute = ?",
                           (int(datetime.fromisoformat(settles_at).timestamp()), round(strike), minute)).fetchone()
        if saved and abs(saved[1] - 0.5) >= EMPTY_BOOK_BAND:
            kalshi_gaps.append(kalshi_ask - saved[0])
            poly_gaps.append(poly_ask - saved[1])
    for name, gaps in (("Kalshi NO ask, live minus history", kalshi_gaps),
                       ("Polymarket YES ask, live minus history midpoint", poly_gaps)):
        gaps.sort()
        if gaps:
            print(f"{name}: {len(gaps)} quotes, median {gaps[len(gaps) // 2]:+.3f},"
                  f" 25% {gaps[len(gaps) // 4]:+.3f}, 75% {gaps[len(gaps) * 3 // 4]:+.3f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60, help="how far back to download")
    parser.add_argument("--report", action="store_true", help="skip the download and show results")
    parser.add_argument("--check", action="store_true", help="compare the history with the live scanner's data")
    args = parser.parse_args()

    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    if args.check:
        check(db)
        return
    if not args.report:
        download(db, args.days)
    report(db)


if __name__ == "__main__":
    main()
