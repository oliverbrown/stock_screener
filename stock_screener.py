#!/usr/bin/env python3
"""
Stock Screener — no API key required
Pulls live data from Yahoo Finance via yfinance.
Supports major indices: S&P 500, Dow 30, NASDAQ 100, Russell 2000.

Usage:
    python stock_screener.py                          # default watchlist
    python stock_screener.py --index sp500            # all S&P 500 stocks
    python stock_screener.py --index dow30            # Dow Jones 30
    python stock_screener.py --index nasdaq100        # NASDAQ 100
    python stock_screener.py --index russell2000      # Russell 2000
    python stock_screener.py --tickers AAPL MSFT TSLA
    python stock_screener.py --index sp500 --signal oversold --max-pe 20 --output report.html
    python stock_screener.py --list-indices           # show all available indices

Requirements:
    pip install yfinance pandas
"""

import argparse
import sys
import os
import io
import time
import random
import urllib.request
from datetime import datetime

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    print("Missing dependencies. Run:  pip install yfinance pandas")
    sys.exit(1)


# ── Sector P/E benchmarks ─────────────────────────────────────────────────────
SECTOR_PE = {
    "Technology":             28,
    "Healthcare":             22,
    "Financial Services":     14,
    "Consumer Cyclical":      22,
    "Consumer Defensive":     22,
    "Energy":                 12,
    "Industrials":            20,
    "Utilities":              18,
    "Real Estate":            30,
    "Basic Materials":        15,
    "Communication Services": 20,
}
DEFAULT_SECTOR_PE = 20

# ── Default watchlist ─────────────────────────────────────────────────────────
DEFAULT_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "META", "AMZN",
    "NVDA", "JPM",  "WMT",   "XOM",  "JNJ",
    "V",    "PG",   "UNH",   "HD",   "MA",
]

# ── Bundled index tickers (static fallbacks) ──────────────────────────────────
# These are refreshed via Wikipedia scraping at runtime when possible.
# Last updated: mid-2025. Run --index <name> to get the live list.

DOW30_TICKERS = [
    "AAPL","AMGN","AXP","BA","CAT","CRM","CSCO","CVX","DIS","DOW",
    "GS","HD","HON","IBM","INTC","JNJ","JPM","KO","MCD","MMM",
    "MRK","MSFT","NKE","PG","TRV","UNH","V","VZ","WBA","WMT",
]

NASDAQ100_TICKERS = [
    "AAPL","ABNB","ADBE","ADI","ADP","ADSK","AEP","AMAT","AMD","AMGN",
    "AMZN","ANSS","APP","ARM","ASML","AVGO","AXON","AZN","BIIB","BKNG",
    "BKR","CCEP","CDNS","CDW","CEG","CHTR","CMCSA","COST","CPRT","CRWD",
    "CSCO","CSGP","CSX","CTAS","CTSH","DASH","DDOG","DLTR","DXCM","EA",
    "EXC","FANG","FAST","FTNT","GEHC","GFS","GILD","GOOG","GOOGL","HON",
    "IDXX","ILMN","INTC","INTU","ISRG","KDP","KHC","KLAC","LRCX","LULU",
    "MAR","MCHP","MDB","MDLZ","META","MNST","MRNA","MRVL","MSFT","MU",
    "NFLX","NVDA","NXPI","ODFL","ON","ORLY","PANW","PAYX","PCAR","PDD",
    "PEP","PYPL","QCOM","REGN","ROP","ROST","SBUX","SIRI","SMCI","SNPS",
    "SPLK","TEAM","TMUS","TSLA","TTD","TTWO","TXN","VRSK","VRTX","WBD",
]

