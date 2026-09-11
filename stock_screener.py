#!/usr/bin/env python3
"""
Stock Screener — no API key required
Pulls live data from Yahoo Finance via yfinance.
Supports major indices: S&P 500, Dow 30, NASDAQ 100, Russell 2000.

Stocks (equities):
    Buy side  — undervalued (P/E / P/B vs sector), oversold (RSI < 35)
    Sell side — overvalued (P/E vs sector, PEG > 2), overbought (RSI > 70),
                overextended (>20% above the 200-day moving average)
    Context   — 50/200-day trend, distance from 52-week high/low, and a
                value-trap guard (high debt / negative margins) that
                suppresses a stale "undervalued" call

ETFs / funds (auto-detected by quoteType):
    Buy side  — undervalued (discount to NAV, or holdings P/E below the
                fund's category benchmark), oversold (RSI / trend)
    Sell side — overvalued (premium to NAV, or holdings P/E above category),
                overbought, overextended
    hold      — no signal either way
    Context   — NAV premium/discount (always shown), distribution yield,
                expense ratio (a high-cost fund is not a "bargain"), 3y beta

Every run is also written to a timestamped log file in ./logs/.

Usage:
    python3 stock_screener.py                          # default watchlist
    python3 stock_screener.py --index sp500            # all S&P 500 stocks
    python3 stock_screener.py --index dow30 --signal sell
    python3 stock_screener.py --tickers AAPL MSFT TSLA
    python3 stock_screener.py --tickers-file watchlist.txt   # whitespace/comma/newline separated
    python3 stock_screener.py --tickers-file mixed.txt --asset-type etf   # ETFs only
    python3 stock_screener.py --tickers VOO QQQ SMH --signal hold
    python3 stock_screener.py --index sp500 --signal sell --ascii   # plain-ASCII output
    python3 stock_screener.py --index sp500 --output daily.html    # writes daily-sp500.html
    python3 stock_screener.py --list-indices           # show all available indices

Requirements:
    pip install yfinance pandas
"""

import argparse
import sys
import os
import io
import re
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


# ── Output mode ───────────────────────────────────────────────────────────────
# Emoji status icons render on macOS out of the box but need a color-emoji font
# on Linux. --ascii (or a non-UTF-8 / dumb terminal) swaps them for plain tags
# and folds box-drawing / dashes / check marks down to 7-bit ASCII.
ASCII_OUTPUT = False

_ASCII_SUBS = {
    "─": "-", "━": "-", "═": "=", "│": "|",
    "·": "-", "•": "*", "…": "...",
    "—": "-", "–": "-",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "→": "->", "←": "<-", "≈": "~", "±": "+/-",
    "✓": "OK", "✔": "OK", "✗": "x", "×": "x",
    "⚠": "!", "️": "",
    "\U0001f525": "*", "\U0001f4c8": "^", "\U0001f4c9": "v",
    "\U0001f6a9": "!", "\U0001f53a": "^", "\U0001f53b": "v",
    "❓": "?", "▲": "^", "▼": "v",
}
_ASCII_TABLE = str.maketrans(_ASCII_SUBS)


# ── Run logging ───────────────────────────────────────────────────────────────
class _Tee:
    """Fan writes out to several streams at once (console + log file),
    transliterating to ASCII first when ASCII_OUTPUT is on."""

    def __init__(self, *streams, ascii_only: bool = False):
        self._streams = streams
        self._ascii   = ascii_only

    def write(self, data):
        if self._ascii:
            data = data.translate(_ASCII_TABLE)
        for s in self._streams:
            s.write(data)

    def flush(self):
        for s in self._streams:
            s.flush()


def parse_tickers(text: str) -> list[str]:
    """Split a blob of ticker symbols on any run of whitespace, newlines,
    carriage returns, and/or commas. Upper-cased, de-duplicated, order kept."""
    seen, out = set(), []
    for tok in re.split(r"[,\s]+", text.strip()):
        sym = tok.strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def load_tickers_file(path: str) -> list[str]:
    """Read tickers from a file (see parse_tickers for the accepted format)."""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        sys.exit(f"Could not read tickers file: {e}")
    tickers = parse_tickers(text)
    if not tickers:
        sys.exit(f"No tickers found in {path}")
    return tickers


def resolve_ascii(flag: bool) -> bool:
    """Decide whether to emit plain ASCII: explicit --ascii, the
    STOCK_SCREENER_ASCII env var, a dumb/unset TERM, or a stdout encoding
    that isn't UTF-8 (redirected to a file, a legacy locale, …)."""
    if flag or os.environ.get("STOCK_SCREENER_ASCII"):
        return True
    if (os.environ.get("TERM") or "").lower() == "dumb":
        return True
    return "utf" not in (getattr(sys.stdout, "encoding", "") or "utf-8").lower()


