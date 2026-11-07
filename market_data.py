"""Fetch Kalshi and Polymarket Bitcoin markets and order books in one shared format."""
import json
import math
import statistics
from datetime import datetime, timezone

import requests

KALSHI_URL = "https://external-api.kalshi.com/trade-api/v2"
GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
# Polymarket settles on Binance BTC/USDT. Kalshi settles on CF BRTI, which is built from several
# USD exchanges; Coinbase, Kraken and Bitstamp are three of them, so together they stand in for it.
BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
COINBASE_PRICE_URL = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
KRAKEN_PRICE_URL = "https://api.kraken.com/0/public/Ticker"
BITSTAMP_PRICE_URL = "https://www.bitstamp.net/api/v2/ticker/btcusd/"
# (name, url, query params, how to pull the last trade price out of the reply)
SPOT_SOURCES = (
    ("binance", BINANCE_PRICE_URL, {"symbol": "BTCUSDT"}, lambda data: data["price"]),
    ("coinbase", COINBASE_PRICE_URL, {}, lambda data: data["price"]),
    # Kraken replies {"error": [], "result": {"XXBTZUSD": {"c": ["75991.6", "0.0005"], ...}}}
    ("kraken", KRAKEN_PRICE_URL, {"pair": "XBTUSD"}, lambda data: data["result"]["XXBTZUSD"]["c"][0]),
    ("bitstamp", BITSTAMP_PRICE_URL, {}, lambda data: data["last"]),
)
BRTI_STAND_INS = ("coinbase", "kraken", "bitstamp")

MAX_CONTRACTS = 100
# Kalshi strikes end in .99 (81999.99) and Polymarket's are round (82000).
MAX_STRIKE_GAP = 0.02
DANGER_DOLLARS_PER_SQRT_MINUTE = 50
KALSHI_FEE_RATE = 0.07


def get_json(url, **params):
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    return response.json()


def parse_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def settlement_source(rules):
    """Name the price each market settles on, so a pair shows what it really compares."""
    if "BRTI" in rules:
        return "CF BRTI 60s average"
    if "Binance" in rules and "1 hour" in rules:
        return "Binance BTC/USDT 1h close"
    if "Binance" in rules and "1 minute" in rules:
        return "Binance BTC/USDT 1m close"
    return "unknown"


def normalize_kalshi_market(market):
    if market["strike_type"] != "greater":
        raise ValueError(f"unsupported strike_type {market['strike_type']}")
    return {
        "ticker": market["ticker"],
        "strike": float(market["floor_strike"]),
        "settles_at": parse_time(market["close_time"]),
        "source": settlement_source(market["rules_primary"]),
    }


def normalize_poly_market(market):
    if json.loads(market["outcomes"]) != ["Yes", "No"]:
        raise ValueError(f"unexpected outcomes {market['outcomes']}")
    yes_token, no_token = json.loads(market["clobTokenIds"])
    return {
        "id": market["id"],
        "strike": float(market["groupItemTitle"].replace(",", "")),
        "settles_at": parse_time(market["endDate"]),
        "source": settlement_source(market["description"]),
        "yes_token": yes_token,
        "no_token": no_token,
        "fee_rate": (market.get("feeSchedule") or {}).get("rate", 0),
        "min_size": float(market.get("orderMinSize") or 0),
    }


def fetch_kalshi_markets():
    """Return (markets, skipped). One malformed market is recorded, not fatal."""
    markets = []
    skipped = []
    cursor = None
    while True:
        params = {"limit": 1000, "status": "open", "series_ticker": "KXBTCD"}
        if cursor:
            params["cursor"] = cursor
        data = get_json(f"{KALSHI_URL}/markets", **params)
        for market in data["markets"]:
            try:
                markets.append(normalize_kalshi_market(market))
            except (KeyError, TypeError, ValueError) as error:
                skipped.append(("kalshi", market.get("ticker", "?"), repr(error)))
        cursor = data.get("cursor")
        if not cursor:
            return markets, skipped