SP500_TICKERS = [
    "A","AAL","AAP","AAPL","ABBV","ABC","ABMD","ABT","ACN","ADBE","ADI","ADM",
    "ADP","ADSK","AEE","AEP","AES","AFL","AIG","AIZ","AJG","AKAM","ALB","ALK",
    "ALL","ALLE","AMAT","AMCR","AMD","AME","AMGN","AMP","AMT","AMZN","ANET",
    "ANF","ANSS","AON","AOS","APA","APD","APH","APTV","ARE","ATO","AVB","AVGO",
    "AVY","AWK","AXP","AYI","AZO","BA","BAC","BAX","BBWI","BBY","BDX","BEN",
    "BF.B","BIO","BK","BKNG","BKR","BLK","BMY","BR","BRK.B","BRO","BSX","BWA",
    "BXP","C","CAG","CAH","CARR","CAT","CB","CBOE","CBRE","CCI","CCL","CDNS",
    "CDW","CE","CEG","CF","CFG","CHD","CHRW","CHTR","CI","CINF","CL","CLX",
    "CMA","CMCSA","CME","CMG","CMI","CMS","CNC","CNP","COF","COO","COP","COST",
    "CPB","CPRT","CPT","CRL","CRM","CSCO","CSGP","CSX","CTAS","CTLT","CTRA",
    "CTSH","CTVA","CVS","CVX","CZR","D","DAL","DD","DE","DG","DGX","DHI","DHR",
    "DIS","DISH","DLR","DLTR","DOV","DOW","DPZ","DRI","DTE","DUK","DVA","DVN",
    "DXC","DXCM","EA","EBAY","ECL","ED","EFX","EIX","EL","EMN","EMR","ENPH",
    "EOG","EPAM","EQIX","EQR","EQT","ES","ESS","ETN","ETR","ETSY","EVRG","EW",
    "EXC","EXR","F","FANG","FAST","FCX","FDS","FDX","FE","FFIV","FIS","FISV",
    "FITB","FLT","FMC","FOX","FOXA","FRT","FTV","GD","GE","GILD","GIS","GL",
    "GLW","GM","GNRC","GOOG","GOOGL","GPC","GPN","GRMN","GS","GWW","HAL","HAS",
    "HBAN","HCA","HD","HES","HIG","HII","HLT","HOLX","HON","HPE","HPQ","HRL",
    "HSIC","HST","HSY","HUM","HWM","IBM","ICE","IDXX","IEX","IFF","ILMN","INCY",
    "INTC","INTU","INVH","IP","IPG","IQV","IR","IRM","ISRG","IT","ITW","IVZ",
    "J","JBHT","JCI","JKHY","JNJ","JNPR","JPM","K","KEY","KEYS","KHC","KIM",
    "KLAC","KMB","KMI","KMX","KO","KR","L","LDOS","LEN","LH","LHX","LIN",
    "LKQ","LLY","LMT","LNC","LNT","LOW","LRCX","LUMN","LUV","LVS","LW","LYB",
    "LYV","MA","MAA","MAR","MAS","MCD","MCHP","MCK","MCO","MDLZ","MDT","MET",
    "META","MGM","MHK","MKC","MKTX","MLM","MMC","MMM","MNST","MO","MOH","MOS",
    "MPC","MPWR","MRK","MRNA","MRO","MS","MSCI","MSFT","MSI","MTB","MTCH","MTD",
    "MU","NCLH","NDAQ","NEE","NEM","NFLX","NI","NKE","NOC","NOW","NRG","NSC",
    "NTAP","NTRS","NUE","NVDA","NVR","NWL","NWS","NWSA","NXPI","O","ODFL","OKE",
    "OMC","ON","ORCL","ORLY","OTIS","OXY","PARA","PAYC","PAYX","PCAR","PCG",
    "PEAK","PEG","PEP","PFE","PFG","PG","PGR","PH","PHM","PKG","PLD","PM",
    "PNC","PNR","PNW","POOL","PPG","PPL","PRU","PSA","PSX","PTC","PWR","PXD",
    "PYPL","QCOM","QRVO","RCL","RE","REG","REGN","RF","RJF","RL","RMD","ROK",
    "ROL","ROP","ROST","RSG","RTX","SBAC","SBUX","SEDG","SEE","SHW","SIVB",
    "SJM","SLB","SNA","SNPS","SO","SPG","SPGI","SRE","STE","STT","STX","STZ",
    "SWK","SWKS","SYF","SYK","SYY","T","TAP","TDG","TDY","TECH","TEL","TER",
    "TFC","TFX","TGT","TJX","TMO","TMUS","TPR","TRGP","TRMB","TROW","TRV",
    "TSCO","TSLA","TSN","TT","TTWO","TXN","TXT","TYL","UAL","UDR","UHS","ULTA",
    "UNH","UNP","UPS","URI","USB","V","VFC","VLO","VMC","VNO","VRSK","VRSN",
    "VRTX","VTR","VTRS","VZ","WAB","WAT","WBA","WBD","WEC","WELL","WFC","WHR",
    "WM","WMB","WMT","WRB","WRK","WST","WTW","WY","WYNN","XEL","XOM","XRAY",
    "XYL","YUM","ZBH","ZBRA","ZION","ZTS",
]

