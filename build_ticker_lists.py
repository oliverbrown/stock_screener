#!/usr/bin/env python3
"""
Build reference ticker lists for US indices and exchanges.

Writes one file per list, one ticker per line with its name as a comment:

    AAPL     # Apple Inc.
    BRK-B    # Berkshire Hathaway Inc. New

stock_screener.py ignores everything after '#', so any of these files can be
screened directly:

    python3 stock_screener.py --tickers-file ticker-lists/sp400.txt

Index and exchange membership changes over time; re-run this to refresh.

Usage:
    python3 build_ticker_lists.py                  # every list -> ./ticker-lists/
    python3 build_ticker_lists.py sp500 nyse       # just these
    python3 build_ticker_lists.py --list           # show available lists
    python3 build_ticker_lists.py --all-securities  # exchanges incl. warrants,
                                                    # preferreds, units, notes
    python3 build_ticker_lists.py russell2000 --russell2000-csv ~/Downloads/IWM_holdings.csv
"""

import argparse
import os
import sys
from datetime import datetime

import ticker_sources as ts

INDEX_LISTS = {
    "sp500":       "S&P 500",
    "sp400":       "S&P MidCap 400",
    "sp600":       "S&P SmallCap 600",
    "nasdaq100":   "Nasdaq-100",
    "dow30":       "Dow Jones Industrial Average (Dow 30)",
    "russell2000": "Russell 2000 (from an iShares IWM holdings CSV)",
}
EXCHANGE_LISTS = {
    "nasdaq":        "Nasdaq-listed",
    "nyse":          "NYSE-listed",
    "nyse-american": "NYSE American-listed",
    "nyse-arca":     "NYSE Arca-listed",
    "cboe":          "Cboe BZX-listed",
    "us-stocks":     "All US exchange-listed stocks (no ETFs)",
    "us-etfs":       "All US exchange-listed ETFs",
    "us-all":        "All US exchange-listed stocks and ETFs",
}
ALL_LISTS = {**INDEX_LISTS, **EXCHANGE_LISTS}

INDEX_SOURCES = {
    "sp500":       "Wikipedia, List of S&P 500 companies",
    "sp400":       "Wikipedia, List of S&P 400 companies",
    "sp600":       "Wikipedia, List of S&P 600 companies",
    "nasdaq100":   "Nasdaq (api.nasdaq.com index list)",
    "dow30":       "SPDR Dow Jones Industrial Average ETF (DIA) daily holdings",
}
EXCHANGE_SOURCE = "NASDAQ Trader symbol directory (nasdaqlisted.txt, otherlisted.txt)"


def write_list(path: str, title: str, source: str, rows: list[tuple[str, str]],
               note: str = "") -> None:
    rows = sorted(dict(rows).items())                     # de-dupe by symbol, sort
    width = max((len(s) for s, _ in rows), default=0) + 1
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {title} — {len(rows):,} tickers\n")
        f.write(f"# Source: {source}\n")
        if note:
            f.write(f"# {note}\n")
        f.write(f"# Generated {stamp} by build_ticker_lists.py. Membership changes; re-run to refresh.\n")
        f.write(f"# Use with: python3 stock_screener.py --tickers-file {path}\n")
        f.write("\n")
        for sym, name in rows:
            f.write(f"{sym:<{width}}# {name}\n" if name else f"{sym}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Build reference ticker lists (TICKER # name) for US indices and exchanges.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Lists: " + ", ".join(ALL_LISTS),
    )
    parser.add_argument("lists", nargs="*", metavar="LIST",
                        help="Which lists to build (default: all). See --list.")
    parser.add_argument("--out-dir", default="ticker-lists", metavar="DIR",
                        help="Where to write the files (default: ./ticker-lists)")
    parser.add_argument("--all-securities", action="store_true",
                        help="Exchange lists also include warrants, rights, units, "
                             "preferreds and notes (default: common stocks, ADRs and ETFs)")
    parser.add_argument("--russell2000-csv", metavar="FILE",
                        help="iShares IWM holdings CSV to build russell2000.txt from. "
                             "Download it from the IWM fund page on ishares.com "
                             "(Holdings tab -> Detailed Holdings and Analytics). "
                             "iShares blocks automated downloads, so this can't be fetched.")
    parser.add_argument("--list", action="store_true", help="Show available lists and exit")
    args = parser.parse_args()

    if args.list:
        print("\nIndices:")
        for k, v in INDEX_LISTS.items():
            print(f"  {k:<14} {v}")
        print("\nExchanges:")
        for k, v in EXCHANGE_LISTS.items():
            print(f"  {k:<14} {v}")
        print()
        return

    unknown = [k for k in args.lists if k not in ALL_LISTS]
    if unknown:
        parser.error(f"unknown list(s): {', '.join(unknown)}  (see --list)")
    wanted = args.lists or list(ALL_LISTS)
    if "russell2000" in wanted and not args.russell2000_csv:
        if args.lists:
            parser.error("russell2000 needs --russell2000-csv FILE (see --help)")
        wanted.remove("russell2000")
        print("  - russell2000  skipped (needs --russell2000-csv; see --help)")

    os.makedirs(args.out_dir, exist_ok=True)
    failures = 0

    # Exchange listings: one download covers every exchange list, and also
    # supplies clean company names for index sources that only give UPPERCASE.
    listings, names = None, {}
    if any(k in EXCHANGE_LISTS for k in wanted) or "dow30" in wanted:
        try:
            listings = ts.fetch_exchange_listings(all_securities=args.all_securities)
            names = {r["symbol"]: r["name"] for r in listings}
        except Exception as e:
            print(f"  ✗ exchange listings  failed: {e}")

    def done(key, rows, source, note=""):
        path = os.path.join(args.out_dir, f"{key}.txt")
        write_list(path, ALL_LISTS[key], source, rows, note)
        print(f"  ✓ {key:<14} {len(dict(rows)):>6,} tickers  → {path}")

    for key in wanted:
        try:
            if key in EXCHANGE_LISTS:
                if listings is None:
                    raise RuntimeError("exchange listings unavailable")
                if key == "us-stocks":
                    sel = [r for r in listings if not r["etf"]]
                elif key == "us-etfs":
                    sel = [r for r in listings if r["etf"]]
                elif key == "us-all":
                    sel = listings
                else:
                    sel = [r for r in listings if r["exchange"] == key]
                kinds = ("all listed securities" if args.all_securities
                         else "common stocks, ADRs and ETFs; no warrants, rights, units, "
                              "preferreds or notes")
                done(key, [(r["symbol"], r["name"]) for r in sel], EXCHANGE_SOURCE, kinds)
            elif key == "russell2000":
                done(key, ts.parse_ishares_holdings(args.russell2000_csv),
                     f"iShares Russell 2000 ETF (IWM) holdings file {os.path.basename(args.russell2000_csv)}")
            else:
                rows = ts.INDEX_FETCHERS[key]()
                if key == "dow30":                        # DIA gives UPPERCASE names
                    rows = [(s, names.get(s, n)) for s, n in rows]
                done(key, rows, INDEX_SOURCES[key])
        except Exception as e:
            failures += 1
            print(f"  ✗ {key:<14} failed: {e}")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
