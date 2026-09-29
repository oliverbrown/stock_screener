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
    Money flow— 20-day Chaikin Money Flow (all names); for stocks, notes
                whether accumulation/distribution supports the signal
    Earnings  — next report date (before open / after close / estimated);
                a report within 14 days is flagged, as is one in the last 5
    Info      — analysts' 1-year consensus price target (mean, with low–high
                range, analyst count, and rating); not used in the signal

ETFs / funds (auto-detected by quoteType):
    Buy side  — undervalued (discount to NAV, or holdings P/E below the
                fund's category benchmark), oversold (RSI / trend)
    Sell side — overvalued (premium to NAV, or holdings P/E above category),
                overbought, overextended
    hold      — no signal either way
    Context   — NAV premium/discount (always shown), distribution yield,
                expense ratio (a high-cost fund is not a "bargain"), 3y beta

All names — pre-market / after-hours price while that session is open.
Tickers that can't be screened are skipped with a reason; Yahoo rate limits
are retried with backoff. Share classes may be written BRK.B or BRK-B.

Every run is also written to a timestamped log file in ./logs/ (logs older
than 30 days are deleted automatically). Downloaded data is cached in
./cache/ for 30 minutes, so back-to-back runs over overlapping ticker lists
download each ticker only once.

Usage:
    python3 stock_screener.py                          # default watchlist
    python3 stock_screener.py --index sp500            # all S&P 500 stocks
    python3 stock_screener.py --index dow30 --signal sell
    python3 stock_screener.py --tickers AAPL MSFT TSLA
    python3 stock_screener.py --tickers-file watchlist.txt   # whitespace/comma/newline separated;
                                                             # '#' starts a comment to end of line
    python3 stock_screener.py --tickers-file ticker-lists/sp400.txt   # from build_ticker_lists.py
    python3 stock_screener.py --tickers-file mixed.txt --asset-type etf   # ETFs only
    python3 stock_screener.py --tickers VOO QQQ SMH --signal hold
    python3 stock_screener.py --index sp500 --signal sell --ascii   # plain-ASCII output
    python3 stock_screener.py --index sp500 --output daily.html    # writes daily-sp500.html
    python3 stock_screener.py --list-indices           # show all available indices

Requirements:
    pip install yfinance pandas
"""

import argparse
import glob
import json
import sys
import os
import logging
import re
import time
import random
import urllib.parse
import warnings
from datetime import date, datetime, timezone

try:
    from zoneinfo import ZoneInfo
    MARKET_TZ = ZoneInfo("America/New_York")
except Exception:                                     # no tz database available
    MARKET_TZ = timezone.utc

try:
    import yfinance as yf
    import pandas as pd
    import ticker_sources
except ImportError:
    print("Missing dependencies. Run:  pip install -r requirements.txt")
    sys.exit(1)

try:
    from yfinance.exceptions import YFRateLimitError
except ImportError:                                   # older yfinance
    class YFRateLimitError(Exception):
        pass

# yfinance's own __init__ re-enables DeprecationWarning for its module
# (warnings.filterwarnings('default', ..., module='^yfinance')), which is
# added *after* any filter set before the import and so takes precedence.
# Register ours after importing so it wins: its history scraper creates an
# empty pd.Series() with no dtype on some tickers (e.g. no capital-gains
# history) — a harmless pandas deprecation notice, not a real problem.
warnings.filterwarnings(
    "ignore",
    message="The default dtype for empty Series",
    category=DeprecationWarning,
)

# yfinance logs its own raw HTTP errors (e.g. a 404 JSON blob for an unknown
# ticker) straight to stderr, breaking up the progress line. fetch_stock
# reports a readable skip reason instead, so mute yfinance's logger.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

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
    "\U0001f6a9": "!", "\U0001f680": "^", "\U0001f53a": "^", "\U0001f53b": "v",
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


def yahoo_symbol(sym: str) -> str:
    """Normalise a share-class ticker to Yahoo's dash form: BRK.B -> BRK-B.
    Only a single A/B/C class letter is rewritten, so exchange suffixes
    such as SHOP.TO or VOD.L are left alone."""
    return re.sub(r"^([A-Z]+)\.([ABC])$", r"\1-\2", sym.strip().upper())


def parse_tickers(text: str) -> list[str]:
    """Split a blob of ticker symbols on any run of whitespace, newlines,
    carriage returns, and/or commas. A '#' starts a comment that runs to the
    end of its line (e.g. "AAPL  # Apple Inc."). Upper-cased, share classes
    normalised to Yahoo's form (BRK.B -> BRK-B), de-duplicated, order kept."""
    text = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    seen, out = set(), []
    for tok in re.split(r"[,\s]+", text.strip()):
        sym = yahoo_symbol(tok) if tok.strip() else ""
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


def prune_old_logs(log_dir: str, keep_days: int) -> int:
    """Delete this screener's run logs (screener_*.log) older than keep_days.
    keep_days <= 0 keeps everything. Returns how many were removed."""
    if keep_days <= 0:
        return 0
    cutoff  = time.time() - keep_days * 86400
    removed = 0
    for path in glob.glob(os.path.join(log_dir, "screener_*.log")):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


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
# --index fetches the current members live (ticker_sources.py) when possible;
# these are only used when that fails.
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
    "BF-B","BIO","BK","BKNG","BKR","BLK","BMY","BR","BRK-B","BRO","BSX","BWA",
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

# Russell 2000: too large to bundle fully (~2000 tickers). --index russell2000
# fetches the full list live; this small sample is only the offline fallback.
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
        "live":    True,
        "static":  SP500_TICKERS,
    },
    "dow30": {
        "label":   "Dow Jones Industrial Average (Dow 30)",
        "live":    True,
        "static":  DOW30_TICKERS,
    },
    "nasdaq100": {
        "label":   "NASDAQ-100",
        "live":    True,
        "static":  NASDAQ100_TICKERS,
    },
    "russell2000": {
        "label":   "Russell 2000",
        "live":    True,
        "static":  RUSSELL2000_SAMPLE,
    },
}


