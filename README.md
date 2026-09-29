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
pip install -r requirements.txt
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

When a stock is both overbought and overextended (but not overvalued), the
headline reads `overbought`; both tags still apply, so `--signal
overextended` still finds it.

A **value-trap guard** suppresses `undervalued` (and notes why) when the
stock also has high debt/equity (>200%) or a negative profit margin — cheap
for a reason, not a bargain.

Every stock row also shows: P/E, P/B, PEG, RSI, 50/200-day trend, distance
from its 52-week high/low, debt/equity, profit margin, dividend yield, short
interest (% of float), and the analysts' **1-year consensus price target**
(Yahoo's "1y Target Est" — the mean target) with upside/downside vs. the
current price, the low–high target range, the number of analysts, and their
consensus rating (e.g. `buy`). These are shown for context only; they don't
feed into the signal.

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

### Pre-market / after-hours price

While the pre-market (4:00–9:30am ET) or after-hours (4:00–8:00pm ET)
session is open, stocks and ETFs also show the extended-hours price and its
% change from the regular close — e.g. `Post-mkt: $341.46 (+0.11%)`. Outside
those sessions nothing extra is shown. Signals always use the
regular-session price.

Non-ETF, non-equity tickers (crypto, futures, …) route through the fund path
too and degrade gracefully to RSI/trend/52-week signals only.

## Ticker sources

```bash
python3 stock_screener.py --index sp500              # sp500 | dow30 | nasdaq100 | russell2000
python3 stock_screener.py --tickers AAPL MSFT TSLA
python3 stock_screener.py --tickers-file watchlist.txt
python3 stock_screener.py --list-indices              # show what's available
```

`--index` fetches the current members live (S&P 500 from Wikipedia,
Nasdaq-100 from Nasdaq, Dow 30 from the SPDR DIA ETF's holdings), falling
back to a bundled static list if that fails (shown in the output either
way). The Russell 2000 comes from the holdings of the iShares Russell 2000
ETF (IWM); its offline fallback is only a 190-stock sample.

`--tickers-file` reads symbols separated by **any mix** of whitespace,
newlines, carriage returns, and/or commas. A `#` starts a comment that runs
to the end of the line, so you can annotate a list:

```
AAPL, MSFT GOOGL
TSLA	NVDA,,amzn

# Energy
XOM     # Exxon Mobil
CVX     # Chevron
```

Share-class tickers can be written either way — `BRK.B` and `BF.B` are
converted to Yahoo's `BRK-B` / `BF-B` form automatically. Exchange suffixes
such as `SHOP.TO` or `VOD.L` are left alone.

Precedence when more than one is given: `--tickers` > `--tickers-file` >
`--index` > default watchlist.

### Skipped tickers

A ticker that can't be screened is marked `skipped` with the reason (e.g.
`no price data (unknown, delisted, or mistyped ticker?)`), and every skipped
ticker is listed again at the end of the fetch. If Yahoo rate-limits a
request, the screener waits and retries (10s, 30s, then 60s) before giving
up on that ticker.

## Reference ticker lists

[`build_ticker_lists.py`](build_ticker_lists.py) downloads the current
members of the major US indices and every US exchange listing, and writes
one file per list to `./ticker-lists/`, one ticker per line with its name as
a comment:

```
AAPL   # Apple Inc.
AMGN   # Amgen Inc.
BRK-B  # Berkshire Hathaway
```

```bash
python3 build_ticker_lists.py                 # build every list
python3 build_ticker_lists.py sp400 nyse       # just these
python3 build_ticker_lists.py --list           # show what's available
python3 stock_screener.py --tickers-file ticker-lists/sp400.txt --signal buy
```

| List | Contents | Source |
|---|---|---|
| `sp500`, `sp400`, `sp600` | S&P 500 / MidCap 400 / SmallCap 600 | Wikipedia |
| `nasdaq100` | Nasdaq-100 | Nasdaq |
| `dow30` | Dow Jones Industrial Average | SPDR DIA ETF holdings |
| `russell2000` | Russell 2000 | iShares Russell 2000 ETF (IWM) holdings |
| `nasdaq`, `nyse`, `nyse-american`, `nyse-arca`, `cboe` | Everything listed on that exchange | NASDAQ Trader symbol directory |
| `us-stocks`, `us-etfs`, `us-all` | All US-listed stocks, ETFs, or both | NASDAQ Trader symbol directory |

Exchange lists hold common stocks, ADRs and ETFs; add `--all-securities` to
also include warrants, rights, SPAC units, preferreds and notes. Symbols are
written in Yahoo's form (`BRK-B`, preferreds as `ABR-PD`).

Membership changes (index rebalances, IPOs, delistings), so re-run the
script to refresh. The generated files are gitignored.

## Filtering

```bash
--signal flagged        # default: anything non-neutral
--signal buy            # undervalued / oversold / buy
--signal sell           # overvalued / overbought / overextended / sell
--signal hold            # no signal either way
--signal all             # everything, unfiltered
--signal undervalued|oversold|overvalued|overbought|overextended   # one specific tag

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

Each ticker in the HTML report links to its Yahoo Finance quote page
(opens in a new tab).

## Logs

Every run is mirrored — console output, unmodified — to a uniquely named,
timestamped file: `./logs/screener_<YYYYMMDD_HHMMSS>.log` (override the
directory with `--log-dir`).

Logs older than 30 days are deleted automatically at the start of each run.
Change that with `--keep-logs DAYS`, or keep everything with `--keep-logs 0`.
Only `screener_*.log` files are ever removed.

## Data cache

Each ticker's downloaded data (~38 KB from Yahoo) is saved to `./cache/`
and reused for **30 minutes**, so a runner script that screens overlapping
lists — your stocks, then the S&P 500, then the Nasdaq-100 — downloads each
ticker only once. Cached tickers are marked `(cached 12m ago)` in the
progress output, and each run ends with a `Data: N from cache, M downloaded`
line.

```bash
--cache-minutes 60    # reuse data for up to an hour
--no-cache            # always download fresh data
--cache-dir DIR       # default: ./cache
```

Only successful fetches are cached (never skipped tickers or errors), and
entries older than a day are cleaned up automatically. Cached prices can be
up to `--cache-minutes` old, so use `--no-cache` when you need the latest
price during market hours.

## Plain-ASCII output

Emoji status icons render out of the box on macOS but need a color-emoji
font on many Linux terminals. `--ascii` swaps them for plain tags
(`[BUY]`, `[SELL]`, `[UV]`, `[OV]`, `[OS]`, `[OB]`, `[MIX]`, `[HOLD]`) and
folds box-drawing/dashes/checkmarks down to 7-bit ASCII — including in the
log file. It auto-enables on a dumb or non-UTF-8 terminal, or when the
`STOCK_SCREENER_ASCII` environment variable is set.

## Example runner

[`run-screener.sh`](run-screener.sh) is a runnable example that drives the
screener over indices and an inline watchlist. Copy it to
`run-my-screener.sh` (or any `run-my-*.sh` name — all gitignored) and point it at your own `--tickers-file`
lists.

## Tests

The signal rules, ticker parsing, output naming, RSI, rate-limit retries and
filters are covered by an offline test suite (no network calls — Yahoo is
faked):

```bash
pip install -r requirements-dev.txt
python3 -m pytest
```

CI runs it on Python 3.10–3.12 for every push and pull request.

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
