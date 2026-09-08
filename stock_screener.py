#!/usr/bin/env python3
"""
Stock Screener — no API key required
Pulls live data from Yahoo Finance via yfinance.

Usage:
    python stock_screener.py
    python stock_screener.py --tickers AAPL MSFT GOOGL
    python stock_screener.py --signal undervalued --max-pe 20
    python stock_screener.py --output report.html

Requirements:
    pip install yfinance pandas
"""

import argparse
import sys
import os
from datetime import datetime

# ── Dependency check ──────────────────────────────────────────────────────────
try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    print("Missing dependencies. Run:  pip install yfinance pandas")
    sys.exit(1)


# ── Default watchlist ─────────────────────────────────────────────────────────
DEFAULT_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "META", "AMZN",
    "NVDA", "JPM", "WMT", "XOM", "JNJ",
    "V",    "PG",  "UNH",  "HD",  "MA",
]

# Rough sector-average P/E benchmarks (used for undervalued signal)
SECTOR_PE = {
    "Technology":           28,
    "Healthcare":           22,
    "Financial Services":   14,
    "Consumer Cyclical":    22,
    "Consumer Defensive":   22,
    "Energy":               12,
    "Industrials":          20,
    "Utilities":            18,
    "Real Estate":          30,
    "Basic Materials":      15,
    "Communication Services": 20,
}
DEFAULT_SECTOR_PE = 20   # fallback when sector not in table


# ── RSI calculation ───────────────────────────────────────────────────────────
def compute_rsi(prices: pd.Series, period: int = 14) -> float:
    """Return the most-recent RSI value for a price series."""
    if len(prices) < period + 1:
        return float("nan")
    delta = prices.diff().dropna()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs   = avg_gain / avg_loss.replace(0, float("nan"))
    rsi  = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 1)


# ── Fetch one ticker ──────────────────────────────────────────────────────────
def fetch_stock(ticker: str) -> dict | None:
    """Return a dict of fundamentals + technicals, or None on failure."""
    try:
        t    = yf.Ticker(ticker)
        info = t.info

        # Skip if no price data (delisted / bad ticker)
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not price:
            return None

        # 1-month history for RSI and 1-day change
        hist = t.history(period="1mo", auto_adjust=True)
        if hist.empty or len(hist) < 2:
            return None

        rsi      = compute_rsi(hist["Close"])
        change1d = round((hist["Close"].iloc[-1] / hist["Close"].iloc[-2] - 1) * 100, 2)

        pe   = info.get("trailingPE")   or info.get("forwardPE")
        pb   = info.get("priceToBook")
        name = info.get("longName")     or info.get("shortName", ticker)
        sector = info.get("sector",     "Unknown")

        return {
            "ticker":   ticker,
            "name":     name,
            "sector":   sector,
            "price":    round(float(price), 2),
            "change1d": change1d,
            "pe":       round(float(pe), 1) if pe else None,
            "pb":       round(float(pb), 2) if pb else None,
            "rsi":      rsi,
        }
    except Exception:
        return None


# ── Signal logic ──────────────────────────────────────────────────────────────
def classify_signal(stock: dict, max_pe: float | None) -> tuple[str, str]:
    """
    Return (signal, thesis) where signal is one of:
    'undervalued', 'oversold', 'both', 'neutral'
    """
    pe     = stock["pe"]
    pb     = stock["pb"]
    rsi    = stock["rsi"]
    sector = stock["sector"]

    is_oversold    = isinstance(rsi, float) and rsi < 35
    sector_pe_avg  = SECTOR_PE.get(sector, DEFAULT_SECTOR_PE)
    is_undervalued = (
        (pe is not None and pe < sector_pe_avg * 0.80) or
        (pb is not None and pb < 1.5 and (pe is None or pe < sector_pe_avg))
    )

    # Apply user P/E cap
    if max_pe is not None and pe is not None and pe > max_pe:
        is_undervalued = False

    reasons = []
    if is_undervalued:
        parts = []
        if pe is not None:
            parts.append(f"P/E of {pe}x vs sector avg ~{sector_pe_avg}x")
        if pb is not None and pb < 1.5:
            parts.append(f"P/B of {pb}x")
        reasons.append("Undervalued: " + ", ".join(parts) + ".")

    if is_oversold:
        reasons.append(f"Oversold: RSI at {rsi} signals excess selling pressure.")

    if not reasons:
        reasons.append("No strong undervalued or oversold signal at current levels.")

    if is_undervalued and is_oversold:
        signal = "both"
    elif is_undervalued:
        signal = "undervalued"
    elif is_oversold:
        signal = "oversold"
    else:
        signal = "neutral"

    return signal, " ".join(reasons)


