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
kalshi_markets = []

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
    kalshi_markets.append({
        "title": title,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
    })

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
print("\nStored Kalshi Markets:", len(kalshi_markets))
print(kalshi_markets[0])

poly_url = "https://gamma-api.polymarket.com/markets"

poly_response = requests.get(
    poly_url,
    params = {"limit": 5, "closed": "false", "active": "true"},
    timeout = 10
)

poly_response.raise_for_status()
poly_data = poly_response.json()

print("Number of Polymarket markets:", len(poly_data))

poly_markets = []

for market in poly_data:
    question = market["question"]
    prices = json.loads(market["outcomePrices"])

    yes_price = float(prices[0])
    no_price = float(prices[1])

    poly_markets.append({
        "question": question,
        "yes_price": yes_price,
        "no_price": no_price,
    })

    best_bid = float(market["bestBid"])
    best_ask = float(market["bestAsk"])
    volume = float(market["volume24hr"])
    
    print("\nQuestion:", question)
    print("Yes Price:", yes_price)
    print("No Price:", no_price)
    print("Best Bid:", best_bid)
    print("Best Ask:", best_ask)
    print("24h Volume:", volume)

print("\nStored Polymarket markets:", len(poly_markets))
print(poly_markets[0])