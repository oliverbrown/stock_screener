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
stock also has high debt/equity (>200%), a negative profit margin, or
negative free cash flow — cheap for a reason, not a bargain. The same
problems on a stock that isn't cheap are noted as a *quality watch*.

**Free-cash-flow yield** (free cash flow ÷ market cap) is shown for every
stock. Negative FCF isn't used in the value-trap check for financial stocks,
whose cash flow mostly reflects their loan book or premiums, and it's
ignored for foreign ADRs, which report cash flow in their local currency.
Companies with a finance arm (e.g. Ford Credit) can show negative FCF even
when the core business generates cash.

**P/B sanity check:** Yahoo's price-to-book is ignored (shown as
`N/A (bad data)`, with the reason in the HTML report) when it can't be
trusted: for foreign ADRs, whose book value is per local share in the local
currency (e.g. TSM, TM), and when it's implausibly low (< 0.05), as with
BRK-B, whose book value Yahoo reports per Class A share.

Every stock row also shows: P/E, P/B, PEG, RSI, 50/200-day trend, distance
from its 52-week high/low, debt/equity, profit margin, dividend yield, short
interest (% of float), and the analysts' **1-year consensus price target**
(Yahoo's "1y Target Est" — the mean target) with upside/downside vs. the
current price, the low–high target range, the number of analysts, and their
consensus rating (e.g. `buy`). These are shown for context only; they don't
feed into the signal.

### Performance vs the S&P 500

Every stock and fund shows how it has done against the S&P 500 over 3
months, 6 months and 1 year, in percentage points: `+12.3 pts` means its
total return beat the index by 12.3 points. The benchmark is SPY (fetched
once per run and cached like any ticker); both sides use dividend-adjusted
prices, so it compares total returns.

For stocks with a buy signal, the explanation adds a note when this changes
how the signal should be read:

- **Long-term laggard**: 20+ points behind the S&P 500 over a year, so the
  "dip" may be a longer decline.
- **Pullback in a leader**: 10+ points ahead over a year but 5+ behind over
  3 months.

It never changes a signal.

### Relative volume

Every stock and fund shows its **relative volume**: average volume over the
last 5 sessions divided by the average of the 50 sessions before them
(`1.8×` = 80% above normal), plus the price change over those 5 days.
1.5× or more is *heavy* (2× *very heavy*), 0.7× or less is *light*. While
the market is open, today's partial session is left out, so a half-finished
day doesn't read as light volume.

For stocks with a one-sided buy or sell signal that moved at least 1% in
those 5 days, the explanation notes the volume behind the move, e.g.
"sell-off (−9% in 5 days) on heavy volume (1.8× normal), so sellers are
committed, or capitulating" or "rally (+4% in 5 days) on light volume
(0.5× normal), so the move lacks conviction." It never changes a signal.

### Earnings dates

Every stock shows its **next earnings report**: the date, a countdown, and
whether it's before the open or after the close (or "estimated" when the
company hasn't confirmed it), e.g. `Oct 29 · in 31 days · after close`.

A report within **14 days** gets a 📅 *Earnings in 4 days* chip next to the
price, and — for any stock with a signal — a note in the explanation that
the report can quickly reverse it. A report in the last 5 days gets a note
that the price may still be adjusting. Like money flow, earnings dates never
change a signal. Funds have no earnings dates.

The countdown is worked out when the report is generated, so it stays
correct for cached data.

### Money flow (Chaikin Money Flow)

Every stock and fund also shows its 20-day **Chaikin Money Flow** (CMF),
from −1 to +1: where each day closed within its high–low range, weighted by
volume. Above +0.05 is **accumulation** (buyers pushing closes toward the
day's highs), below −0.05 is **distribution**, and beyond ±0.25 is strong.

CMF never changes a signal. For stocks with a one-sided buy or sell signal,
it adds a line to the explanation when money flow agrees or disagrees, e.g.
"Money flow: accumulation (CMF +0.12) supports the buy case" or "Money flow:
still distribution (CMF −0.10); selling pressure hasn't eased." Funds show
the value without the commentary, since much ETF volume is market-maker
creation/redemption rather than real buying and selling. CMF ignores
overnight gaps.

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

## Custom filters and watch lists

Beyond the built-in signals, you can filter on any data field, rank the
results, and save them as a watch list:

```bash
python3 stock_screener.py --tickers-file ticker-lists/sp500.txt \
    --where "fcf_yield>=5" --where "pe<20" --where "debt_equity<100" \
    --where "rel_1y>0" --where "earnings_days>7" \
    --sort-by fcf_yield:desc --top 10 --save-tickers watch.txt --csv watch.csv
```

| Option | What it does |
|---|---|
| `--where "FIELD OP VALUE"` | Keep tickers matching the condition; repeat to require several. Operators `< <= > >= = !=`; numbers may use `K`/`M`/`B`/`T` and `%` (`market_cap>=2B`). Text fields compare case-insensitively (`sector=Energy`); `tag=oversold` checks the signal tags |
| `--list-fields` | Every field you can use (valuation, quality, size, liquidity, momentum, returns vs the S&P 500, `earnings_days`, …) with units |
| `--sort-by FIELD[:desc]` | Rank matches; tickers missing the field go last |
| `--top N` | Keep the first N matches |
| `--save-tickers FILE` | Save matches as a `TICKER # name` watch list for `--tickers-file` |
| `--csv FILE` | Save every field for each match, for a spreadsheet |
| `--quiet` | No per-ticker progress lines |
| `--sector NAME` | Keep only this sector; repeat for *any of* several (`--sector Energy --sector Utilities`) |
| `--exclude-sector NAME` | Drop this sector; repeatable. ETFs (no sector) are kept |
| `--no-earnings-within N` | Drop tickers reporting in the next N days. Unlike `--where "earnings_days>N"`, keeps ETFs and tickers with no announced date |

With `--where`, `--signal` defaults to `all`, so only your conditions apply;
add e.g. `--signal buy` to combine them with the built-in signals. A ticker
**missing** a field never matches a condition on it — `pe<15` drops
loss-making companies (they have no P/E), and `market_cap` is empty for ETFs
(use `fund_assets`).

Useful extra fields: `market_cap`, `dollar_volume` (average daily $ traded —
filter out illiquid micro-caps in `us-all`), `forward_pe`, `ev_ebitda`,
`roe`. High FCF yields are common among insurers (their cash flow includes
premiums they'll later pay out); add `--where "sector!=Financial Services"`
to leave them out.

### Saved screens

Name a combination of settings once and run it with `--screen NAME`:

```bash
python3 stock_screener.py --list-screens
python3 stock_screener.py --tickers-file ticker-lists/sp500.txt --screen quality-value
python3 stock_screener.py --tickers-file my-stocks.txt --screen trim-candidates
```

[`screens.toml`](screens.toml) ships with examples — `quality-value`,
`quality-dip`, `oversold-accumulation`, `momentum-leaders`,
`dividend-income`, `etf-discounts` and `trim-candidates` — and documents
every setting. Put your own in **`my-screens.toml`** (gitignored), same
format; a screen there replaces a bundled one with the same name:

```toml
[my-cheap-tech]
description = "Cheap, profitable tech"
where = ["pe<18", "margin>=15", "market_cap>=5B"]
sector = ["Technology"]
no_earnings_within = 7
sort_by = "pe"
top = 20
```

Command-line options **add to** a screen (`--where`, `--sector`,
`--exclude-sector`) or **override** it (`--top`, `--sort-by`, `--signal`,
…), so `--screen quality-value --top 5 --sector Energy` works as expected.

### Filter the whole market in seconds: `--cache-only`

Fill the cache once (e.g. each morning), then re-filter as often as you like
without downloading anything:

```bash
# ~15-20 min for all ~11,700 US stocks and ETFs
python3 stock_screener.py --tickers-file ticker-lists/us-all.txt --signal all --workers 8 --quiet

# then, instantly
python3 stock_screener.py --tickers-file ticker-lists/us-all.txt --cache-only --quiet \
    --where "market_cap>=2B" --where "dollar_volume>=10M" --where "rsi<30" --sort-by rsi
```

`--cache-only` uses cached data of any age up to a day (or `--cache-minutes`,
if longer), never contacts Yahoo, and skips tickers that aren't cached. The
`Data:` line shows how old the oldest data is.

For a script that runs several screens over overlapping lists, download
everything once with `--prefetch` (fills the cache, no report), then run
each screen with `--cache-only`:

```bash
cat my-stocks.txt ticker-lists/sp500.txt ticker-lists/nasdaq100.txt > universe.txt
python3 stock_screener.py --tickers-file universe.txt --prefetch --workers 4 --quiet
python3 stock_screener.py --tickers-file my-stocks.txt --cache-only --signal all
python3 stock_screener.py --tickers-file ticker-lists/sp500.txt --cache-only --screen quality-value
```

Each ticker is downloaded once however many lists it's in, and every report
uses the same snapshot. A re-run within `--cache-minutes` skips the
downloads. About 4 workers is the sweet spot: Yahoo caps sustained
downloads at roughly 2 tickers a second, and more workers mostly trigger
rate-limit pauses.

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

## Parallel downloads

```bash
python3 stock_screener.py --tickers-file ticker-lists/sp500.txt --workers 8
```

`--workers N` downloads N tickers at a time (default 1). On 60 S&P 500
tickers, 8 workers took 4.7 s instead of 26 s. Results and the report keep
the original ticker order; progress lines appear as tickers finish. If Yahoo
rate-limits any request, *all* workers pause together before retrying. Going
much above 8 mostly just triggers those rate limits.

When screening several overlapping lists, run the biggest list first with
`--workers` (it fills the cache), then the rest — they'll mostly be served
from the cache. Use a `--cache-minutes` long enough to cover the first run.

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
entries older than a day are cleaned up automatically.

### Fundamentals cache

Each ticker used to cost Yahoo **4 requests**: price history, a quote, the
fundamentals, and the PEG ratio. Now:

- **Quotes** (price, P/E, market cap, pre/after-hours, earnings dates) are
  fetched **100 tickers per request**, always fresh.
- **Fundamentals** (sector, margins, debt, free cash flow, analyst targets,
  PEG…) are cached in `./cache/fundamentals/` for **7 days** for stocks and
  1 day for funds (fund NAV only comes with the fundamentals and changes
  daily). PEG and EV/EBITDA, which depend on the price, are rescaled to the
  current price.

On most days a stock now costs about **1 request** (its price history), so
big runs are several times faster under Yahoo's rate limit. Change the
window with `--fundamentals-days N` (`0` = always download them);
`--no-cache` turns this cache off too. Tickers Yahoo doesn't know are
dropped after the batched quote, without any further requests. Cached prices can be
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
