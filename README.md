# Python-Arbitrage

A small Python tool that scans for **price arbitrage between two prediction markets — [Kalshi](https://kalshi.com) and [Polymarket](https://polymarket.com)** — using their public APIs.

It focuses on **Bitcoin price markets** ("will BTC be above $X at settlement?") and looks for moments where the same real-world bet is priced differently on the two platforms.

**Learning project.** This was built to learn Python, real-world APIs, and how to think carefully about market data. It is **not** trading advice and does not place trades. See [Limitations](#limitations).

## How it works

1. **Fetch** open Bitcoin markets from both platforms:
   - Kalshi — the `KXBTCD` daily price series.
   - Polymarket — the Gamma API search for `bitcoin`.
2. **Extract** two things from every market:
   - the **strike price** (Kalshi hides it in the ticker, e.g. `KXBTCD-26JUL0512-T61999.99` → `$61,999.99`; Polymarket exposes it in `groupItemTitle`).
   - the **settlement time** (Kalshi `close_time`, Polymarket `endDate`).
3. **Match** a Kalshi market to a Polymarket market only when **both** agree:
   - strike prices are within $50 of each other, **and**
   - they settle at the **exact same time**.
4. **Check both directions** for a guaranteed profit:
   - Kalshi-Yes + Polymarket-No < \$1.00, or
   - Polymarket-Yes + Kalshi-No < \$1.00.
5. **Report honestly** — if nothing truly matches, it says so instead of inventing a fake opportunity.

### Why the settlement-time check matters

Matching on strike price *alone* produces **false positives**. Two markets can both be "BTC above $62,000" yet settle **hours apart** — Bitcoin can move a lot in between, so they are *not* the same bet. The tool only treats a pair as arbitrage when the strike **and** the settlement timestamp match exactly.

On these two platforms the alignment is real: Kalshi's noon-ET Bitcoin market (`...12` in the ticker) settles at **16:00 UTC**, the same moment Polymarket resolves. That match is only *tradeable* during the midday window, so run the tool around **midday ET** to catch a live pair.

## Requirements

- Python 3.9+
- [`requests`](https://pypi.org/project/requests/)

```bash
pip install requests
```

## Usage

```bash
python main.py
```

The script prints each platform's Bitcoin markets, then a `CHECKING FOR ARBITRAGE` section listing any true matched pairs (or `No arbitrage found`).

## Limitations

This finds *theoretical* price gaps, not free money. A real trade would also have to account for:

- **Trading fees and bid/ask spreads**, which eat thin margins.
- **Speed** — professional bots take real mispricings in milliseconds.
- **Capital lockup** until settlement.
- **Legal/access** — Kalshi is US-regulated (CFTC); Polymarket is restricted for US persons.

## Roadmap

- [x] Compare Kalshi vs Polymarket Bitcoin prices
- [x] Match on strike **and** settlement time (no false positives)
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