# ── Index loader ──────────────────────────────────────────────────────────────
def load_index(name: str) -> list[str]:
    """Return constituent tickers for a named index. Fetches the current list
    live (see ticker_sources.py), falling back to the bundled static list."""
    idx = INDICES[name]

    if idx.get("live"):
        try:
            tickers = [sym for sym, _ in ticker_sources.INDEX_FETCHERS[name]()]
            if tickers:
                print(f"  ✓ Loaded {len(tickers)} current tickers ({idx['label']})")
                return tickers
        except Exception as e:
            print(f"  ⚠ Live index fetch failed ({e}), using bundled list.")

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


# ── Chaikin Money Flow ────────────────────────────────────────────────────────
# CMF above +0.05 = accumulation (closes near the day's high on volume),
# below -0.05 = distribution; beyond ±0.25 is strong.
CMF_THRESHOLD = 0.05
CMF_STRONG    = 0.25


def compute_cmf(hist: pd.DataFrame, period: int = 20) -> float | None:
    """Chaikin Money Flow over the last `period` days, from -1 to +1.

    Each day's close is placed within its high-low range (+1 at the high,
    -1 at the low), weighted by volume, and summed over the window relative
    to total volume. Days with no range (high == low) count as neutral."""
    if len(hist) < period or not {"High", "Low", "Close", "Volume"} <= set(hist.columns):
        return None
    h = hist.iloc[-period:]
    rng = (h["High"] - h["Low"]).replace(0, float("nan"))
    mfm = (((h["Close"] - h["Low"]) - (h["High"] - h["Close"])) / rng).fillna(0)
    vol = h["Volume"].sum()
    if not vol or pd.isna(vol):
        return None
    return round(float((mfm * h["Volume"]).sum() / vol), 3)


def cmf_label(cmf: float | None) -> str:
    if cmf is None:
        return "N/A"
    if cmf >=  CMF_STRONG:    return "strong accumulation"
    if cmf >=  CMF_THRESHOLD: return "accumulation"
    if cmf <= -CMF_STRONG:    return "strong distribution"
    if cmf <= -CMF_THRESHOLD: return "distribution"
    return "neutral"