# Russell 2000: too large to bundle fully (2000 tickers).
# We include a representative 200-stock sample from the index.
RUSSELL2000_SAMPLE = [
    "ACLS","ACLX","ACMR","ACT","AEHR","AEYE","AGIO","AGYS","AIOT","AIRG",
    "AKRO","ALEC","ALGM","ALLO","ALNT","ALRM","AMBC","AMEH","AMKR","AMPH",
    "AMRX","AMSC","AMSF","AMTB","AMWD","ANAB","ANIP","AORT","APLT","APOG",
    "APPF","AQST","ARLO","AROC","ARQT","ARTNA","ARVN","ASAI","ASGN","ASIX",
    "ASLE","ASND","ASRV","ASTE","ATEX","ATHI","ATRO","ATSG","ATTO","AUPH",
    "AVAH","AVNS","AVPT","AXNX","AXSM","AZEK","AZTA","BANF","BANR","BCAL",
    "BCML","BCOV","BCPC","BDTX","BEAT","BFIN","BFLY","BFS","BGFV","BGSF",
    "BHVN","BIRD","BJRI","BKRT","BKSY","BLFS","BLMN","BLND","BMBL","BOOT",
    "BOWL","BRC","BRKL","BSIG","BSVN","BVS","CAKE","CALX","CARE","CATO",
    "CBRL","CCBG","CCEC","CCRN","CDMO","CECO","CENT","CEVA","CHCO","CHCT",
    "CHEF","CHGG","CHMG","CHPT","CHRS","CIFR","CIGI","CINC","CIVB","CIVITAS",
    "CLAR","CLDX","CLFD","CLW","CLXT","CMCO","CMLS","CMPO","CNMD","CNSL",
    "CNTY","CODI","COHN","COHU","COLB","COMP","COOK","COOP","CORT","CPNG",
    "CPRX","CRBP","CRCT","CRDO","CRIS","CROX","CRVL","CSBR","CSGS","CSWI",
    "CTBI","CTKB","CTLP","CTMX","CTOS","CTRI","CURI","CVI","CVLG","CVLT",
    "CWH","CXDO","CXM","CYCN","CYTH","DENN","DFIN","DFH","DGII","DIOD",
    "DJCO","DLB","DLHC","DMTK","DNOW","DOMO","DPW","DSGN","DSS","DSTL",
    "DTIL","DXPE","DY","EBIX","ECPG","EGHT","EGP","ELME","ELSE","ELTK",
    "EMBC","EMKR","EMTX","ENOV","ENVA","ENVX","EPIX","EPRT","EQBK","EQRX",
]


# ── Index registry ────────────────────────────────────────────────────────────
INDICES = {
    "sp500": {
        "label":   "S&P 500",
        "wiki":    "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "table":   0,
        "col":     "Symbol",
        "static":  SP500_TICKERS,
    },
    "dow30": {
        "label":   "Dow Jones Industrial Average (Dow 30)",
        "wiki":    "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average",
        "table":   1,
        "col":     "Symbol",
        "static":  DOW30_TICKERS,
    },
    "nasdaq100": {
        "label":   "NASDAQ-100",
        "wiki":    "https://en.wikipedia.org/wiki/Nasdaq-100",
        "table":   4,
        "col":     "Ticker",
        "static":  NASDAQ100_TICKERS,
    },
    "russell2000": {
        "label":   "Russell 2000 (200-stock representative sample)",
        "wiki":    None,   # no reliable Wikipedia table
        "static":  RUSSELL2000_SAMPLE,
    },
}


