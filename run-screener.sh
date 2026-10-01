#!/bin/bash
#
# run-screener.sh — example runner for stock_screener.py
#
# Copy this to your own file (e.g. run-my-screener.sh — any run-my-*.sh name
# is gitignored) and edit the settings below. As shipped it uses only an
# inline watch list and built-in index lists, so it runs as-is.
#
# It works in two steps:
#   1. Download every ticker it will use once, into the cache (--prefetch).
#      A ticker that appears in several lists is downloaded only once.
#   2. Run every report and saved screen from the cache (--cache-only):
#      no more downloads, a few seconds each, all from the same snapshot.
#
# Usage: ./run-screener.sh [OUTPUT_DIR]
#   OUTPUT_DIR  where the HTML reports and watchlists/ go (default: this
#               folder). Created if missing; relative to this script's folder.
#   e.g. from cron, weekdays at 9:30:
#     30 9 * * 1-5 cd /path/to/stock_screener && ./run-screener.sh screens >> screens/screener.log 2>&1
#
# Every run of the screener also writes its console output to ./logs/
# (logs older than 30 days are deleted automatically).

set -uo pipefail
cd "$(dirname "$0")"

OUT_DIR="${1:-.}"
mkdir -p "$OUT_DIR" || { echo "can't create output directory: $OUT_DIR"; exit 1; }

PY=${PYTHON:-python3}

# 24-hour HH_MM prefix, set once so every file from this run matches
TS=$(date +%H_%M)

# ── Settings ──────────────────────────────────────────────────────────────────
# Your watch list: tickers here, or point WATCHLIST_FILE at a file of them
# (separated by whitespace, newlines or commas; "#" starts a comment).
WATCHLIST="AAPL MSFT NVDA KO XOM VOO QQQ TLT"
WATCHLIST_FILE=""

# Index lists to screen (built into ticker-lists/ by build_ticker_lists.py):
#   dow30 nasdaq100 russell2000   sp500 sp400 sp600
#   nasdaq nyse nyse-american nyse-arca cboe   us-stocks us-etfs us-all
INDEXES="dow30 nasdaq100"

# Saved screens to run over those indexes (see: stock_screener.py --list-screens;
# add your own in my-screens.toml).
SCREENS="quality-value quality-dip oversold-accumulation momentum-leaders dividend-income"

WORKERS=4        # parallel downloads in step 1; more mostly triggers Yahoo's rate limits
CACHE_MIN=30     # data younger than this isn't downloaded again, so a re-run within
                 # CACHE_MIN minutes skips step 1's downloads entirely

# --signal options: flagged | buy | sell | hold | all
#                   | undervalued | oversold | overvalued | overbought | overextended


# ── Ticker lists ──────────────────────────────────────────────────────────────
# Refresh the index lists so each run screens current members. If the refresh
# fails, the lists from the last successful run are used.
$PY build_ticker_lists.py $INDEXES || echo "⚠ ticker list refresh failed; using existing lists"

mkdir -p ticker-lists
if [ -z "$WATCHLIST_FILE" ]; then
    WATCHLIST_FILE=ticker-lists/example-watchlist.txt
    echo "$WATCHLIST" > "$WATCHLIST_FILE"
fi


# ── Step 1: download everything once ─────────────────────────────────────────
UNIVERSE=ticker-lists/example-universe.txt
cat "$WATCHLIST_FILE" $(for idx in $INDEXES; do echo "ticker-lists/${idx}.txt"; done) > "$UNIVERSE"

$PY stock_screener.py --tickers-file "$UNIVERSE" --prefetch --quiet \
    --workers $WORKERS --cache-minutes $CACHE_MIN \
    || { echo "download step failed; see the newest log in ./logs/"; exit 1; }


# ── Step 2: every report from the cache ──────────────────────────────────────
SCREEN="$PY stock_screener.py --cache-only"

# Your watch list: everything, then just the trim candidates (sell signals)
$SCREEN --tickers-file "$WATCHLIST_FILE" --signal all --output "$OUT_DIR/${TS}_watchlist.html"
$SCREEN --tickers-file "$WATCHLIST_FILE" --screen trim-candidates --output "$OUT_DIR/${TS}_watchlist-sell.html"

# ETFs only, from a mixed list (uncomment to enable)
# $SCREEN --tickers-file "$WATCHLIST_FILE" --asset-type etf --signal all --output "$OUT_DIR/${TS}_watchlist-etfs.html"

# Each index: buy candidates
for idx in $INDEXES; do
    $SCREEN --tickers-file "ticker-lists/${idx}.txt" --signal buy --output "$OUT_DIR/${TS}_daily-buy-${idx}.html"
done

# Each index: sell / trim candidates (uncomment to enable)
# for idx in $INDEXES; do
#     $SCREEN --tickers-file "ticker-lists/${idx}.txt" --signal sell --output "$OUT_DIR/${TS}_daily-sell-${idx}.html"
# done

# Saved screens over all of INDEXES combined. Each keeps a timestamped report
# and overwrites its watch list, so OUT_DIR/watchlists/<screen>.txt is always
# the latest (and can be fed back in with --tickers-file).
INDEX_UNIVERSE=ticker-lists/example-indexes.txt
for idx in $INDEXES; do cat "ticker-lists/${idx}.txt"; done > "$INDEX_UNIVERSE"
mkdir -p "$OUT_DIR/watchlists"
for sc in $SCREENS; do
    $SCREEN --tickers-file "$INDEX_UNIVERSE" --screen "$sc" --quiet \
        --output "$OUT_DIR/${TS}_screen-${sc}.html" --save-tickers "$OUT_DIR/watchlists/${sc}.txt"
done

# A custom filter without a saved screen (uncomment to enable; see --list-fields)
# $SCREEN --tickers-file "$INDEX_UNIVERSE" --where "rsi<35" --where "market_cap>=10B" \
#     --sort-by rsi --top 10 --output "$OUT_DIR/${TS}_oversold-large-caps.html"

echo
echo "Done. Reports are in $(cd "$OUT_DIR" && pwd); watch lists in $(cd "$OUT_DIR" && pwd)/watchlists/; logs in $(pwd)/logs/"