# ── Earnings dates ────────────────────────────────────────────────────────────
# A report within this many days gets a warning chip and a thesis note.
EARNINGS_WARN_DAYS   = 14
# A report this recent gets a "just reported" note in the thesis.
EARNINGS_RECENT_DAYS = 5


def market_today() -> date:
    return datetime.now(MARKET_TZ).date()


def parse_earnings(info: dict, today: date | None = None) -> tuple[dict | None, str | None]:
    """Next and most recent earnings report from Yahoo's quote info.

    Returns (next, last): next is {"date", "end", "time", "estimate"} with
    ISO dates ("end" differs from "date" when Yahoo only has a window) and
    time "before open" / "after close" / None; last is an ISO date. Either
    may be None (funds, crypto, or no date announced)."""
    today = today or market_today()

    def et(ts):
        try:
            return datetime.fromtimestamp(float(ts), MARKET_TZ) if ts else None
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    start, end = et(info.get("earningsTimestampStart")), et(info.get("earningsTimestampEnd"))
    stamp = et(info.get("earningsTimestamp"))
    upcoming = [d for d in (start, stamp) if d and d.date() >= today]
    past     = [d for d in (stamp, start) if d and d.date() < today]

    nxt = None
    if upcoming:
        when = upcoming[0]
        estimate = bool(info.get("isEarningsDateEstimate"))
        timing = None
        if not estimate:
            minutes = when.hour * 60 + when.minute
            timing = ("before open" if minutes < 9 * 60 + 30 else
                      "after close" if minutes >= 16 * 60 else None)
        end_d = end.date() if (when is start and end and end.date() > when.date()) else when.date()
        nxt = {"date": when.date().isoformat(), "end": end_d.isoformat(),
               "time": timing, "estimate": estimate}
    last = max(past).date().isoformat() if past else None
    return nxt, last


def earnings_days(stock: dict, today: date | None = None) -> int | None:
    """Days until the next earnings report (0 = today), or None."""
    nxt = stock.get("earnings_next")
    if not nxt:
        return None
    return (date.fromisoformat(nxt["date"]) - (today or market_today())).days


def days_since_earnings(stock: dict, today: date | None = None) -> int | None:
    last = stock.get("earnings_last")
    if not last:
        return None
    return ((today or market_today()) - date.fromisoformat(last)).days


def _in_days(n: int) -> str:
    return "today" if n == 0 else "tomorrow" if n == 1 else f"in {n} days"


def earnings_when(stock: dict, year: bool = False) -> str:
    """The next report's date, e.g. "Oct 29", "Thu Oct 29, 2026", or a
    window "Oct 27–Nov 3" when Yahoo has no exact date."""
    nxt = stock["earnings_next"]
    d, e = date.fromisoformat(nxt["date"]), date.fromisoformat(nxt["end"])
    when = f"{d:%a} {d:%b} {d.day}, {d.year}" if year else f"{d:%b} {d.day}"
    return when + (f"–{e:%b} {e.day}" if e != d else "")


def earnings_details(stock: dict, today: date | None = None) -> list[str]:
    """e.g. ["in 31 days", "after close"] or ["in 40 days", "estimated"]."""
    nxt = stock["earnings_next"]
    out = [_in_days(earnings_days(stock, today))]
    if nxt["time"]:
        out.append(nxt["time"])
    if nxt["estimate"]:
        out.append("estimated")
    return out


# ── Fetch one ticker ──────────────────────────────────────────────────────────
# Seconds to wait before each retry when Yahoo rate-limits us.
RATE_LIMIT_BACKOFF = (10, 30, 60)


class SkipTicker(Exception):
    """Raised when a ticker can't be screened; the message is the reason."""


def _is_rate_limit(e: Exception) -> bool:
    msg = str(e).lower()
    return isinstance(e, YFRateLimitError) or "too many requests" in msg or " 429" in msg