# ── Index loader ──────────────────────────────────────────────────────────────
def load_index(name: str) -> list[str]:
    """Return constituent tickers for a named index. Tries Wikipedia first,
    falls back to the bundled static list."""
    idx = INDICES[name]
    wiki_url = idx.get("wiki")

    if wiki_url:
        try:
            req = urllib.request.Request(
                wiki_url,
                headers={"User-Agent": "Mozilla/5.0 (stock-screener/1.0)"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                html = r.read()
            tables = pd.read_html(io.BytesIO(html))
            tbl    = tables[idx["table"]]
            col    = idx["col"]
            tickers = tbl[col].str.replace(".", "-", regex=False).dropna().tolist()
            if tickers:
                print(f"  ✓ Loaded {len(tickers)} tickers live from Wikipedia ({idx['label']})")
                return tickers
        except Exception as e:
            print(f"  ⚠ Wikipedia fetch failed ({e}), using bundled list.")

    static = idx["static"]
    print(f"  Using bundled list: {len(static)} tickers ({idx['label']})")
    return static


# ── RSI ───────────────────────────────────────────────────────────────────────
def compute_rsi(prices: pd.Series, period: int = 14) -> float:
    if len(prices) < period + 1:
        return float("nan")
    delta    = prices.diff().dropna()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss.replace(0, float("nan"))
    rsi      = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 1)


# ── Fetch one ticker ──────────────────────────────────────────────────────────
def fetch_stock(ticker: str) -> dict | None:
    try:
        t    = yf.Ticker(ticker)
        info = t.info
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not price:
            return None
        hist = t.history(period="1mo", auto_adjust=True)
        if hist.empty or len(hist) < 2:
            return None
        rsi      = compute_rsi(hist["Close"])
        change1d = round((hist["Close"].iloc[-1] / hist["Close"].iloc[-2] - 1) * 100, 2)
        pe       = info.get("trailingPE") or info.get("forwardPE")
        pb       = info.get("priceToBook")
        return {
            "ticker":   ticker,
            "name":     info.get("longName") or info.get("shortName", ticker),
            "sector":   info.get("sector", "Unknown"),
            "price":    round(float(price), 2),
            "change1d": change1d,
            "pe":       round(float(pe), 1) if pe else None,
            "pb":       round(float(pb), 2) if pb else None,
            "rsi":      rsi,
        }
    except Exception:
        return None


# ── Signal classifier ─────────────────────────────────────────────────────────
def classify_signal(stock: dict, max_pe: float | None) -> tuple[str, str]:
    pe, pb, rsi, sector = stock["pe"], stock["pb"], stock["rsi"], stock["sector"]
    is_oversold    = isinstance(rsi, float) and not pd.isna(rsi) and rsi < 35
    sector_avg_pe  = SECTOR_PE.get(sector, DEFAULT_SECTOR_PE)
    is_undervalued = (
        (pe is not None and pe < sector_avg_pe * 0.80) or
        (pb is not None and pb < 1.5 and (pe is None or pe < sector_avg_pe))
    )
    if max_pe is not None and pe is not None and pe > max_pe:
        is_undervalued = False

    reasons = []
    if is_undervalued:
        parts = []
        if pe is not None:
            parts.append(f"P/E {pe}x vs sector avg ~{sector_avg_pe}x")
        if pb is not None and pb < 1.5:
            parts.append(f"P/B {pb}x")
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
    tickers:       list[str],
    signal_filter: str        = "both",
    max_pe:        float | None = None,
    verbose:       bool        = True,
    batch_size:    int         = 20,
    delay:         float       = 1.0,
) -> tuple[list[dict], int]:
    """
    Screen tickers in batches to avoid rate-limiting from Yahoo Finance.
    Returns (matched_stocks, total_analyzed).
    """
    results = []
    total   = len(tickers)
    icons   = {"undervalued": "📉", "oversold": "⚠️", "both": "🔥", "neutral": "  "}

    for i, ticker in enumerate(tickers, 1):
        if verbose:
            print(f"  [{i:>4}/{total}] {ticker:<8}", end="", flush=True)

        stock = fetch_stock(ticker)
        if stock is None:
            if verbose:
                print("skipped")
            continue

        signal, thesis = classify_signal(stock, max_pe)
        stock["signal"] = signal
        stock["thesis"] = thesis
        results.append(stock)

        if verbose:
            icon = icons.get(signal, "  ")
            pe_s = f"P/E {stock['pe']}x" if stock["pe"] else "P/E N/A"
            rsi_s = f"RSI {stock['rsi']}" if stock["rsi"] else ""
            print(f"{icon} {signal:<12}  {pe_s}  {rsi_s}")

        # Polite delay every batch_size tickers to avoid rate limits
        if i % batch_size == 0 and i < total:
            if verbose:
                print(f"\n  Pausing {delay}s to respect rate limits…\n")
            time.sleep(delay)

    # Apply signal filter
    if signal_filter == "both":
        matched = [r for r in results if r["signal"] in ("undervalued", "oversold", "both")]
    elif signal_filter in ("undervalued", "oversold"):
        matched = [r for r in results if r["signal"] in (signal_filter, "both")]
    else:
        matched = results

    return matched, len(results)


