#!/bin/bash
#
# run-scanner.sh — example runner for stock_screener.py
#
# Copy this to your own file (e.g. run-my-scanner.sh, which is gitignored via
# the my-* rules) and edit it to point at your own watchlists. This version
# uses only built-in indices and inline tickers so it runs as-is.
#
# Every run also writes a timestamped copy of its console output to ./logs/.
#
#   --signal   flagged | buy | sell | hold | all
#              | undervalued | oversold | overvalued | overbought
#   --asset-type  all | stock | etf        (auto-detected per ticker)
#   --ascii       plain-text icons (no emoji) — for terminals without an
#                 emoji font; also auto-enables when STOCK_SCREENER_ASCII is set
#
# With --index, the index name is folded into --output automatically:
#   --index dow30 --output daily-buy.html   ->   daily-buy-dow30.html
#
# Valid indices: sp500  dow30  nasdaq100  russell2000   (see --list-indices)

set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}

# ── 1. A quick custom watchlist (mix of stocks and ETFs) ──────────────────────
$PY stock_screener.py \
    --tickers AAPL MSFT NVDA KO XOM VOO QQQ TLT \
    --signal all \
    --output example-watchlist.html

# ── 2. Screen tickers from a file ────────────────────────────────────────────
# File format: symbols separated by any whitespace, newlines, or commas.
#   echo "AAPL, MSFT GOOGL
#   TSLA NVDA" > watchlist.txt
#
# $PY stock_screener.py --tickers-file watchlist.txt --signal sell \
#     --output watchlist-sell.html

# ── 3. Daily index scans — buy candidates ───────────────────────────────────
for idx in dow30 nasdaq100; do
    $PY stock_screener.py --index "$idx" --signal buy --output daily-buy.html
done

# ── 4. Daily index scans — sell / trim candidates (uncomment to enable) ─────
# for idx in sp500 dow30 nasdaq100; do
#     $PY stock_screener.py --index "$idx" --signal sell --output daily-sell.html
# done

# ── 5. ETFs only, from a mixed list ────────────────────────────────────────
# $PY stock_screener.py --tickers-file watchlist.txt --asset-type etf \
#     --signal all --output etfs.html

echo
echo "Done. HTML reports written to $(pwd); logs in ./logs/"