def _open_run_log(log_dir: str = "logs"):
    """Create ./logs (if needed) and open a uniquely named, timestamped log.
    Returns (file_handle, path)."""
    os.makedirs(log_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path  = os.path.join(log_dir, f"screener_{stamp}.log")
    n = 1
    while os.path.exists(path):                       # same-second reruns
        path = os.path.join(log_dir, f"screener_{stamp}_{n}.log")
        n += 1
    return open(path, "w", encoding="utf-8", buffering=1), path


def resolve_output_path(output: str | None, index_key: str | None) -> str | None:
    """Fold the index name into the --output filename.

    - "{index}" anywhere in the name is replaced with the index slug
      (or "watchlist" when screening the default / custom list).
    - Otherwise, when an index is selected, the slug is inserted before
      the extension:  daily.html  --index sp500  ->  daily-sp500.html
    """
    if not output:
        return None
    slug = index_key or "watchlist"
    if "{index}" in output:
        return output.replace("{index}", slug)
    if index_key:
        root, ext = os.path.splitext(output)
        return f"{root}-{index_key}{ext or '.html'}"
    return output


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

# ── ETF category P/E benchmarks (yfinance "category" strings) ──────────────────
# Used to judge whether an equity fund's blended holdings P/E looks rich or
# cheap. Bond / commodity / preferred funds have no P/E and skip this entirely.
CATEGORY_PE = {
    "Large Blend":               21,
    "Large Growth":              28,
    "Large Value":               16,
    "Mid-Cap Blend":             18,
    "Mid-Cap Growth":            25,
    "Mid-Cap Value":             15,
    "Small Blend":               16,
    "Small Growth":              22,
    "Small Value":               14,
    "Technology":                28,
    "Health":                    20,
    "Financial":                 14,
    "Equity Energy":             12,
    "Industrials":               20,
    "Utilities":                 18,
    "Real Estate":               30,
    "Consumer Cyclical":         22,
    "Consumer Defensive":        22,
    "Communications":            20,
    "Foreign Large Blend":       15,
    "Foreign Large Growth":      19,
    "Foreign Large Value":       12,
    "Diversified Emerging Mkts": 14,
    "Focused Region":            13,
}
DEFAULT_CATEGORY_PE = 20

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
        hist = t.history(period="1y", auto_adjust=True)
        if hist.empty or len(hist) < 2:
            return None
        close    = hist["Close"]
        price    = float(price)
        rsi      = compute_rsi(close)
        change1d = round((close.iloc[-1] / close.iloc[-2] - 1) * 100, 2)

        # ── Trend: 50 / 200-day moving averages ──────────────────────────────
        ma50  = float(close.rolling(50).mean().iloc[-1])  if len(close) >= 50  else None
        ma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
        pct_vs_200 = round((price / ma200 - 1) * 100, 1) if ma200 else None
        trend = None
        if ma50 is not None and ma200 is not None:
            trend = "up" if ma50 >= ma200 else "down"

        # ── 52-week range position ──────────────────────────────────────────
        wk_high = info.get("fiftyTwoWeekHigh")
        wk_low  = info.get("fiftyTwoWeekLow")
        pct_off_high = round((price / wk_high - 1) * 100, 1) if wk_high else None
        pct_off_low  = round((price / wk_low  - 1) * 100, 1) if wk_low  else None

        quote_type = (info.get("quoteType") or "EQUITY").upper()
        is_fund    = quote_type != "EQUITY"

        if is_fund:
            # Only the fund's blended holdings P/E; ignore garbage forward values
            raw_pe = info.get("trailingPE")
            pe = raw_pe if (raw_pe and 0 < raw_pe < 200) else None
        else:
            pe = info.get("trailingPE") or info.get("forwardPE")
        pb     = info.get("priceToBook")
        peg    = info.get("trailingPegRatio") or info.get("pegRatio")
        dte    = info.get("debtToEquity")          # already expressed as a percentage
        margin = info.get("profitMargins")         # fraction, e.g. -0.05

        # ── Dividend yield & short interest (stocks; sparsely populated for funds) ──
        div_yield = info.get("dividendYield")          # already a percentage, e.g. 2.42
        pct_short = info.get("shortPercentOfFloat")    # fraction, e.g. 0.0096

        # ── Fund-only fields ────────────────────────────────────────────────
        nav      = info.get("navPrice")
        nav_prem = round((price / nav - 1) * 100, 2) if nav else None
        expense  = info.get("netExpenseRatio")     # percent, e.g. 0.03 = 0.03%
        fyield   = info.get("yield")               # fraction, e.g. 0.0104
        beta     = info.get("beta3Year") or info.get("beta")

        return {
            "ticker":       ticker,
            "name":         info.get("longName") or info.get("shortName", ticker),
            "sector":       info.get("sector", "Unknown"),
            "quote_type":   quote_type,
            "is_fund":      is_fund,
            "category":     info.get("category"),
            "price":        round(price, 2),
            "change1d":     change1d,
            "pe":           round(float(pe), 1)  if pe  else None,
            "pb":           round(float(pb), 2)  if pb  else None,
            "peg":          round(float(peg), 2) if peg else None,
            "rsi":          rsi,
            "ma50":         round(ma50, 2)  if ma50  is not None else None,
            "ma200":        round(ma200, 2) if ma200 is not None else None,
            "pct_vs_200":   pct_vs_200,
            "trend":        trend,
            "wk_high":      round(float(wk_high), 2) if wk_high else None,
            "wk_low":       round(float(wk_low), 2)  if wk_low  else None,
            "pct_off_high": pct_off_high,
            "pct_off_low":  pct_off_low,
            "debt_equity":  round(float(dte), 0)        if dte    is not None else None,
            "margin":       round(float(margin) * 100, 1) if margin is not None else None,
            "div_yield":    round(float(div_yield), 2) if div_yield is not None else None,
            "pct_short":    round(float(pct_short) * 100, 2) if pct_short is not None else None,
            "nav_premium":  nav_prem,
            "expense":      round(float(expense), 2) if expense is not None else None,
            "fund_yield":   round(float(fyield) * 100, 2) if fyield is not None else None,
            "beta":         round(float(beta), 2) if beta is not None else None,
        }
    except Exception:
        return None


# ── Signal classifier ─────────────────────────────────────────────────────────
# Atomic tags a stock can earn. The headline `signal` is derived from these.
BUY_TAGS  = ("undervalued", "oversold")
SELL_TAGS = ("overvalued", "overbought", "overextended")


def classify_signal(stock: dict, max_pe: float | None) -> tuple[str, list[str], str]:
    pe, pb, peg   = stock["pe"], stock["pb"], stock["peg"]
    rsi, sector   = stock["rsi"], stock["sector"]
    dte           = stock["debt_equity"]
    margin        = stock["margin"]
    pct_vs_200    = stock["pct_vs_200"]
    pct_off_high  = stock["pct_off_high"]
    pct_off_low   = stock["pct_off_low"]

    rsi_ok        = isinstance(rsi, float) and not pd.isna(rsi)
    sector_avg_pe = SECTOR_PE.get(sector, DEFAULT_SECTOR_PE)

    # ── Quality guards ──────────────────────────────────────────────────────
    high_debt    = dte    is not None and dte    > 200          # debt/equity > 200%
    unprofitable = margin is not None and margin < 0
    quality_flags = []
    if high_debt:
        quality_flags.append(f"debt/equity {dte:.0f}%")
    if unprofitable:
        quality_flags.append(f"profit margin {margin:.1f}%")

    # ── Momentum ───────────────────────────────────────────────────────────
    is_oversold   = rsi_ok and rsi < 35
    is_overbought = rsi_ok and rsi > 70

    # ── Trend / extension ──────────────────────────────────────────────────
    is_overextended = pct_vs_200 is not None and pct_vs_200 > 20
    near_52w_high   = pct_off_high is not None and pct_off_high > -3
    near_52w_low    = pct_off_low  is not None and pct_off_low  < 5

    # ── Valuation ──────────────────────────────────────────────────────────
    is_undervalued = (
        (pe is not None and pe < sector_avg_pe * 0.80) or
        (pb is not None and pb < 1.5 and (pe is None or pe < sector_avg_pe))
    )
    if max_pe is not None and pe is not None and pe > max_pe:
        is_undervalued = False

    is_overvalued = (
        (pe is not None and pe > sector_avg_pe * 1.25) or
        (peg is not None and peg > 2.0)
    )

    # A cheap stock with a broken balance sheet is a value trap, not a
    # bargain — suppress the undervalued call and flag it instead.
    value_trap = is_undervalued and (high_debt or unprofitable)
    if value_trap:
        is_undervalued = False

    # ── Tags + headline signal ─────────────────────────────────────────────
    tags = []
    if is_undervalued:   tags.append("undervalued")
    if is_oversold:      tags.append("oversold")
    if is_overvalued:    tags.append("overvalued")
    if is_overbought:    tags.append("overbought")
    if is_overextended:  tags.append("overextended")

    buy  = is_undervalued or is_oversold
    sell = is_overvalued or is_overbought or is_overextended

    if buy and sell:
        signal = "mixed"
    elif buy:
        signal = "buy"  if (is_undervalued and is_oversold) else \
                 "undervalued" if is_undervalued else "oversold"
    elif sell:
        signal = "sell" if (is_overvalued and (is_overbought or is_overextended)) else \
                 "overvalued" if is_overvalued else "overbought"
    else:
        signal = "neutral"

    # ── Thesis text ────────────────────────────────────────────────────────
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
    if near_52w_low and not is_undervalued and signal != "neutral":
        reasons.append(f"Trading {pct_off_low:+.0f}% from its 52-week low.")

    if is_overvalued:
        parts = []
        if pe is not None and pe > sector_avg_pe * 1.25:
            parts.append(f"P/E {pe}x vs sector avg ~{sector_avg_pe}x")
        if peg is not None and peg > 2.0:
            parts.append(f"PEG {peg}x")
        reasons.append("Overvalued: " + ", ".join(parts) + ".")
    if is_overbought:
        reasons.append(f"Overbought: RSI at {rsi} signals stretched buying.")
    if is_overextended:
        reasons.append(f"Overextended: {pct_vs_200:+.0f}% above its 200-day average.")
    if near_52w_high and not (is_overvalued or is_overbought) and signal != "neutral":
        reasons.append(f"Within {abs(pct_off_high):.0f}% of its 52-week high.")

    if value_trap:
        reasons.append("Screens cheap but flagged as a possible value trap ("
                       + ", ".join(quality_flags) + ").")
    elif quality_flags:
        reasons.append("Quality watch: " + ", ".join(quality_flags) + ".")

    if stock["trend"] and signal != "neutral":
        reasons.append("Trend: 50-day MA is "
                       + ("above" if stock["trend"] == "up" else "below")
                       + " the 200-day MA.")

    if not reasons:
        reasons.append("No strong valuation, momentum, or trend signal at current levels.")

    return signal, tags, " ".join(reasons)


def classify_fund(fund: dict, max_pe: float | None) -> tuple[str, list[str], str]:
    """ETF / fund classifier. Same buy/sell/hold vocabulary as classify_signal,
    but valuation comes from price-vs-NAV and holdings-P/E-vs-category rather
    than single-company fundamentals."""
    pe           = fund["pe"]
    rsi          = fund["rsi"]
    category     = fund["category"] or "Fund"
    nav_prem     = fund["nav_premium"]
    expense      = fund["expense"]
    pct_vs_200   = fund["pct_vs_200"]
    pct_off_high = fund["pct_off_high"]
    pct_off_low  = fund["pct_off_low"]

    rsi_ok = isinstance(rsi, float) and not pd.isna(rsi)
    cat_pe = CATEGORY_PE.get(category, DEFAULT_CATEGORY_PE)

    # ── Momentum ───────────────────────────────────────────────────────────
    is_oversold   = rsi_ok and rsi < 35
    is_overbought = rsi_ok and rsi > 70

    # ── Trend / extension ──────────────────────────────────────────────────
    is_overextended = pct_vs_200 is not None and pct_vs_200 > 20
    near_52w_high   = pct_off_high is not None and pct_off_high > -3
    near_52w_low    = pct_off_low  is not None and pct_off_low  < 5

    # ── Valuation: price vs NAV, and holdings P/E vs category benchmark ─────
    cheap_to_nav = nav_prem is not None and nav_prem < -1.5
    rich_to_nav  = nav_prem is not None and nav_prem >  1.5
    pe_cheap = pe is not None and pe < cat_pe * 0.80
    pe_rich  = pe is not None and pe > cat_pe * 1.25
    if max_pe is not None and pe is not None and pe > max_pe:
        pe_cheap = False

    is_undervalued = cheap_to_nav or pe_cheap
    is_overvalued  = rich_to_nav or pe_rich

    # Cost guard — a high-fee fund is not a "bargain".
    costly = expense is not None and expense > 0.50
    expensive_trap = is_undervalued and expense is not None and expense > 0.75
    if expensive_trap:
        is_undervalued = False

    # ── Tags + headline signal ─────────────────────────────────────────────
    tags = []
    if is_undervalued:   tags.append("undervalued")
    if is_oversold:      tags.append("oversold")
    if is_overvalued:    tags.append("overvalued")
    if is_overbought:    tags.append("overbought")
    if is_overextended:  tags.append("overextended")

    buy  = is_undervalued or is_oversold
    sell = is_overvalued or is_overbought or is_overextended

    if buy and sell:
        signal = "mixed"
    elif buy:
        signal = "buy"  if (is_undervalued and is_oversold) else \
                 "undervalued" if is_undervalued else "oversold"
    elif sell:
        signal = "sell" if (is_overvalued and (is_overbought or is_overextended)) else \
                 "overvalued" if is_overvalued else "overbought"
    else:
        signal = "hold"

    # ── Thesis text ────────────────────────────────────────────────────────
    reasons = []
    if cheap_to_nav:
        reasons.append(f"Trading {nav_prem:+.1f}% vs NAV (discount).")
    if pe_cheap:
        reasons.append(f"Holdings P/E {pe:.0f}x vs {category} avg ~{cat_pe}x.")
    if is_oversold:
        reasons.append(f"Oversold: RSI at {rsi} signals excess selling pressure.")
    if near_52w_low and signal != "hold" and not is_undervalued:
        reasons.append(f"Trading {pct_off_low:+.0f}% from its 52-week low.")

    if rich_to_nav:
        reasons.append(f"Trading {nav_prem:+.1f}% vs NAV (premium).")
    if pe_rich:
        reasons.append(f"Holdings P/E {pe:.0f}x vs {category} avg ~{cat_pe}x.")
    if is_overbought:
        reasons.append(f"Overbought: RSI at {rsi} signals stretched buying.")
    if is_overextended:
        reasons.append(f"Overextended: {pct_vs_200:+.0f}% above its 200-day average.")
    if near_52w_high and signal != "hold" and not (is_overvalued or is_overbought):
        reasons.append(f"Within {abs(pct_off_high):.0f}% of its 52-week high.")

    if expensive_trap:
        reasons.append(f"Screens cheap but expense ratio {expense:.2f}% is high.")
    elif costly:
        reasons.append(f"Cost watch: expense ratio {expense:.2f}%.")

    if fund["trend"] and signal != "hold":
        reasons.append("Trend: 50-day MA is "
                       + ("above" if fund["trend"] == "up" else "below")
                       + " the 200-day MA.")

    if not reasons:
        reasons.append("No strong valuation, momentum, or trend signal — hold.")

    return signal, tags, " ".join(reasons)


# ── Screen runner ─────────────────────────────────────────────────────────────
def run_screen(
    tickers:       list[str],
    signal_filter: str        = "flagged",
    max_pe:        float | None = None,
    asset_type:    str         = "all",
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
    icons   = {
        "buy": "[BUY] ", "undervalued": "[UV]  ", "oversold": "[OS]  ",
        "sell": "[SELL]", "overvalued": "[OV]  ", "overbought": "[OB]  ",
        "mixed": "[MIX] ", "neutral": "      ", "hold": "[HOLD]",
    } if ASCII_OUTPUT else {
        "buy": "🔥", "undervalued": "📉", "oversold": "⚠️",
        "sell": "🚩", "overvalued": "📈", "overbought": "🔺",
        "mixed": "❓", "neutral": "  ", "hold": "  ",
    }

    for i, ticker in enumerate(tickers, 1):
        if verbose:
            print(f"  [{i:>4}/{total}] {ticker:<8}", end="", flush=True)

        stock = fetch_stock(ticker)
        if stock is None:
            if verbose:
                print("skipped")
            continue

        if (asset_type == "stock" and stock["is_fund"]) or \
           (asset_type == "etf"   and stock["quote_type"] != "ETF"):
            if verbose:
                print(f"filtered ({stock['quote_type'].lower()})")
            continue

        classifier = classify_fund if stock["is_fund"] else classify_signal
        signal, tags, thesis = classifier(stock, max_pe)
        stock["signal"] = signal
        stock["tags"]   = tags
        stock["thesis"] = thesis
        results.append(stock)

        if verbose:
            icon = icons.get(signal, "  ")
            kind = f"[{stock['quote_type'][:3]}] " if stock["is_fund"] else ""
            pe_s = f"P/E {stock['pe']}x" if stock["pe"] else "P/E N/A"
            rsi_s = f"RSI {stock['rsi']}" if stock["rsi"] else ""
            print(f"{icon} {kind}{signal:<12}  {pe_s}  {rsi_s}")

        # Polite delay every batch_size tickers to avoid rate limits
        if i % batch_size == 0 and i < total:
            if verbose:
                print(f"\n  Pausing {delay}s to respect rate limits…\n")
            time.sleep(delay)

    # Apply signal filter
    def _passes(stock: dict) -> bool:
        tags = stock["tags"]
        if signal_filter == "all":
            return True
        if signal_filter == "flagged":
            return stock["signal"] not in ("neutral", "hold")
        if signal_filter == "hold":
            return stock["signal"] in ("neutral", "hold")
        if signal_filter == "buy":
            return any(t in tags for t in BUY_TAGS)
        if signal_filter == "sell":
            return any(t in tags for t in SELL_TAGS)
        return signal_filter in tags        # a specific tag name

    matched = [r for r in results if _passes(r)]
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
    labels = {
        "buy":         "BUY  — UNDERVALUED + OVERSOLD",
        "undervalued": "UNDERVALUED",
        "oversold":    "OVERSOLD",
        "sell":        "SELL — OVERVALUED + STRETCHED",
        "overvalued":  "OVERVALUED",
        "overbought":  "OVERBOUGHT",
        "mixed":       "MIXED SIGNALS",
        "neutral":     "NEUTRAL",
        "hold":        "HOLD",
    }
    for s in stocks:
        chg_s   = f"{'+' if s['change1d'] >= 0 else ''}{s['change1d']}%"
        pe_s    = f"{s['pe']}x"  if s["pe"]  is not None else "N/A"
        rsi_s   = str(s["rsi"]) if s["rsi"] is not None else "N/A"
        ma200_s = f"{s['pct_vs_200']:+.0f}% vs 200DMA" if s["pct_vs_200"] is not None else "N/A"
        hi_s    = f"{s['pct_off_high']:+.0f}%" if s["pct_off_high"] is not None else "N/A"
        lo_s    = f"{s['pct_off_low']:+.0f}%"  if s["pct_off_low"]  is not None else "N/A"
        short_s = f"{s['pct_short']:.1f}% of float" if s["pct_short"] is not None else "N/A"
        sig_s   = labels.get(s["signal"], s["signal"].upper())

        if s["is_fund"]:
            nav_s  = f"{s['nav_premium']:+.2f}% vs NAV" if s["nav_premium"] is not None else "N/A"
            yld_s  = f"{s['fund_yield']:.2f}%" if s["fund_yield"] is not None else "N/A"
            exp_s  = f"{s['expense']:.2f}%"    if s["expense"]    is not None else "N/A"
            beta_s = str(s["beta"]) if s["beta"] is not None else "N/A"
            print(f"\n  {s['ticker']:6}  {s['name']}  [{s['quote_type']}]")
            print(f"  {'─' * (W - 2)}")
            print(f"  Price   : ${s['price']:<10.2f} Today : {chg_s}")
            print(f"  NAV     : {nav_s:<16} Yield : {yld_s}")
            print(f"  Hold P/E: {pe_s:<16} RSI   : {rsi_s}")
            print(f"  Trend   : {ma200_s:<16} 52wk  : {lo_s} from low / {hi_s} from high")
            print(f"  Expense : {exp_s:<16} Beta  : {beta_s}")
            print(f"  Short   : {short_s:<16} Category: {s['category'] or '—'}")
            print(f"  Signal  : {sig_s}")
            print(f"  {s['thesis']}")
            continue

        pb_s    = f"{s['pb']}x"  if s["pb"]  is not None else "N/A"
        peg_s   = f"{s['peg']}x" if s["peg"] is not None else "N/A"
        dte_s   = f"{s['debt_equity']:.0f}%"   if s["debt_equity"]  is not None else "N/A"
        mgn_s   = f"{s['margin']:.1f}%"        if s["margin"]       is not None else "N/A"
        dy_s    = f"{s['div_yield']:.2f}%"     if s["div_yield"]    is not None else "N/A"
        print(f"\n  {s['ticker']:6}  {s['name']}")
        print(f"  {'─' * (W - 2)}")
        print(f"  Price  : ${s['price']:<10.2f}  Today : {chg_s}")
        print(f"  P/E    : {pe_s:<10}  P/B   : {pb_s}")
        print(f"  PEG    : {peg_s:<10}  RSI   : {rsi_s}")
        print(f"  Trend  : {ma200_s:<15}  52wk  : {lo_s} from low / {hi_s} from high")
        print(f"  Debt/Eq: {dte_s:<10}  Margin: {mgn_s}")
        print(f"  Div Yld: {dy_s:<10}  Short : {short_s}")
        print(f"  Sector : {s['sector']}")
        print(f"  Signal : {sig_s}")
        print(f"  {s['thesis']}")
    print("\n" + "═" * W)
    print("  ⚠  Not financial advice. Always do your own research.\n")


# ── HTML report ───────────────────────────────────────────────────────────────
def save_html_report(stocks: list[dict], screened: int, path: str,
                     index_label: str = "") -> None:
    date_str = datetime.now().strftime("%B %d, %Y  %H:%M")
    badge = {
        "buy":         ("Buy signal",   "#eff6ff", "#2563eb"),
        "undervalued": ("Undervalued",  "#f0fdf4", "#16a34a"),
        "oversold":    ("Oversold",     "#fffbeb", "#d97706"),
        "sell":        ("Sell signal",  "#fef2f2", "#dc2626"),
        "overvalued":  ("Overvalued",   "#fdf2f8", "#db2777"),
        "overbought":  ("Overbought",   "#fff7ed", "#ea580c"),
        "mixed":       ("Mixed",        "#f5f3ff", "#7c3aed"),
        "neutral":     ("Neutral",      "#f5f5f3", "#6b6b67"),
        "hold":        ("Hold",         "#f5f5f3", "#6b6b67"),
    }
    cards = ""
    for s in stocks:
        label, bg, fg = badge.get(s["signal"], ("Neutral", "#f5f5f3", "#6b6b67"))
        chg_c   = "#16a34a" if s["change1d"] >= 0 else "#dc2626"
        chg_s   = f"{'+' if s['change1d'] >= 0 else ''}{s['change1d']}%"
        pe_s    = f"{s['pe']}x"  if s["pe"]  is not None else "N/A"
        rsi_s   = str(s["rsi"]) if s["rsi"] is not None else "N/A"
        ma200_s = f"{s['pct_vs_200']:+.0f}%" if s["pct_vs_200"] is not None else "N/A"
        hi_s    = f"{s['pct_off_high']:+.0f}%" if s["pct_off_high"] is not None else "N/A"
        short_s = f"{s['pct_short']:.1f}%" if s["pct_short"] is not None else "N/A"

        if s["is_fund"]:
            nav_s  = f"{s['nav_premium']:+.2f}%" if s["nav_premium"] is not None else "N/A"
            yld_s  = f"{s['fund_yield']:.2f}%" if s["fund_yield"] is not None else "N/A"
            exp_s  = f"{s['expense']:.2f}%"    if s["expense"]    is not None else "N/A"
            beta_s = str(s["beta"]) if s["beta"] is not None else "N/A"
            cards += f"""
        <div class="card">
          <div class="card-top">
            <div>
              <div class="sname">{s['ticker']} <span class="price">${s['price']:.2f}</span>
                <span class="kind">{s['quote_type']}</span></div>
              <div class="ssub">{s['name']} &middot; {s['category'] or 'fund'}</div>
            </div>
            <span class="badge" style="background:{bg};color:{fg}">{label}</span>
          </div>
          <div class="metrics">
            <div><div class="ml">Price vs NAV</div><div class="mv">{nav_s}</div></div>
            <div><div class="ml">Yield</div><div class="mv">{yld_s}</div></div>
            <div><div class="ml">Holdings P/E</div><div class="mv">{pe_s}</div></div>
            <div><div class="ml">RSI (14d)</div><div class="mv">{rsi_s}</div></div>
            <div><div class="ml">vs 200-day</div><div class="mv">{ma200_s}</div></div>
            <div><div class="ml">Off 52w high</div><div class="mv">{hi_s}</div></div>
            <div><div class="ml">Expense ratio</div><div class="mv">{exp_s}</div></div>
            <div><div class="ml">Beta (3y)</div><div class="mv">{beta_s}</div></div>
            <div><div class="ml">Short % float</div><div class="mv">{short_s}</div></div>
            <div><div class="ml">Today</div><div class="mv" style="color:{chg_c}">{chg_s}</div></div>
          </div>
          <div class="thesis">{s['thesis']}</div>
        </div>"""
            continue

        pb_s    = f"{s['pb']}x"  if s["pb"]  is not None else "N/A"
        peg_s   = f"{s['peg']}x" if s["peg"] is not None else "N/A"
        dte_s   = f"{s['debt_equity']:.0f}%"  if s["debt_equity"] is not None else "N/A"
        mgn_s   = f"{s['margin']:.1f}%"       if s["margin"]      is not None else "N/A"
        dy_s    = f"{s['div_yield']:.2f}%"    if s["div_yield"]   is not None else "N/A"
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
            <div><div class="ml">PEG ratio</div><div class="mv">{peg_s}</div></div>
            <div><div class="ml">RSI (14d)</div><div class="mv">{rsi_s}</div></div>
            <div><div class="ml">vs 200-day</div><div class="mv">{ma200_s}</div></div>
            <div><div class="ml">Off 52w high</div><div class="mv">{hi_s}</div></div>
            <div><div class="ml">Debt / equity</div><div class="mv">{dte_s}</div></div>
            <div><div class="ml">Profit margin</div><div class="mv">{mgn_s}</div></div>
            <div><div class="ml">Dividend yield</div><div class="mv">{dy_s}</div></div>
            <div><div class="ml">Short % float</div><div class="mv">{short_s}</div></div>
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
.kind{{font-size:10px;font-weight:600;color:#6b6b67;border:0.5px solid rgba(0,0,0,.18);
        border-radius:4px;padding:1px 4px;margin-left:4px;vertical-align:middle}}
.ssub{{font-size:12px;color:#6b6b67;margin-top:2px}}
.badge{{font-size:11px;padding:3px 9px;border-radius:6px;font-weight:500;
         white-space:nowrap;flex-shrink:0}}
.metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;
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
        description="Stock screener — buy-side (undervalued / oversold) and "
                    "sell-side (overvalued / overbought / overextended) signals "
                    "from Yahoo Finance.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 stock_screener.py                            # default 15-stock watchlist
  python3 stock_screener.py --index sp500              # all ~500 S&P stocks
  python3 stock_screener.py --index sp500 --signal sell    # trim candidates
  python3 stock_screener.py --index nasdaq100 --signal overbought
  python3 stock_screener.py --index sp500 --signal oversold --max-pe 20
  python3 stock_screener.py --index sp500 --output daily.html   # -> daily-sp500.html
  python3 stock_screener.py --tickers AAPL MSFT TSLA --output report.html
  python3 stock_screener.py --tickers-file watchlist.txt
  python3 stock_screener.py --tickers-file mixed.txt --asset-type etf   # ETFs only
  python3 stock_screener.py --tickers VOO QQQ SMH --signal hold
  python3 stock_screener.py --index sp500 --signal sell --ascii   # no emoji
  python3 stock_screener.py --list-indices

ETFs / funds are auto-detected and screened on price-vs-NAV, holdings P/E vs
category, and the usual RSI / trend signals. Every run is mirrored to a
timestamped log in ./logs/ (override with --log-dir).

--ascii swaps the emoji status icons for [BUY]/[SELL]/… tags (handy on Linux
without a color-emoji font); it auto-enables on a dumb / non-UTF-8 terminal or
when STOCK_SCREENER_ASCII is set.
        """
    )
    parser.add_argument("--index",   choices=list(INDICES.keys()), default=None,
                        help="Screen an entire index")
    parser.add_argument("--tickers", nargs="+", default=None, metavar="TICK",
                        help="Custom space-separated tickers (overrides --index and default list)")
    parser.add_argument("--tickers-file", type=str, default=None, metavar="FILE",
                        help="Read tickers from a file, separated by any whitespace, "
                             "newlines, carriage returns, and/or commas "
                             "(overrides --index; --tickers wins over this)")
    parser.add_argument("--signal",
                        choices=["flagged", "buy", "sell", "hold", "all",
                                 "undervalued", "oversold", "overvalued", "overbought"],
                        default="flagged",
                        help="Which signals to include (default: flagged = any non-neutral; "
                             "buy = undervalued/oversold; sell = overvalued/overbought/overextended; "
                             "hold = no signal either way)")
    parser.add_argument("--asset-type", choices=["all", "stock", "etf"], default="all",
                        help="Restrict to equities only or ETFs only (default: all). "
                             "Type is auto-detected per ticker.")
    parser.add_argument("--max-pe",  type=float, default=None, metavar="N",
                        help="Only include names with P/E below N (holdings P/E for funds)")
    parser.add_argument("--output",  type=str, default=None, metavar="FILE.html",
                        help="Save HTML report to this file. With --index, the index "
                             "name is added automatically (daily.html -> daily-sp500.html); "
                             "use a literal {index} in the name to place it yourself")
    parser.add_argument("--log-dir", type=str, default="logs", metavar="DIR",
                        help="Directory for timestamped run logs (default: ./logs)")
    parser.add_argument("--ascii", action="store_true",
                        help="Plain-ASCII output: replace emoji status icons with [BUY]/[SELL]/… "
                             "tags and box-drawing with -/=. Auto-enabled on a non-UTF-8 or "
                             "dumb terminal, or when STOCK_SCREENER_ASCII is set.")
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

    global ASCII_OUTPUT
    ASCII_OUTPUT = resolve_ascii(args.ascii)

    # ── Start run log: mirror everything to ./logs/screener_<timestamp>.log ──
    log_file, log_path = _open_run_log(args.log_dir)
    sys.stdout = _Tee(sys.__stdout__, log_file, ascii_only=ASCII_OUTPUT)
    sys.stderr = _Tee(sys.__stderr__, log_file, ascii_only=ASCII_OUTPUT)

    try:
        # Resolve ticker list
        index_label = ""
        if args.tickers:
            tickers = parse_tickers(" ".join(args.tickers))
        elif args.tickers_file:
            tickers = load_tickers_file(args.tickers_file)
            print(f"\nLoaded {len(tickers)} tickers from {args.tickers_file}")
        elif args.index:
            index_label = INDICES[args.index]["label"]
            print(f"\nLoading index: {index_label}")
            tickers = load_index(args.index)
        else:
            tickers = DEFAULT_TICKERS

        out_path = resolve_output_path(args.output, args.index)

        print(f"\nStock screener  ·  {datetime.now().strftime('%B %d, %Y')}")
        print(f"Index   : {index_label or 'custom/default watchlist'}")
        print(f"Tickers : {len(tickers)} to analyze")
        print(f"Signal  : {args.signal}")
        print(f"Assets  : {args.asset_type}")
        print(f"Max P/E : {args.max_pe or 'none'}")
        print(f"Output  : {out_path or 'terminal only'}")
        print(f"Run log : {os.path.abspath(log_path)}")
        print(f"\nFetching live data from Yahoo Finance…\n")

        stocks, screened = run_screen(
            tickers       = tickers,
            signal_filter = args.signal,
            max_pe        = args.max_pe,
            asset_type    = args.asset_type,
            verbose       = True,
        )

        print_report(stocks, screened)

        if out_path:
            save_html_report(stocks, screened, out_path, index_label)
            print(f"  HTML report saved → {os.path.abspath(out_path)}\n")
    finally:
        print(f"  Run log saved  → {os.path.abspath(log_path)}\n")
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        log_file.close()


if __name__ == "__main__":
    main()