# ── Terminal report ───────────────────────────────────────────────────────────
def print_report(stocks: list[dict], screened: int) -> None:
    W = 68
    print("\n" + "═" * W)
    print(f"  STOCK SCREENER REPORT   {datetime.now().strftime('%B %d, %Y  %H:%M')}")
    print(f"  {len(stocks)} result(s) matched  ·  {screened} tickers analyzed")
    print("═" * W)
    if not stocks:
        print("  No stocks matched the selected filters.\n")
        return
    labels = {"undervalued": "UNDERVALUED", "oversold": "OVERSOLD",
              "both": "UNDERVALUED + OVERSOLD", "neutral": "NEUTRAL"}
    for s in stocks:
        chg_s  = f"{'+' if s['change1d'] >= 0 else ''}{s['change1d']}%"
        pe_s   = f"{s['pe']}x"  if s["pe"]  is not None else "N/A"
        pb_s   = f"{s['pb']}x"  if s["pb"]  is not None else "N/A"
        rsi_s  = str(s["rsi"]) if s["rsi"] is not None else "N/A"
        print(f"\n  {s['ticker']:6}  {s['name']}")
        print(f"  {'─' * (W - 2)}")
        print(f"  Price : ${s['price']:<10.2f}  Today : {chg_s}")
        print(f"  P/E   : {pe_s:<10}  P/B   : {pb_s}")
        print(f"  RSI   : {rsi_s:<10}  Sector: {s['sector']}")
        print(f"  Signal: {labels.get(s['signal'], '')}")
        print(f"  {s['thesis']}")
    print("\n" + "═" * W)
    print("  ⚠  Not financial advice. Always do your own research.\n")


# ── HTML report ───────────────────────────────────────────────────────────────
def save_html_report(stocks: list[dict], screened: int, path: str,
                     index_label: str = "") -> None:
    date_str = datetime.now().strftime("%B %d, %Y  %H:%M")
    badge = {
        "undervalued": ("Undervalued",            "#f0fdf4", "#16a34a"),
        "oversold":    ("Oversold",                "#fffbeb", "#d97706"),
        "both":        ("Undervalued + Oversold",  "#eff6ff", "#2563eb"),
        "neutral":     ("Neutral",                 "#f5f5f3", "#6b6b67"),
    }
    cards = ""
    for s in stocks:
        label, bg, fg = badge.get(s["signal"], ("Neutral", "#f5f5f3", "#6b6b67"))
        chg_c  = "#16a34a" if s["change1d"] >= 0 else "#dc2626"
        chg_s  = f"{'+' if s['change1d'] >= 0 else ''}{s['change1d']}%"
        pe_s   = f"{s['pe']}x"  if s["pe"]  is not None else "N/A"
        pb_s   = f"{s['pb']}x"  if s["pb"]  is not None else "N/A"
        rsi_s  = str(s["rsi"]) if s["rsi"] is not None else "N/A"
        cards += f"""
        <div class="card">
          <div class="card-top">
            <div>
              <div class="sname">{s['ticker']} <span class="price">${s['price']:.2f}</span></div>
              <div class="ssub">{s['name']} &middot; {s['sector']}</div>
            </div>
            <span class="badge" style="background:{bg};color:{fg}">{label}</span>
          </div>
          <div class="metrics">
            <div><div class="ml">P/E ratio</div><div class="mv">{pe_s}</div></div>
            <div><div class="ml">P/B ratio</div><div class="mv">{pb_s}</div></div>
            <div><div class="ml">RSI (14d)</div><div class="mv">{rsi_s}</div></div>
            <div><div class="ml">Today</div><div class="mv" style="color:{chg_c}">{chg_s}</div></div>
          </div>
          <div class="thesis">{s['thesis']}</div>
        </div>"""

    subtitle = f"{index_label} &middot; " if index_label else ""
    empty = "" if cards else \
        '<p style="color:#6b6b67;text-align:center;padding:2rem">No stocks matched the selected filters.</p>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stock Screener — {date_str}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
      background:#f5f5f3;color:#1a1a18;min-height:100vh;padding:2rem 1rem}}
