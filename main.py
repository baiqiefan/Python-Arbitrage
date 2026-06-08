import requests

url = "https://external-api.kalshi.com/trade-api/v2/markets"
response = requests.get(url,
                        params = {"limit" : 5 , "status": "open"},
                        timeout = 10)

print("Status Code: ", response.status_code)

data = response.json()
markets = data["markets"]

for market in markets:
    print("Market: ", market["title"])
    print("Ticker:", market["ticker"])
    print("Yes Price: ", market["yes_ask_dollars"])
    print("No Price: ", market["no_ask_dollars"])

print("\nType of data: ", type(data))
print("\nKeys in data: ", data.keys())