class TickerCache:
    """On-disk cache of each ticker's fetched data, one JSON file per ticker.

    Every stock_screener.py run is a separate process, so a runner script
    that screens overlapping lists (my-stocks, then the S&P 500, then the
    Nasdaq-100…) would otherwise download the same ticker again each time.
    Entries are reused for max_age_min minutes; only successful fetches are
    cached, never skips or errors."""

    # Bump when fetch_stock's output fields change, so older entries (which
    # would be missing the new fields) are ignored instead of crashing.
    VERSION = 3

    def __init__(self, cache_dir: str, max_age_min: float):
        self.dir     = cache_dir
        self.max_age = max_age_min * 60
        self.hits    = 0
        self.misses  = 0
        os.makedirs(cache_dir, exist_ok=True)

    def _path(self, ticker: str) -> str:
        return os.path.join(self.dir, urllib.parse.quote(ticker, safe="") + ".json")

    def get(self, ticker: str) -> dict | None:
        try:
            with open(self._path(ticker), encoding="utf-8") as f:
                entry = json.load(f)
            age = time.time() - entry["fetched"]
            if entry.get("version") == self.VERSION and 0 <= age <= self.max_age:
                self.hits += 1
                return {**entry["data"], "cached_min": int(age // 60)}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.misses += 1
        return None

    def put(self, ticker: str, data: dict) -> None:
        entry = {"version": self.VERSION, "fetched": time.time(), "data": data}
        tmp = self._path(ticker) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(entry, f)
            os.replace(tmp, self._path(ticker))        # atomic: no half-written files
        except OSError:
            pass

    def prune(self, older_than_hours: float = 24) -> int:
        """Delete entries older than older_than_hours. Returns how many."""
        cutoff  = time.time() - older_than_hours * 3600
        removed = 0
        for path in glob.glob(os.path.join(self.dir, "*.json")):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
        return removed


# Set by main() unless --no-cache; None means always download.
CACHE: TickerCache | None = None


def fetch_stock(ticker: str) -> tuple[dict | None, str | None]:
    """Fetch and derive everything the screener needs for one ticker.
    Returns (data, None), or (None, reason) when the ticker is skipped.
    Uses CACHE when set (data then carries "cached_min", its age in
    minutes). Retries with backoff when Yahoo rate-limits the request."""
    if CACHE is not None:
        cached = CACHE.get(ticker)
        if cached is not None:
            return cached, None
    for wait in (*RATE_LIMIT_BACKOFF, None):
        try:
            data = _fetch_stock_once(ticker)
            if CACHE is not None:
                CACHE.put(ticker, data)
            return data, None
        except SkipTicker as e:
            return None, str(e)
        except Exception as e:
            if not _is_rate_limit(e):
                msg = " ".join(str(e).split())
                msg = msg[:77] + "…" if len(msg) > 80 else msg
                return None, f"{type(e).__name__}: {msg}" if msg else type(e).__name__
            if wait is None:
                return None, f"rate-limited by Yahoo (gave up after {len(RATE_LIMIT_BACKOFF)} retries)"
            print(f"rate-limited, retrying in {wait}s… ", end="", flush=True)
            time.sleep(wait)
    return None, "unreachable"


def _fetch_stock_once(ticker: str) -> dict:
    t    = yf.Ticker(ticker)
    info = t.info
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    if not price:
        raise SkipTicker("no price data (unknown, delisted, or mistyped ticker?)")
    hist = t.history(period="1y", auto_adjust=True)
    if hist.empty or len(hist) < 2:
        raise SkipTicker("not enough price history")
    close    = hist["Close"]
    price    = float(price)
    rsi      = compute_rsi(close)
    cmf      = compute_cmf(hist)
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

    # ── Analyst 1-year consensus price target (equities only; rarely
    #    populated for funds). Yahoo's "1y Target Est" is the mean. ───────
    target       = info.get("targetMeanPrice")
    target_upside = round((target / price - 1) * 100, 1) if target else None
    target_low   = info.get("targetLowPrice")
    target_high  = info.get("targetHighPrice")
    num_analysts = info.get("numberOfAnalystOpinions")
    rating       = info.get("recommendationKey")       # e.g. "buy", "strong_buy"
    if rating in (None, "none"):
        rating = None

    earnings_next, earnings_last = parse_earnings(info)

    # ── Extended hours: only while the pre / post session is open ────────
    market_state = (info.get("marketState") or "").upper()
    ext_session, ext_price, ext_change = None, None, None
    if market_state == "PRE":
        ext_session = "pre"
        ext_price   = info.get("preMarketPrice")
        ext_change  = info.get("preMarketChangePercent")
    elif market_state == "POST":
        ext_session = "post"
        ext_price   = info.get("postMarketPrice")
        ext_change  = info.get("postMarketChangePercent")
    if not ext_price:
        ext_session, ext_change = None, None

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
        "cmf":          cmf,
        "earnings_next": earnings_next,
        "earnings_last": earnings_last,
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
        "target_price":  round(float(target), 2) if target else None,
        "target_upside": target_upside,
        "target_low":    round(float(target_low), 2)  if target_low  else None,
        "target_high":   round(float(target_high), 2) if target_high else None,
        "num_analysts":  int(num_analysts) if num_analysts else None,
        "rating":        rating.replace("_", " ") if rating else None,
        "ext_session":   ext_session,
        "ext_price":     round(float(ext_price), 2) if ext_price else None,
        "ext_change":    round(float(ext_change), 2) if ext_change is not None else None,
    }


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
                 "overvalued" if is_overvalued else \
                 "overbought" if is_overbought else "overextended"
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

    # Earnings: a report can reverse any signal overnight.
    if signal != "neutral":
        n = earnings_days(stock)
        if n is not None and n <= EARNINGS_WARN_DAYS:
            est = ", estimated" if stock["earnings_next"]["estimate"] else ""
            reasons.append(f"Earnings {_in_days(n)} ({earnings_when(stock)}{est}): "
                           "the report can quickly reverse this signal.")
        ago = days_since_earnings(stock)
        if ago is not None and ago <= EARNINGS_RECENT_DAYS:
            reasons.append(f"Reported earnings {'today' if ago == 0 else 'yesterday' if ago == 1 else f'{ago} days ago'}; "
                           "the price may still be adjusting.")

    # Money flow confirms or questions a one-sided call; it never changes it.
    cmf = stock.get("cmf")
    if cmf is not None and buy != sell:
        strength = "strong " if abs(cmf) >= CMF_STRONG else ""
        if buy and cmf >= CMF_THRESHOLD:
            reasons.append(f"Money flow: {strength}accumulation (CMF {cmf:+.2f}) supports the buy case.")
        elif buy and cmf <= -CMF_THRESHOLD:
            reasons.append(f"Money flow: still {strength}distribution (CMF {cmf:+.2f}); "
                           "selling pressure hasn't eased.")
        elif sell and cmf <= -CMF_THRESHOLD:
            reasons.append(f"Money flow: {strength}distribution (CMF {cmf:+.2f}) supports the sell case.")
        elif sell and cmf >= CMF_THRESHOLD:
            reasons.append(f"Money flow: still {strength}accumulation (CMF {cmf:+.2f}); "
                           "buyers remain active.")

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
                 "overvalued" if is_overvalued else \
                 "overbought" if is_overbought else "overextended"
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
    skipped = []                                     # (ticker, reason)
    fetched = 0                                      # real downloads (not cache hits)
    total   = len(tickers)
    icons   = {
        "buy": "[BUY] ", "undervalued": "[UV]  ", "oversold": "[OS]  ",
        "sell": "[SELL]", "overvalued": "[OV]  ", "overbought": "[OB]  ",
        "overextended": "[OX]  ",
        "mixed": "[MIX] ", "neutral": "      ", "hold": "[HOLD]",
    } if ASCII_OUTPUT else {
        "buy": "🔥", "undervalued": "📉", "oversold": "⚠️",
        "sell": "🚩", "overvalued": "📈", "overbought": "🔺",
        "overextended": "🚀",
        "mixed": "❓", "neutral": "  ", "hold": "  ",
    }

    for i, ticker in enumerate(tickers, 1):
        if verbose:
            print(f"  [{i:>4}/{total}] {ticker:<8}", end="", flush=True)

        stock, reason = fetch_stock(ticker)
        from_cache = stock is not None and stock.get("cached_min") is not None
        if not from_cache:
            fetched += 1
        if stock is None:
            skipped.append((ticker, reason))
            if verbose:
                print(f"skipped: {reason}")
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
            cache_s = f"  (cached {stock['cached_min']}m ago)" if from_cache else ""
            print(f"{icon} {kind}{signal:<12}  {pe_s}  {rsi_s}{cache_s}")

        # Polite delay every batch_size downloads to avoid rate limits
        # (cache hits make no requests, so they don't count)
        if not from_cache and fetched % batch_size == 0 and i < total:
            if verbose:
                print(f"\n  Pausing {delay}s to respect rate limits…\n")
            time.sleep(delay)

    if verbose and CACHE is not None:
        print(f"\n  Data: {total - fetched} ticker(s) from cache, {fetched} downloaded")

    if verbose and skipped:
        print(f"\n  Skipped {len(skipped)} of {total} ticker(s):")
        for tkr, why in skipped:
            print(f"    {tkr:<8} {why}")

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
        "overextended": "OVEREXTENDED",
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
        cmf     = s.get("cmf")
        cmf_s   = f"{cmf:+.2f} ({cmf_label(cmf)})" if cmf is not None else "N/A"
        ext_line = None
        if s["ext_price"] is not None:
            ext_s = f"${s['ext_price']:.2f}"
            if s["ext_change"] is not None:
                ext_s += f" ({s['ext_change']:+.2f}%)"
            ext_line = f"{'Pre-mkt' if s['ext_session'] == 'pre' else 'Post-mkt'}: {ext_s}"

        if s["is_fund"]:
            nav_s  = f"{s['nav_premium']:+.2f}% vs NAV" if s["nav_premium"] is not None else "N/A"
            yld_s  = f"{s['fund_yield']:.2f}%" if s["fund_yield"] is not None else "N/A"
            exp_s  = f"{s['expense']:.2f}%"    if s["expense"]    is not None else "N/A"
            beta_s = str(s["beta"]) if s["beta"] is not None else "N/A"
            print(f"\n  {s['ticker']:6}  {s['name']}  [{s['quote_type']}]")
            print(f"  {'─' * (W - 2)}")
            print(f"  Price   : ${s['price']:<10.2f} Today : {chg_s}")
            if ext_line:
                print(f"  {ext_line}")
            print(f"  NAV     : {nav_s:<16} Yield : {yld_s}")
            print(f"  Hold P/E: {pe_s:<16} RSI   : {rsi_s}")
            print(f"  Trend   : {ma200_s:<16} 52wk  : {lo_s} from low / {hi_s} from high")
            print(f"  Expense : {exp_s:<16} Beta  : {beta_s}")
            print(f"  Short   : {short_s:<16} Category: {s['category'] or '—'}")
            print(f"  MoneyFlw: {cmf_s}")
            print(f"  Signal  : {sig_s}")
            print(f"  {s['thesis']}")
            continue

        pb_s    = f"{s['pb']}x"  if s["pb"]  is not None else "N/A"
        peg_s   = f"{s['peg']}x" if s["peg"] is not None else "N/A"
        dte_s   = f"{s['debt_equity']:.0f}%"   if s["debt_equity"]  is not None else "N/A"
        mgn_s   = f"{s['margin']:.1f}%"        if s["margin"]       is not None else "N/A"
        dy_s    = f"{s['div_yield']:.2f}%"     if s["div_yield"]    is not None else "N/A"
        if s["target_price"] is not None:
            tgt_s = f"${s['target_price']:.2f} ({s['target_upside']:+.1f}%)"
            if s["target_low"] is not None and s["target_high"] is not None:
                tgt_s += f"  range ${s['target_low']:.0f}–${s['target_high']:.0f}"
            extra = [x for x in (f"{s['num_analysts']} analysts" if s["num_analysts"] else None,
                                 s["rating"]) if x]
            if extra:
                tgt_s += f"  ({', '.join(extra)})"
        else:
            tgt_s = "N/A"
        print(f"\n  {s['ticker']:6}  {s['name']}")
        print(f"  {'─' * (W - 2)}")
        print(f"  Price  : ${s['price']:<10.2f}  Today : {chg_s}")
        if ext_line:
            print(f"  {ext_line}")
        print(f"  P/E    : {pe_s:<10}  P/B   : {pb_s}")
        print(f"  PEG    : {peg_s:<10}  RSI   : {rsi_s}")
        print(f"  Trend  : {ma200_s:<15}  52wk  : {lo_s} from low / {hi_s} from high")
        print(f"  Debt/Eq: {dte_s:<10}  Margin: {mgn_s}")
        print(f"  Div Yld: {dy_s:<10}  Short : {short_s}")
        print(f"  MnyFlow: {cmf_s}")
        if s.get("earnings_next"):
            n = earnings_days(s)
            warn = "⚠ " if n <= EARNINGS_WARN_DAYS else ""
            print(f"  Earning: {warn}{earnings_when(s, year=True)} "
                  f"({', '.join(earnings_details(s))})")
        print(f"  1y Tgt : {tgt_s}")
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
        "overextended": ("Overextended", "#fefce8", "#a16207"),
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
        cmf     = s.get("cmf")
        if cmf is None:
            cmf_html = "N/A"
        else:
            cmf_c = ("#16a34a" if cmf >= CMF_THRESHOLD else
                     "#dc2626" if cmf <= -CMF_THRESHOLD else "inherit")
            cmf_html = (f'<span style="color:{cmf_c}">{cmf:+.2f}</span>'
                        f'<div class="msub">{cmf_label(cmf)}</div>')
        yahoo_url = "https://finance.yahoo.com/quote/" + urllib.parse.quote(s["ticker"])
        tkr_html = (f'<a class="tkr" href="{yahoo_url}" target="_blank" '
                    f'rel="noopener">{s["ticker"]}</a>')
        ext_html = ""
        if s["ext_price"] is not None:
            ext_lbl = "Pre-market" if s["ext_session"] == "pre" else "After hours"
            ext_c   = "#16a34a" if (s["ext_change"] or 0) >= 0 else "#dc2626"
            ext_pct = (f' <span style="color:{ext_c}">{s["ext_change"]:+.2f}%</span>'
                       if s["ext_change"] is not None else "")
            ext_html = (f'<span class="ext">{ext_lbl} ${s["ext_price"]:.2f}{ext_pct}</span>')
        earn_n    = earnings_days(s)
        earn_soon = earn_n is not None and earn_n <= EARNINGS_WARN_DAYS
        earn_chip = f'<span class="earn">📅 Earnings {_in_days(earn_n)}</span>' if earn_soon else ""
        if s.get("earnings_next"):
            earn_html = (f'<span class="{"warn" if earn_soon else ""}">{earnings_when(s)}</span>'
                         f'<div class="msub">{" &middot; ".join(earnings_details(s))}</div>')
        else:
            earn_html = "N/A"

        if s["is_fund"]:
            nav_s  = f"{s['nav_premium']:+.2f}%" if s["nav_premium"] is not None else "N/A"
            yld_s  = f"{s['fund_yield']:.2f}%" if s["fund_yield"] is not None else "N/A"
            exp_s  = f"{s['expense']:.2f}%"    if s["expense"]    is not None else "N/A"
            beta_s = str(s["beta"]) if s["beta"] is not None else "N/A"
            cards += f"""
        <div class="card">
          <div class="card-top">
            <div>
              <div class="sname">{tkr_html} <span class="price">${s['price']:.2f}</span>
                <span class="kind">{s['quote_type']}</span>{ext_html}</div>
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
            <div><div class="ml">Money flow (CMF 20d)</div><div class="mv">{cmf_html}</div></div>
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
        if s["target_price"] is not None:
            tgt_s = f"${s['target_price']:.2f} ({s['target_upside']:+.1f}%)"
            sub = []
            if s["target_low"] is not None and s["target_high"] is not None:
                sub.append(f"${s['target_low']:.0f}–${s['target_high']:.0f}")
            if s["num_analysts"]:
                sub.append(f"{s['num_analysts']} analysts")
            if s["rating"]:
                sub.append(s["rating"])
            if sub:
                tgt_s += f'<div class="msub">{" &middot; ".join(sub)}</div>'
        else:
            tgt_s = "N/A"
        cards += f"""
        <div class="card">
          <div class="card-top">
            <div>
              <div class="sname">{tkr_html} <span class="price">${s['price']:.2f}</span>{ext_html}{earn_chip}</div>
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
            <div><div class="ml">1y target (consensus)</div><div class="mv">{tgt_s}</div></div>
            <div><div class="ml">Money flow (CMF 20d)</div><div class="mv">{cmf_html}</div></div>
            <div><div class="ml">Next earnings</div><div class="mv">{earn_html}</div></div>
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
.tkr{{color:inherit;text-decoration:none;border-bottom:1px dotted rgba(0,0,0,.35)}}
.tkr:hover{{color:#2563eb;border-bottom-color:#2563eb}}
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
.msub{{font-size:10px;font-weight:400;color:#9f9f9b;margin-top:2px}}
.ext{{font-size:12px;font-weight:400;color:#6b6b67;margin-left:8px;white-space:nowrap}}
.earn{{font-size:11px;font-weight:500;color:#b45309;background:#fffbeb;border-radius:4px;
       padding:1px 6px;margin-left:8px;white-space:nowrap}}
.warn{{color:#b45309}}
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
  python3 stock_screener.py --index sp500 --signal overextended
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
timestamped log in ./logs/ (override with --log-dir; logs older than
--keep-logs days are deleted). Each ticker's data is cached for
--cache-minutes (default 30) in ./cache/; use --no-cache for fresh data.

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
                             "newlines, carriage returns, and/or commas; '#' starts a "
                             "comment to the end of the line "
                             "(overrides --index; --tickers wins over this)")
    parser.add_argument("--signal",
                        choices=["flagged", "buy", "sell", "hold", "all",
                                 "undervalued", "oversold", "overvalued", "overbought",
                                 "overextended"],
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
    parser.add_argument("--keep-logs", type=int, default=30, metavar="DAYS",
                        help="Delete run logs older than DAYS days at startup "
                             "(default: 30; 0 keeps them all)")
    parser.add_argument("--cache-minutes", type=float, default=30, metavar="N",
                        help="Reuse each ticker's downloaded data for N minutes, so "
                             "back-to-back runs over overlapping lists don't download it "
                             "again (default: 30)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Always download fresh data (same as --cache-minutes 0)")
    parser.add_argument("--cache-dir", type=str, default="cache", metavar="DIR",
                        help="Directory for the ticker data cache (default: ./cache)")
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
    old_logs = prune_old_logs(args.log_dir, args.keep_logs)
    log_file, log_path = _open_run_log(args.log_dir)
    sys.stdout = _Tee(sys.__stdout__, log_file, ascii_only=ASCII_OUTPUT)
    sys.stderr = _Tee(sys.__stderr__, log_file, ascii_only=ASCII_OUTPUT)

    global CACHE
    cache_min = 0 if args.no_cache else args.cache_minutes
    if cache_min > 0:
        CACHE = TickerCache(args.cache_dir, cache_min)
        CACHE.prune()

    try:
        if old_logs:
            print(f"Removed {old_logs} run log(s) older than {args.keep_logs} days")

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
        print(f"Cache   : " + (f"reuse data up to {cache_min:g} min old ({os.path.abspath(args.cache_dir)})"
                               if CACHE else "off"))
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
