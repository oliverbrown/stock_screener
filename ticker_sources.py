"""
Live ticker sources for US indices and exchanges.

Each fetcher returns a list of (symbol, name) pairs, symbols already in
Yahoo Finance form (BRK-B, not BRK.B). Used by build_ticker_lists.py to write
reference ticker files, and by stock_screener.py's --index to get current
index members.

Sources (all free, no API key):
    Exchanges       NASDAQ Trader symbol directory (nasdaqlisted.txt /
                    otherlisted.txt) — every US exchange-listed security
    S&P 500/400/600 Wikipedia constituent tables
    Nasdaq-100      Nasdaq's own index list (api.nasdaq.com)
    Dow 30          Daily holdings of the SPDR Dow Jones ETF (DIA)
    Russell 2000    No free automated source; parse an iShares IWM holdings
                    CSV downloaded in a browser (see parse_ishares_holdings)
"""

import csv
import io
import json
import re
import ssl
import urllib.request

import pandas as pd

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:                                   # fall back to the system store
    _SSL_CONTEXT = ssl.create_default_context()

_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36")


def fetch_url(url: str, timeout: int = 30) -> bytes:
    """GET a URL with a browser User-Agent and certifi's CA bundle.

    The python.org macOS installer ships without system root certificates,
    so plain urllib fails every HTTPS request with CERTIFICATE_VERIFY_FAILED
    unless "Install Certificates.command" was run; certifi avoids that."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as r:
        return r.read()


def to_yahoo(sym: str) -> str:
    """Convert an exchange symbol to Yahoo Finance form.

    Share class       BRK.B  -> BRK-B
    Preferred series  ABR$D  -> ABR-PD
    Warrant           AAC.W  -> AAC-WT
    """
    sym = sym.strip().upper()
    if "$" in sym:
        base, series = sym.split("$", 1)
        return f"{base}-P{series}"
    if sym.endswith(".W") or sym.endswith(".WS"):
        return sym.rsplit(".", 1)[0] + "-WT"
    return sym.replace(".", "-")


def _clean_name(name: str) -> str:
    """Trim the security-type boilerplate NASDAQ Trader appends to names."""
    name = " ".join(str(name).split())
    name = re.sub(r"\s*-?\s*(Class [A-Z] )?(Common Stock|Common Shares|Ordinary Shares)"
                  r"(,? (par value|\$)[^,]*)?$",
                  lambda m: f" - {m.group(1).strip()}" if m.group(1) else "", name)
    return name.strip(" -,")


# ── Exchanges ─────────────────────────────────────────────────────────────────
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL  = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# otherlisted.txt "Exchange" codes
EXCHANGE_CODES = {"N": "nyse", "A": "nyse-american", "P": "nyse-arca", "Z": "cboe"}

# Non-common securities: warrants, rights, SPAC units, preferreds, notes.
# Only applied to non-ETFs, so e.g. a "Preferred Securities ETF" is kept;
# "Common Units" (MLPs) are real equity and also kept.
_NON_COMMON = re.compile(
    r"\bwarrants?\b|\brights?\b|(?<!common )\bunits?\b|\bpreferred\b"
    r"|\bnotes? due\b|\bsenior notes\b|\bdebentures?\b|\bbaby bonds?\b",
    re.IGNORECASE,
)


def _read_symdir(url: str) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(fetch_url(url)), sep="|", dtype=str, keep_default_na=False)
    first = df.columns[0]
    return df[~df[first].str.startswith("File Creation Time")]


def fetch_exchange_listings(all_securities: bool = False) -> list[dict]:
    """Every US exchange-listed security as dicts with keys
    symbol, name, exchange (nasdaq / nyse / nyse-american / nyse-arca /
    cboe / other) and etf (bool).

    By default only common stocks, ADRs and ETFs are returned; test issues
    are always dropped. all_securities=True keeps warrants, rights, units,
    preferreds and notes too."""
    rows = []

    nasdaq = _read_symdir(NASDAQ_LISTED_URL)
    for r in nasdaq.itertuples(index=False):
        rows.append({
            "raw": r.Symbol, "name": r[1], "exchange": "nasdaq",
            "etf": r.ETF == "Y", "test": r[3] == "Y",
        })

    other = _read_symdir(OTHER_LISTED_URL)
    for r in other.itertuples(index=False):
        rows.append({
            "raw": r[0], "name": r[1],
            "exchange": EXCHANGE_CODES.get(r.Exchange, "other"),
            "etf": r.ETF == "Y", "test": r[6] == "Y",
        })

    out = []
    for r in rows:
        if r["test"]:
            continue
        if not all_securities and not r["etf"]:
            # CQS suffixes: $ preferred, .U units, .W warrants, .R rights
            if re.search(r"\$|\.(U|W|WS|R)$", r["raw"]) or _NON_COMMON.search(r["name"]):
                continue
        out.append({
            "symbol":   to_yahoo(r["raw"]),
            "name":     _clean_name(r["name"]),
            "exchange": r["exchange"],
            "etf":      r["etf"],
        })
    return out


# ── Indices ───────────────────────────────────────────────────────────────────
WIKI_SP = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "sp600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}
NASDAQ100_URL = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
DIA_HOLDINGS_URL = ("https://www.ssga.com/us/en/intermediary/library-content/products/"
                    "fund-data/etfs/us/holdings-daily-us-en-dia.xlsx")


def fetch_sp_index(key: str) -> list[tuple[str, str]]:
    """S&P 500 / 400 / 600 members from Wikipedia. The constituents table is
    found by its Symbol + Security columns, not by position."""
    tables = pd.read_html(io.BytesIO(fetch_url(WIKI_SP[key])))
    for t in tables:
        if {"Symbol", "Security"} <= set(map(str, t.columns)):
            return [(to_yahoo(s), str(n)) for s, n in zip(t["Symbol"], t["Security"])
                    if isinstance(s, str) and s.strip()]
    raise ValueError(f"no constituents table found at {WIKI_SP[key]}")


def fetch_nasdaq100() -> list[tuple[str, str]]:
    data = json.loads(fetch_url(NASDAQ100_URL))
    rows = data["data"]["data"]["rows"]
    return [(to_yahoo(r["symbol"]), _clean_name(r["companyName"])) for r in rows]


def fetch_dow30() -> list[tuple[str, str]]:
    """Dow 30 members from the SPDR DIA ETF's daily holdings spreadsheet."""
    sheet = pd.read_excel(io.BytesIO(fetch_url(DIA_HOLDINGS_URL)), header=None)
    header = sheet.index[sheet.iloc[:, 0].astype(str).str.strip().eq("Name")][0]
    t = sheet.iloc[header + 1:, :2]
    t.columns = ["name", "ticker"]
    out = []
    for name, tkr in zip(t["name"], t["ticker"]):
        if isinstance(tkr, str) and re.fullmatch(r"[A-Z.]{1,6}", tkr.strip()):
            out.append((to_yahoo(tkr), str(name).strip()))
    return out


def parse_ishares_holdings(path: str) -> list[tuple[str, str]]:
    """Equity holdings from an iShares fund holdings CSV (e.g. IWM for the
    Russell 2000), as downloaded from the fund page's "Holdings" tab.
    The file has a few lines of fund info before the "Ticker,Name,…" header."""
    with open(path, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Ticker,"))
    out = []
    for row in csv.DictReader(lines[start:]):
        tkr = (row.get("Ticker") or "").strip()
        if (row.get("Asset Class") or "").strip() == "Equity" and re.fullmatch(r"[A-Z.]{1,6}", tkr):
            out.append((to_yahoo(tkr), (row.get("Name") or "").strip()))
    return out


INDEX_FETCHERS = {
    "sp500":     lambda: fetch_sp_index("sp500"),
    "sp400":     lambda: fetch_sp_index("sp400"),
    "sp600":     lambda: fetch_sp_index("sp600"),
    "nasdaq100": fetch_nasdaq100,
    "dow30":     fetch_dow30,
}
