# stock_screener

[![CI](https://github.com/oliverbrown/stock_screener/actions/workflows/ci.yml/badge.svg)](https://github.com/oliverbrown/stock_screener/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

A no-API-key stock/ETF screener. Pulls live data from Yahoo Finance via
[`yfinance`](https://github.com/ranaroussi/yfinance) and flags names as
**undervalued / oversold** (buy side) or **overvalued / overbought /
overextended** (sell side), with a separate valuation model for ETFs and
other funds.

> [!IMPORTANT]
> **This is not financial advice.** The signals are simple, rule-of-thumb
> heuristics (P/E vs. a sector average, RSI, distance from a moving average,
> etc.) — they can be wrong, stale, or miss context a human would catch.
> **Always do your own research before acting on any result this tool
> produces.**

## Requirements

```bash
pip install yfinance pandas
```

Python 3.10+.

## Quick start

```bash
python3 stock_screener.py                       # default 15-stock watchlist
python3 stock_screener.py --index sp500          # screen the whole S&P 500
python3 stock_screener.py --tickers AAPL MSFT TSLA
```

Every run also prints a summary banner, then a per-ticker report, then a
one-line pointer to a timestamped log copy of the whole run in `./logs/`.

## Signals

### Stocks (equities)

| Signal | Trigger |
|---|---|
| `undervalued` | P/E well below the stock's sector average, or low P/B |
| `oversold` | RSI(14) < 35 |
| `overvalued` | P/E well above the sector average, or PEG > 2 |
| `overbought` | RSI(14) > 70 |
| `overextended` | Price > 20% above its 200-day moving average |
| `buy` | undervalued **and** oversold |
| `sell` | overvalued **and** (overbought or overextended) |
| `mixed` | has both a buy-side and a sell-side tag |
| `neutral` | none of the above |

A **value-trap guard** suppresses `undervalued` (and notes why) when the
stock also has high debt/equity (>200%) or a negative profit margin — cheap
for a reason, not a bargain.

Every stock row also shows: P/E, P/B, PEG, RSI, 50/200-day trend, distance
from its 52-week high/low, debt/equity, profit margin, dividend yield, and
short interest (% of float).

### ETFs and other funds

Auto-detected per ticker (via Yahoo's `quoteType`) — no flag needed, and a
mixed stocks+ETFs list just works. Same signal vocabulary as stocks, with
`neutral` shown as **`hold`**, and valuation coming from:

- **Price vs. NAV** — premium/discount to the fund's net asset value
- **Holdings P/E vs. category benchmark** — e.g. Large Blend ~21x,
  Technology ~28x; skipped for bond/preferred/commodity funds that have no
  meaningful P/E

A **cost guard** suppresses `undervalued` on a fund with an expense ratio
above 0.75% (and notes a cost watch above 0.50%).

Every fund row also shows: price vs. NAV, holdings P/E, RSI, 50/200-day
trend, 52-week range, expense ratio, distribution yield, and 3-year beta.

Non-ETF, non-equity tickers (crypto, futures, …) route through the fund path
too and degrade gracefully to RSI/trend/52-week signals only.

## Ticker sources

```bash
python3 stock_screener.py --index sp500              # sp500 | dow30 | nasdaq100 | russell2000
python3 stock_screener.py --tickers AAPL MSFT TSLA
python3 stock_screener.py --tickers-file watchlist.txt
python3 stock_screener.py --list-indices              # show what's available
```

`--index` tries to scrape the current constituent list from Wikipedia first,
falling back to a bundled static list if that fails (shown in the output
either way).

`--tickers-file` reads symbols separated by **any mix** of whitespace,
newlines, carriage returns, and/or commas:

```
AAPL, MSFT GOOGL
TSLA	NVDA,,amzn
```

Precedence when more than one is given: `--tickers` > `--tickers-file` >
`--index` > default watchlist.

## Filtering

```bash
--signal flagged        # default: anything non-neutral
--signal buy            # undervalued / oversold / buy
--signal sell           # overvalued / overbought / overextended / sell
--signal hold            # no signal either way
--signal all             # everything, unfiltered
--signal undervalued|oversold|overvalued|overbought   # one specific tag

--asset-type all|stock|etf   # restrict a mixed ticker list
--max-pe 20                  # only names with P/E under 20 (holdings P/E for funds)
```

## Output

```bash
python3 stock_screener.py --index sp500 --output daily.html
```

Writes an HTML report alongside the terminal report. When `--index` is set,
the index name is folded into the filename automatically:
`daily.html` → `daily-sp500.html`. Use a literal `{index}` in the name to
place it yourself (`report-{index}.html`), or omit `--index` to leave the
name untouched.

## Logs

Every run is mirrored — console output, unmodified — to a uniquely named,
timestamped file: `./logs/screener_<YYYYMMDD_HHMMSS>.log` (override the
directory with `--log-dir`).

## Plain-ASCII output

Emoji status icons render out of the box on macOS but need a color-emoji
font on many Linux terminals. `--ascii` swaps them for plain tags
(`[BUY]`, `[SELL]`, `[UV]`, `[OV]`, `[OS]`, `[OB]`, `[MIX]`, `[HOLD]`) and
folds box-drawing/dashes/checkmarks down to 7-bit ASCII — including in the
log file. It auto-enables on a dumb or non-UTF-8 terminal, or when the
`STOCK_SCREENER_ASCII` environment variable is set.

## Example runner

[`run-scanner.sh`](run-scanner.sh) is a runnable example that drives the
screener over indices and an inline watchlist. Copy it to
`run-my-scanner.sh` (gitignored) and point it at your own `--tickers-file`
lists.

## Disclaimer

This tool is provided for informational and educational purposes only. It
does not constitute financial, investment, or trading advice, and nothing it
outputs should be treated as a recommendation to buy, sell, or hold any
security. The signals are simple heuristics computed from public data — they
can be wrong, incomplete, or based on stale/incorrect data from the
underlying source (Yahoo Finance via `yfinance`).

**Always do your own research and consult a qualified financial advisor
before making any investment decision.** Use of this software is entirely at
your own risk; see [LICENSE](LICENSE) for the full "as is, no warranty"
terms.