# ── Screen runner ─────────────────────────────────────────────────────────────
def run_screen(
    tickers:    list[str],
    signal_filter: str    = "both",
    max_pe:    float | None = None,
    verbose:   bool        = True,
) -> list[dict]:

    results = []
    total   = len(tickers)

    for i, ticker in enumerate(tickers, 1):
        if verbose:
            print(f"  [{i}/{total}] {ticker}...", end="", flush=True)
        stock = fetch_stock(ticker)
        if stock is None:
            if verbose:
                print(" skipped (no data)")
            continue

        signal, thesis = classify_signal(stock, max_pe)
        stock["signal"] = signal
        stock["thesis"] = thesis
        results.append(stock)

        if verbose:
            tag = {"undervalued": "📉", "oversold": "⚠️", "both": "🔥", "neutral": "  "}.get(signal, "  ")
            print(f" {tag} {signal}")

    # Apply filter
    if signal_filter == "both":
        filtered = [r for r in results if r["signal"] in ("undervalued", "oversold", "both")]
    elif signal_filter in ("undervalued", "oversold"):
        filtered = [r for r in results if r["signal"] in (signal_filter, "both")]
    else:
        filtered = results  # "all"

    return filtered, len(results)


# ── Terminal report ───────────────────────────────────────────────────────────
def print_report(stocks: list[dict], screened: int) -> None:
    date_str = datetime.now().strftime("%B %d, %Y")
    print("\n" + "═" * 66)
    print(f"  STOCK SCREENER REPORT   {date_str}")
    print(f"  {len(stocks)} result(s) from {screened} analyzed")
    print("═" * 66)

    if not stocks:
        print("  No stocks matched the selected filters.\n")
        return

    for s in stocks:
        tag = {"undervalued": "UNDERVALUED", "oversold": "OVERSOLD",
               "both": "UNDERVALUED + OVERSOLD", "neutral": "NEUTRAL"}.get(s["signal"], "")
        chg_sign = "+" if s["change1d"] >= 0 else ""
        pe_str   = f"{s['pe']}x"  if s["pe"]  is not None else "N/A"
        pb_str   = f"{s['pb']}x"  if s["pb"]  is not None else "N/A"
        rsi_str  = str(s["rsi"])  if s["rsi"] is not None else "N/A"

        print(f"\n  {s['ticker']:6}  {s['name']}")
        print(f"  {'─'*62}")
        print(f"  Price:  ${s['price']:<10.2f}  Today: {chg_sign}{s['change1d']}%")
        print(f"  P/E:    {pe_str:<10}  P/B:   {pb_str}")
        print(f"  RSI:    {rsi_str:<10}  Sector: {s['sector']}")
        print(f"  Signal: {tag}")
        print(f"  {s['thesis']}")

    print("\n" + "═" * 66)
    print("  ⚠  Not financial advice. Do your own research.\n")