def fetch_poly_markets():
    """Return (markets, skipped). One malformed market is recorded, not fatal."""
    data = get_json(
        f"{GAMMA_URL}/public-search",
        q="bitcoin above", limit_per_type=50, events_status="active",
    )
    markets = []
    skipped = []
    if (data.get("pagination") or {}).get("hasMore"):
        skipped.append(("polymarket", "search", "more results than one page returned"))
    for event in data["events"]:
        if event["closed"] or "above" not in event["title"]:
            continue
        for market in event["markets"]:
            if market["closed"] or not market.get("acceptingOrders"):
                continue
            try:
                markets.append(normalize_poly_market(market))
            except (KeyError, TypeError, ValueError) as error:
                skipped.append(("polymarket", market.get("id", "?"), repr(error)))
    return markets, skipped


def match_markets(kalshi_markets, poly_markets):
    pairs = []
    for k in kalshi_markets:
        for p in poly_markets:
            same_strike = abs(k["strike"] - p["strike"]) <= MAX_STRIKE_GAP
            if same_strike and k["settles_at"] == p["settles_at"]:
                pairs.append((k, p))
    return pairs


def coverage(kalshi_markets, poly_markets):
    """Explain a lack of matches: shared settlement times, and how far apart equal strikes settle."""
    shared = {k["settles_at"] for k in kalshi_markets} & {p["settles_at"] for p in poly_markets}
    gaps = [
        abs((k["settles_at"] - p["settles_at"]).total_seconds()) / 60
        for k in kalshi_markets for p in poly_markets
        if abs(k["strike"] - p["strike"]) <= MAX_STRIKE_GAP
    ]
    return {"shared_times": len(shared), "closest_gap_minutes": min(gaps) if gaps else None}


def make_book(yes_bids, no_bids):
    """Build one book shape: buying YES at p means matching a NO bid at 1 - p, and vice versa."""
    yes_bids = sorted(yes_bids, reverse=True)
    no_bids = sorted(no_bids, reverse=True)
    return {
        "yes_bids": yes_bids,
        "no_bids": no_bids,
        "yes_asks": [(round(1 - price, 4), size) for price, size in no_bids],
        "no_asks": [(round(1 - price, 4), size) for price, size in yes_bids],
        "fetched_at": datetime.now(timezone.utc),
    }


def kalshi_book_from(raw):
    raw = raw["orderbook_fp"]
    # Either side is null when it has no resting orders.
    return make_book(
        [(float(price), float(size)) for price, size in raw.get("yes_dollars") or []],
        [(float(price), float(size)) for price, size in raw.get("no_dollars") or []],
    )


def poly_book_from(raw):
    """Takes the YES token's book. Its asks mirror the NO token's bids at 1 - price."""
    return make_book(
        [(float(level["price"]), float(level["size"])) for level in raw["bids"]],
        [(round(1 - float(level["price"]), 4), float(level["size"])) for level in raw["asks"]],
    )


def fetch_kalshi_book(ticker):
    return kalshi_book_from(get_json(f"{KALSHI_URL}/markets/{ticker}/orderbook"))


def fetch_poly_book(yes_token):
    return poly_book_from(get_json(f"{CLOB_URL}/book", token_id=yes_token))


def book_flags(book):
    """Return a list of data problems in a book, such as ["crossed book"]. Empty means clean.

    An empty side is normal for strikes far from the price, so it isn't flagged here.
    """
    flags = []
    # A healthy book's best bids add up to $1.00 or less; more means stale or broken data.
    if book["yes_bids"] and book["no_bids"] and book["yes_bids"][0][0] + book["no_bids"][0][0] > 1:
        flags.append("crossed book")
    return flags


def fetch_spot_prices():
    """Return {name: BTC price or None} for every exchange in SPOT_SOURCES."""
    prices = {}
    for name, url, params, pick in SPOT_SOURCES:
        try:
            prices[name] = float(pick(get_json(url, **params)))
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError):
            prices[name] = None
    return prices


