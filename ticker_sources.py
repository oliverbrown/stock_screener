"""
Live ticker sources for US indices and exchanges.

Each fetcher returns a list of (symbol, name) pairs, symbols already in
Yahoo Finance form (BRK-B, not BRK.B). Used by build_ticker_lists.py to write
reference ticker files, and by stock_screener.py's --index to get current
index members.

Sources (all free, no API key):
    Exchanges       NASDAQ Trader symbol directory (nasdaqlisted.txt /
                    otherlisted.txt, via FTP or HTTPS) — every US
                    exchange-listed security
    S&P 500/400/600 Wikipedia constituent tables
    Nasdaq-100      Nasdaq's own index list (api.nasdaq.com)
    Dow 30          Daily holdings of the SPDR Dow Jones ETF (DIA)
    Russell 2000    Holdings of the iShares Russell 2000 ETF (IWM)
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
# The same files are published on NASDAQ Trader's FTP server and website.
# FTP is tried first: the website sits behind bot protection (Incapsula)
# that intermittently answers scripts with an HTML block page instead.
SYMDIR_MIRRORS = (
    "ftp://ftp.nasdaqtrader.com/SymbolDirectory/",
    "https://www.nasdaqtrader.com/dynamic/SymDir/",
)

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
# ...unless the name says it's common stock ("Preferred Bank - Common Stock").
_COMMON = re.compile(r"(common stock|common shares|ordinary shares)(,? (par value|\$).*)?\s*$",
                     re.IGNORECASE)


def is_non_common(name: str) -> bool:
    """True for warrants, rights, units, preferreds and notes."""
    return bool(_NON_COMMON.search(name)) and not _COMMON.search(name)


def _read_symdir(filename: str) -> pd.DataFrame:
    """Download a NASDAQ Trader symbol-directory file, trying each mirror."""
    errors = []
    for base in SYMDIR_MIRRORS:
        try:
            if base.startswith("ftp:"):
                with urllib.request.urlopen(base + filename, timeout=60) as r:
                    data = r.read()
            else:
                data = fetch_url(base + filename)
            if b"|Security Name|" not in data.split(b"\n", 1)[0]:
                raise ValueError("got an HTML page instead of the symbol file "
                                 "(blocked by bot protection?)")
        except Exception as e:
            errors.append(f"{base}: {e}")
            continue
        df = pd.read_csv(io.BytesIO(data), sep="|", dtype=str, keep_default_na=False)
        return df[~df[df.columns[0]].str.startswith("File Creation Time")]
    raise RuntimeError(f"could not download {filename}: " + "; ".join(errors))


def fetch_exchange_listings(all_securities: bool = False) -> list[dict]:
    """Every US exchange-listed security as dicts with keys
    symbol, name, exchange (nasdaq / nyse / nyse-american / nyse-arca /
    cboe / other) and etf (bool).

    By default only common stocks, ADRs and ETFs are returned; test issues
    are always dropped. all_securities=True keeps warrants, rights, units,
    preferreds and notes too."""
    rows = []

    nasdaq = _read_symdir("nasdaqlisted.txt")
    for r in nasdaq.itertuples(index=False):
        rows.append({
            "raw": r.Symbol, "name": r[1], "exchange": "nasdaq",
            "etf": r.ETF == "Y", "test": r[3] == "Y",
        })

    other = _read_symdir("otherlisted.txt")
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
            if re.search(r"\$|\.(U|W|WS|R)$", r["raw"]) or is_non_common(r["name"]):
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


IWM_HOLDINGS_URL = ("https://www.ishares.com/us/products/239710/"
                    "ishares-russell-2000-etf/latest-holdings.csv")


def parse_ishares_holdings(text: str) -> list[tuple[str, str]]:
    """Listed equity holdings from an iShares fund holdings CSV. The file has
    a few lines of fund info before the "Ticker,Name,…" header; share classes
    are written with a space ("MOG A"); cash, futures, unlisted positions
    (ticker "-", exchange "NO MARKET") and contingent value rights (CVRs,
    left over from acquisitions and not exchange-traded) are skipped."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Ticker,"))
    out = []
    for row in csv.DictReader(lines[start:]):
        tkr = (row.get("Ticker") or "").strip()
        if ((row.get("Asset Class") or "").strip() == "Equity"
                and not (row.get("Exchange") or "").upper().startswith("NO MARKET")
                and not re.search(r"\bCVR\b", row.get("Name") or "")
                and re.fullmatch(r"[A-Z]{1,6}([ .][A-Z])?", tkr)):
            out.append((to_yahoo(tkr.replace(" ", ".")), (row.get("Name") or "").strip()))
    return out


def fetch_russell2000() -> list[tuple[str, str]]:
    """Russell 2000 members from the iShares Russell 2000 ETF (IWM) holdings."""
    return parse_ishares_holdings(fetch_url(IWM_HOLDINGS_URL).decode("utf-8-sig"))


INDEX_FETCHERS = {
    "sp500":     lambda: fetch_sp_index("sp500"),
    "sp400":     lambda: fetch_sp_index("sp400"),
    "sp600":     lambda: fetch_sp_index("sp600"),
    "nasdaq100": fetch_nasdaq100,
    "dow30":     fetch_dow30,
    "russell2000": fetch_russell2000,
}