# ── HTML report ───────────────────────────────────────────────────────────────
def save_html_report(stocks: list[dict], screened: int, path: str) -> None:
    date_str = datetime.now().strftime("%B %d, %Y  %H:%M")

    badge_styles = {
        "undervalued": ("Undervalued",        "#f0fdf4", "#16a34a"),
        "oversold":    ("Oversold",            "#fffbeb", "#d97706"),
        "both":        ("Undervalued + Oversold", "#eff6ff", "#2563eb"),
        "neutral":     ("Neutral",             "#f5f5f3", "#6b6b67"),
    }

    cards_html = ""
    for s in stocks:
        label, bg, fg = badge_styles.get(s["signal"], ("Neutral", "#f5f5f3", "#6b6b67"))
        chg_color = "#16a34a" if s["change1d"] >= 0 else "#dc2626"
        chg_sign  = "+" if s["change1d"] >= 0 else ""
        pe_str    = f"{s['pe']}x"  if s["pe"]  is not None else "N/A"
        pb_str    = f"{s['pb']}x"  if s["pb"]  is not None else "N/A"
        rsi_str   = str(s["rsi"]) if s["rsi"] is not None else "N/A"

        cards_html += f"""
        <div class="card">
          <div class="card-top">
            <div>
              <div class="stock-name">{s['ticker']} <span class="price">${s['price']:.2f}</span></div>
              <div class="stock-sub">{s['name']} &middot; {s['sector']}</div>
            </div>
            <span class="badge" style="background:{bg};color:{fg}">{label}</span>
          </div>
          <div class="metrics">
            <div class="metric"><div class="ml">P/E ratio</div><div class="mv">{pe_str}</div></div>
            <div class="metric"><div class="ml">P/B ratio</div><div class="mv">{pb_str}</div></div>
            <div class="metric"><div class="ml">RSI (14d)</div><div class="mv">{rsi_str}</div></div>
            <div class="metric"><div class="ml">Today</div>
              <div class="mv" style="color:{chg_color}">{chg_sign}{s['change1d']}%</div></div>
          </div>
          <div class="thesis">{s['thesis']}</div>
        </div>"""

    if not cards_html:
        cards_html = '<p style="color:#6b6b67;text-align:center;padding:2rem">No stocks matched the selected filters.</p>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stock Screener Report — {date_str}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
        background:#f5f5f3;color:#1a1a18;min-height:100vh;padding:2rem 1rem}}
  .wrap{{max-width:760px;margin:0 auto}}
  header{{margin-bottom:1.5rem}}
  header h1{{font-size:22px;font-weight:500;margin-bottom:4px}}
  header p{{font-size:13px;color:#6b6b67}}
  .summary{{font-size:13px;color:#6b6b67;margin-bottom:1rem;
             display:flex;justify-content:space-between}}
  .card{{background:#fff;border:0.5px solid rgba(0,0,0,.1);border-radius:12px;
          padding:14px 16px;margin-bottom:10px}}
  .card-top{{display:flex;justify-content:space-between;align-items:flex-start;
              gap:8px;margin-bottom:10px}}
  .stock-name{{font-size:15px;font-weight:500}}
  .price{{font-weight:400}}
  .stock-sub{{font-size:12px;color:#6b6b67;margin-top:2px}}
  .badge{{font-size:11px;padding:3px 9px;border-radius:6px;font-weight:500;
           white-space:nowrap;flex-shrink:0}}
  .metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;
             padding:10px 0;border-top:0.5px solid rgba(0,0,0,.1);
             border-bottom:0.5px solid rgba(0,0,0,.1);margin-bottom:10px}}
  .ml{{font-size:10px;color:#9f9f9b;margin-bottom:3px}}
  .mv{{font-size:13px;font-weight:500}}
  .thesis{{font-size:12px;color:#6b6b67;line-height:1.65}}
  .disc{{font-size:11px;color:#9f9f9b;text-align:center;margin-top:1.5rem;line-height:1.6}}
  @media(max-width:500px){{.metrics{{grid-template-columns:repeat(2,1fr)}}}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Stock screener report</h1>
    <p>Generated {date_str}</p>
  </header>
  <div class="summary">
    <strong>{len(stocks)} result(s)</strong>
    <span>{screened} tickers analyzed</span>
  </div>
  {cards_html}
  <p class="disc">Not financial advice. For informational purposes only.<br>
  Always do your own research before making investment decisions.</p>
</div>
</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Stock screener — finds undervalued and oversold stocks using Yahoo Finance data."
    )
    parser.add_argument(
        "--tickers", nargs="+", default=DEFAULT_TICKERS,
        metavar="TICK",
        help="Space-separated ticker symbols (default: built-in watchlist of 15)"
    )
    parser.add_argument(
        "--signal", choices=["both", "undervalued", "oversold", "all"],
        default="both",
        help="Signal filter: both (default), undervalued, oversold, or all"
    )
    parser.add_argument(
        "--max-pe", type=float, default=None,
        metavar="N",
        help="Only include stocks with P/E below this value"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        metavar="FILE.html",
        help="Save an HTML report to this file (e.g. report.html)"
    )
    args = parser.parse_args()

    tickers = [t.upper().strip() for t in args.tickers]

    print(f"\nStock screener  ·  {datetime.now().strftime('%B %d, %Y')}")
    print(f"Tickers : {', '.join(tickers)}")
    print(f"Signal  : {args.signal}")
    print(f"Max P/E : {args.max_pe or 'none'}")
    print(f"Output  : {args.output or 'terminal only'}")
    print(f"\nFetching data from Yahoo Finance…\n")

    stocks, screened = run_screen(
        tickers       = tickers,
        signal_filter = args.signal,
        max_pe        = args.max_pe,
        verbose       = True,
    )

    print_report(stocks, screened)

    if args.output:
        save_html_report(stocks, screened, args.output)
        print(f"  HTML report saved → {os.path.abspath(args.output)}\n")


if __name__ == "__main__":
    main()