def brti_proxy(prices):
    """Estimate Kalshi's BRTI from the exchanges in BRTI_STAND_INS.

    prices is fetch_spot_prices() output; any exchange may be None if its request failed.
    Returns a float, or None when there isn't enough data to trust an estimate.
    """
    available = [prices[name] for name in BRTI_STAND_INS if prices.get(name) is not None]
    if len(available) < 2:
        return None
    return statistics.median(available)


def in_danger_zone(strike, binance, brti, minutes_left):
    """True if the two platforms could settle this strike differently.

    Kalshi settles on BRTI and Polymarket on Binance, and Binance has been running about $100 above.
    A strike between the two prices at settlement makes both legs lose. At quote time we only know
    today's binance and brti (the brti_proxy estimate), and BTC still moves for minutes_left minutes.
    """
    margin = DANGER_DOLLARS_PER_SQRT_MINUTE * math.sqrt(max(minutes_left, 0))
    low = min(binance, brti) - margin
    high = max(binance, brti) + margin
    return low <= strike <= high

def kalshi_settlement(ticker):
    """Return (result, settlement value), or (None, None) before Kalshi publishes them."""
    market = get_json(f"{KALSHI_URL}/markets/{ticker}")["market"]
    if not market["result"]:
        return None, None
    return market["result"], float(market["expiration_value"]) if market.get("expiration_value") else None


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


def legs_won(direction, kalshi_result, poly_result):
    """How many of the two legs paid $1. One is the plan; 0 or 2 means the sources split."""
    kalshi_side, poly_side = ("yes", "no") if direction == "kalshi_yes_poly_no" else ("no", "yes")
    return (kalshi_result == kalshi_side) + (poly_result == poly_side)


def directions(k_book, p_book):
    return (
        ("kalshi_yes_poly_no", k_book["yes_asks"], p_book["no_asks"]),
        ("poly_yes_kalshi_no", k_book["no_asks"], p_book["yes_asks"]),
    )


def fee_per_contract(rate, price):
    return rate * price * (1 - price)


def net_per_contract(kalshi_price, poly_price, poly_fee_rate):
    fees = fee_per_contract(KALSHI_FEE_RATE, kalshi_price) + fee_per_contract(poly_fee_rate, poly_price)
    return 1 - kalshi_price - poly_price - fees


def simulate_fill(k_asks, p_asks, poly_fee_rate, min_size=0, max_contracts=MAX_CONTRACTS):
    """Buy both legs level by level while each extra contract still profits after fees."""
    k_levels = [list(level) for level in k_asks]
    p_levels = [list(level) for level in p_asks]
    contracts = 0
    kalshi_cost = poly_cost = kalshi_fee = poly_fee = 0.0

    while k_levels and p_levels and contracts < max_contracts:
        k_price, k_size = k_levels[0]
        p_price, p_size = p_levels[0]
        if net_per_contract(k_price, p_price, poly_fee_rate) <= 0:
            break
        take = int(min(k_size, p_size, max_contracts - contracts))
        if take > 0:
            contracts += take
            kalshi_cost += take * k_price
            poly_cost += take * p_price
            kalshi_fee += take * fee_per_contract(KALSHI_FEE_RATE, k_price)
            poly_fee += take * fee_per_contract(poly_fee_rate, p_price)
        k_levels[0][1] -= take
        p_levels[0][1] -= take
        if k_levels[0][1] < 1:
            k_levels.pop(0)
        if p_levels[0][1] < 1:
            p_levels.pop(0)

    if contracts == 0 or contracts < min_size:
        return None

    # Kalshi rounds each order's fee up to the cent; round() strips float noise first.
    kalshi_fee = math.ceil(round(kalshi_fee * 100, 6)) / 100
    poly_fee = round(poly_fee, 5)
    net = contracts - kalshi_cost - poly_cost - kalshi_fee - poly_fee
    return {
        "contracts": contracts,
        "kalshi_cost": kalshi_cost,
        "poly_cost": poly_cost,
        "kalshi_fee": kalshi_fee,
        "poly_fee": poly_fee,
        "net_per_contract": net / contracts,
    }
