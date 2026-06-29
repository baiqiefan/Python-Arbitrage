import requests
import json

kalshi_url = "https://external-api.kalshi.com/trade-api/v2/markets"

response = requests.get(
    kalshi_url,
    params = {"limit" : 20 , "status": "open", "mve_filter": "exclude"},
    timeout = 10
)

response.raise_for_status()

data = response.json()
markets = data["markets"]

print("Number of markets: ", len(markets))
printed = 0

for market in markets:
    title = market["title"]
    ticker = market["ticker"]
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

    print("\nMarket: ", title)
    print("Ticker: ", ticker)
    print("Yes Ask: ", yes_ask)
    print("Yes Bid: ", yes_bid)
    print("No Bid: ", no_bid)
    print("No Ask: ", no_ask)
    print("24h Volume: ", volume)

    spread = yes_ask - yes_bid
    print("Spread:", round(spread, 4))

print("\nNumber of markets printed: ", printed)


print("\n\n===== POLYMARKET =====")

poly_url = "https://gamma-api.polymarket.com/markets"

poly_response = requests.get(
    poly_url,
    params = {"limit": 5, "closed": "false", "active": "true"},
    timeout = 10
)

poly_response.raise_for_status()
poly_data = poly_response.json()

first_market = poly_data[0]

question = first_market["question"]
prices = json.loads(first_market["outcomePrices"])

yes_price = float(prices[0])
no_price = float(prices[1])

best_bid = float(first_market["bestBid"])
best_ask = float(first_market["bestAsk"])
volume = float(first_market["volume24hr"])

print("\nQuestion:", question)
print("Yes Price:", yes_price)
print("No Price:", no_price)
print("Best Bid:", best_bid)
print("Best Ask:", best_ask)
print("24H Volume:", volume)