.wrap{{max-width:780px;margin:0 auto}}
header{{margin-bottom:1.5rem}}
h1{{font-size:22px;font-weight:500;margin-bottom:4px}}
.sub{{font-size:13px;color:#6b6b67}}
.summary{{font-size:13px;color:#6b6b67;margin-bottom:1rem;
           display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px}}
.card{{background:#fff;border:0.5px solid rgba(0,0,0,.1);border-radius:12px;
        padding:14px 16px;margin-bottom:10px}}
.card-top{{display:flex;justify-content:space-between;align-items:flex-start;
            gap:8px;margin-bottom:10px}}
.sname{{font-size:15px;font-weight:500}}
.price{{font-weight:400}}
.ssub{{font-size:12px;color:#6b6b67;margin-top:2px}}
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
    <p class="sub">{subtitle}Generated {date_str}</p>
  </header>
  <div class="summary">
    <strong>{len(stocks)} result(s) matched</strong>
    <span>{screened} tickers analyzed</span>
  </div>
  {cards}{empty}
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
        description="Stock screener — undervalued & oversold signals from Yahoo Finance.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python stock_screener.py                            # default 15-stock watchlist
  python stock_screener.py --index sp500              # all ~500 S&P stocks
  python stock_screener.py --index dow30              # Dow 30
  python stock_screener.py --index nasdaq100          # NASDAQ 100
  python stock_screener.py --index russell2000        # Russell 2000 sample
  python stock_screener.py --index sp500 --signal oversold --max-pe 20
  python stock_screener.py --tickers AAPL MSFT TSLA --output report.html
  python stock_screener.py --list-indices
        """
    )
    parser.add_argument("--index",   choices=list(INDICES.keys()), default=None,
                        help="Screen an entire index")
    parser.add_argument("--tickers", nargs="+", default=None, metavar="TICK",
                        help="Custom space-separated tickers (overrides --index and default list)")
    parser.add_argument("--signal",  choices=["both","undervalued","oversold","all"],
                        default="both",
                        help="Signal filter (default: both)")
    parser.add_argument("--max-pe",  type=float, default=None, metavar="N",
                        help="Only include stocks with P/E below N")
    parser.add_argument("--output",  type=str, default=None, metavar="FILE.html",
                        help="Save HTML report to this file")
    parser.add_argument("--list-indices", action="store_true",
                        help="Show available indices and exit")
    args = parser.parse_args()

    if args.list_indices:
        print("\nAvailable indices:\n")
        for key, meta in INDICES.items():
            n = len(meta["static"])
            print(f"  --index {key:<14}  {meta['label']}  ({n} bundled tickers)")
        print()
        return

    # Resolve ticker list
    index_label = ""
    if args.tickers:
        tickers = [t.upper().strip() for t in args.tickers]
    elif args.index:
        index_label = INDICES[args.index]["label"]
        print(f"\nLoading index: {index_label}")
        tickers = load_index(args.index)
    else:
        tickers = DEFAULT_TICKERS

    print(f"\nStock screener  ·  {datetime.now().strftime('%B %d, %Y')}")
    print(f"Index   : {index_label or 'custom/default watchlist'}")
    print(f"Tickers : {len(tickers)} to analyze")
    print(f"Signal  : {args.signal}")
    print(f"Max P/E : {args.max_pe or 'none'}")
    print(f"Output  : {args.output or 'terminal only'}")
    print(f"\nFetching live data from Yahoo Finance…\n")

    stocks, screened = run_screen(
        tickers       = tickers,
        signal_filter = args.signal,
        max_pe        = args.max_pe,
        verbose       = True,
    )

    print_report(stocks, screened)

    if args.output:
        save_html_report(stocks, screened, args.output, index_label)
        print(f"  HTML report saved → {os.path.abspath(args.output)}\n")


if __name__ == "__main__":
    main()
