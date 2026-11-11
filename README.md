# Python-Arbitrage

A small Python tool that scans for **price arbitrage between two prediction markets — [Kalshi](https://kalshi.com) and [Polymarket](https://polymarket.com)** — using their public APIs.

It focuses on **Bitcoin price markets** ("will BTC be above $X at settlement?") and looks for moments where the same real-world bet is priced differently on the two platforms.

**Learning project.** This was built to learn Python, real-world APIs, and how to think carefully about market data. It is **not** trading advice and does not place trades. See [Limitations](#limitations).

## How it works

1. **Fetch** open Bitcoin markets from both platforms:
   - Kalshi — the `KXBTCD` price series.
   - Polymarket — the Gamma API search for active `bitcoin above` events, used for market details only.
2. **Normalize** every market into the same fields: strike, settlement time, settlement source and fees. A malformed market is logged and skipped instead of stopping the scan.
3. **Match** a Kalshi market to a Polymarket market only when **both** agree:
   - strike prices differ by no more than 2 cents (Kalshi's `81999.99` vs Polymarket's `82000`), **and**
   - they settle at the **exact same time**.
4. **Read the order books** for matched pairs and convert both to one format. A YES ask is `1 - best NO bid` on either platform.
5. **Check both directions** using real ask prices and available size, after fees:
   - Kalshi-Yes + Polymarket-No, and
   - Polymarket-Yes + Kalshi-No.
6. **Log everything** to `market_data.db`, including scans with no match and any data problems, so results can be measured over time.

### Why the settlement-time check matters

Matching on strike price *alone* produces **false positives**. Two markets can both be "BTC above $62,000" yet settle **hours apart** — Bitcoin can move a lot in between, so they are *not* the same bet. The tool only treats a pair as arbitrage when the strike **and** the settlement timestamp match exactly.

Polymarket's daily markets settle at **noon ET** (16:00 UTC in daylight saving time, 17:00 UTC otherwise), so a pair can only appear while Kalshi has a market open for that same time. Matches are not guaranteed at any given moment, which is why each scan logs how close the two platforms came.

### Matched pairs still aren't risk-free

The two platforms settle on **different prices**. Kalshi uses a 60-second average of the CF Benchmarks BRTI index, while Polymarket uses a Binance BTC/USDT candle close. If those land on opposite sides of the strike, the two legs can both lose. The scanner records both sources with every quote.

## Results

About five days of live scans (1,350 settled pairs) and a 60-day backtest gave a clear answer: **no reliable edge.**

- **Kalshi-Yes + Polymarket-No** showed "profitable" quotes but lost in every bucket: 240 settled trades averaged -$0.052 per contract. The two settlement prices were about $100 apart, so both legs often lost.
- **Polymarket-Yes + Kalshi-No** looked good live (+$0.07 per contract over 81 trades). `backtest.py` replayed it on 57 earlier days (2,728 trades). After correcting a bias of about $0.08 per contract in the backtest's own price estimate, the result was about zero or negative.
- **The lesson:** the market already prices the gap between the two settlement sources about right, and a small lucky sample can look like an edge.

## Requirements

- Python 3.9+
- [`requests`](https://pypi.org/project/requests/)

```bash
pip install requests
```

## Usage

```bash
python main.py            # run one scan
python main.py --loop     # scan every 60 seconds
python main.py --report   # summarize the logged data
python backtest.py        # replay the reverse trade on about 60 days of settled hours
python -m unittest        # run the tests
```

## Run it on a server

`deploy/setup.sh` installs the scanner as a `systemd` service on an Ubuntu server, so it starts on boot and restarts after a crash:

```bash
git clone -b order-book-data https://github.com/baiqiefan/Python-Arbitrage.git
cd Python-Arbitrage
bash deploy/setup.sh
```

Pick a server outside the US: Binance blocks US addresses, and the Binance price is half of the comparison.

Each scan prints any matched pairs with their prices and profit after fees, then a summary of how many markets were open and how close the platforms came to matching.

`paper_trade.py` holds simulated trading code that is parked until the scanner has collected enough data to choose a trading rule.

## Limitations

This finds *theoretical* price gaps, not free money. A real trade would also have to account for:

- **Different settlement prices**, as explained above. Fees and spreads are already included, using each platform's published fee formula.
- **Speed** — professional bots take real mispricings in milliseconds.
- **Capital lockup** until settlement.
- **Legal/access** — Kalshi is US-regulated (CFTC); Polymarket is restricted for US persons.

## Roadmap

- [x] Compare Kalshi vs Polymarket Bitcoin prices
- [x] Match on strike **and** settlement time
- [x] Use real order book prices and fees
- [x] Log scans and data problems over time
- [x] Check both arbitrage directions
- [ ] Add more prediction markets
- [ ] (Someday, maybe) automated trading

## Future plans

Right now this only compares **Bitcoin** markets between Kalshi and Polymarket. The plan is to grow it over time:

- **More topics** — extend beyond Bitcoin to other markets both platforms share (other crypto, stock indices, economic data, sports, elections, etc.).
- **More platforms** — add other prediction markets so opportunities aren't limited to just two venues.
- **Better matching** — smarter event alignment (e.g. tolerances for close-but-not-identical settlement times, fuzzy question matching).
- **Track over time** — log price differences to spot patterns instead of only checking a single moment.
- **Someday, maybe** — automated trading, once everything above is solid and reliable.

## License

Released under the [MIT License](LICENSE).
