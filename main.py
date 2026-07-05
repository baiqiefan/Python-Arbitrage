import requests
import json

kalshi_url = "https://external-api.kalshi.com/trade-api/v2/markets"

response = requests.get(
    kalshi_url,
    params = {"limit" : 100 , "status": "open", "series_ticker": "KXBTCD"},
    timeout = 10
)

response.raise_for_status()

data = response.json()
markets = data["markets"]

print("Number of markets: ", len(markets))
printed = 0
kalshi_markets = []

for market in markets:
    title = market["title"]
    ticker = market["ticker"]
    close_time = market["close_time"]
    yes_bid = float(market["yes_bid_dollars"])
    yes_ask = float(market["yes_ask_dollars"])
    no_bid = float(market["no_bid_dollars"])
    no_ask = float(market["no_ask_dollars"])
    volume = float(market["volume_24h_fp"])

    if yes_ask <= 0 or yes_ask >= 1:
        continue

    if no_ask <= 0 or no_ask >= 1:
        continue

    printed = printed + 1
    strike = float(ticker.split("-T")[1])

    kalshi_markets.append({
        "strike": strike,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "close_time": close_time,
    })

    print("\nMarket: ", title)
    print("Strike: ", strike)
    print("Ticker: ", ticker)
    print("Yes Ask: ", yes_ask)
    print("Yes Bid: ", yes_bid)
    print("No Bid: ", no_bid)
    print("No Ask: ", no_ask)
    print("24h Volume: ", volume)
    print("Close Time: ", close_time)
 
    spread = yes_ask - yes_bid
    print("Spread:", round(spread, 4))

print("\nNumber of markets printed: ", printed)

print("\n\n===== POLYMARKET BITCOIN =====")

search_url = "https://gamma-api.polymarket.com/public-search"

search_response = requests.get(
    search_url,
    params = {"q": "bitcoin", "limit_per_type": 10},
    timeout = 10
)

search_response.raise_for_status()
search_data = search_response.json()

events = search_data["events"]

poly_markets = []

for event in events:
    if event["closed"]:
        continue

    if "above" not in event["title"]:
        continue

    for market in event["markets"]:
        strike = float(market["groupItemTitle"].replace(",", ""))
        prices = json.loads(market["outcomePrices"])
        yes_price = float(prices[0])
        no_price = float(prices[1])
        end_date = market["endDate"]

        poly_markets.append({
            "strike": strike,
            "yes_price": yes_price,
            "no_price": no_price,
            "end_date": end_date,
        })

        print("Strike:", strike, "-> No:", no_price, "End:", end_date)

print("\nStored Polymarket markets:", len(poly_markets))

print("\n\n===== CHECKING FOR ARBITRAGE =====")

found = 0

for k in kalshi_markets:
    for p in poly_markets:
        if abs(k["strike"] - p["strike"]) > 50:
            continue
        
        if k["close_time"] != p["end_date"]:
            continue

        cost = k["yes_ask"] + p["no_price"]

        print("\nMatched strike ~", p["strike"])
        print("  Kalshi  Yes:", k["yes_ask"], " No:", k["no_ask"])
        print("  Poly    Yes:", p["yes_price"], " No:", p["no_price"])
        print("  Kalshi-Yes + Poly-No cost:", round(cost, 4))

        if cost < 1.00:
            found = found + 1
            print("  >>> ARBITRAGE! profit:", round(1 - cost, 4))
        
        cost2 = p["yes_price"] + k["no_ask"]
        print("  Poly-Yes + Kalshi-No cost:", round(cost2, 4))

        if cost2 < 1.00:
            print("  >>> ARBITRAGE! profit:", round(1 - cost2, 4))
            found = found + 1

if found == 0:
    print("No arbitrage found. Markets are fairly priced or don't match.")

