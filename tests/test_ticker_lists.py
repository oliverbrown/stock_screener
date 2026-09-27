"""Offline tests for ticker-list files: the "TICKER # name" format, symbol
conversion, list writing, iShares CSV parsing and --index fallback."""

import pytest

import build_ticker_lists as btl
import stock_screener as ss
import ticker_sources as ts


# ── Comments in ticker files ──────────────────────────────────────────────────
def test_comment_after_ticker_is_ignored():
    text = "AAPL     # Apple Inc.\nBRK-B    # Berkshire Hathaway, Class B\n"
    assert ss.parse_tickers(text) == ["AAPL", "BRK-B"]


def test_comment_can_contain_ticker_like_words():
    assert ss.parse_tickers("KO # not SPY, not QQQ\nXOM") == ["KO", "XOM"]


def test_whole_line_and_header_comments_are_ignored():
    text = "# S&P 500 — 503 tickers\n# Source: somewhere\n\nMMM # 3M\n#AOS\nABT\n"
    assert ss.parse_tickers(text) == ["MMM", "ABT"]


def test_space_separated_format_still_works():
    assert ss.parse_tickers("AAPL MSFT GOOGL\nTSLA, NVDA") == ["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"]


def test_mixed_formats_in_one_file():
    text = "AAPL MSFT  # two on one line\nKO\t# tab before comment\nXOM,CVX"
    assert ss.parse_tickers(text) == ["AAPL", "MSFT", "KO", "XOM", "CVX"]


# ── Symbol / name conversion ──────────────────────────────────────────────────
@pytest.mark.parametrize("raw, expected", [
    ("AAPL",  "AAPL"),
    ("BRK.B", "BRK-B"),
    ("MOG.A", "MOG-A"),
    ("ABR$D", "ABR-PD"),
    ("AAC.W", "AAC-WT"),
    ("aac.u", "AAC-U"),
])
def test_to_yahoo(raw, expected):
    assert ts.to_yahoo(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Apple Inc. Common Stock",                         "Apple Inc."),
    ("Alcoa Corporation Common Stock ",                 "Alcoa Corporation"),
    ("Alphabet Inc. - Class A Common Stock",            "Alphabet Inc. - Class A"),
    ("Foo Corp Class B Common Stock, $0.01 par value",  "Foo Corp - Class B"),
    ("Abivax SA - American Depositary Shares",          "Abivax SA - American Depositary Shares"),
    ("Enterprise Products Partners L.P. Common Units",  "Enterprise Products Partners L.P. Common Units"),
])
def test_clean_name(raw, expected):
    assert ts._clean_name(raw) == expected


@pytest.mark.parametrize("name, non_common", [
    ("Armada Acquisition Corp. III - Warrant",                       True),
    ("Abony Acquisition Corp. I - Units",                            True),
    ("CELG Contingent Value Rights",                                 True),
    ("Arbor Realty 6.375% Series D Cumulative Preferred Stock",      True),
    ("Foo Inc. 5.50% Senior Notes due 2029",                         True),
    ("Enterprise Products Partners L.P. Common Units",               False),
    ("Apple Inc. Common Stock",                                      False),
    ("Arm Holdings plc - American Depositary Shares",                False),
    ("Preferred Bank - Common Stock",                                False),
    ("First Rights Corp Common Stock, $0.01 par value",              False),
    ("Ares Acquisition Corp III Units, each consisting of one Class A "
     "ordinary share and one-tenth of one redeemable warrant",       True),
])
def test_non_common_filter(name, non_common):
    assert ts.is_non_common(name) is non_common


# ── Written lists round-trip through the screener's parser ────────────────────
def test_write_list_round_trip(tmp_path):
    path = tmp_path / "x.txt"
    btl.write_list(str(path), "Test list", "unit test",
                   [("MSFT", "Microsoft Corporation"), ("AAPL", "Apple Inc."),
                    ("BRK-B", "Berkshire Hathaway # Class B"), ("AAPL", "Apple Inc."), ("NONAME", "")],
                   note="a note")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Test list — 4 tickers\n# Source: unit test\n# a note\n")
    assert "AAPL   # Apple Inc.\n" in text
    assert "NONAME\n" in text
    assert ss.load_tickers_file(str(path)) == ["AAPL", "BRK-B", "MSFT", "NONAME"]


# ── iShares holdings CSV (Russell 2000 via IWM) ───────────────────────────────
ISHARES_CSV = '''iShares Russell 2000 ETF
Fund Holdings as of,"Sep 25, 2026"
Inception Date,"May 22, 2000"
Shares Outstanding,"273,950,000.00"
Stock,"-"

Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Quantity,Price,Location,Exchange,Currency,FX Rate,Market Currency,Accrual Date
"TWST","TWIST BIOSCIENCE","Health Care","Equity","312,780,993.25","0.40","312,780,993.25","1,710,775.00","182.83","United States","NASDAQ","USD","1.00","USD","-"
"MOG A","MOOG INC CLASS A","Industrials","Equity","286,960,753.80","0.37","286,960,753.80","739,971.00","387.80","United States","NYSE","USD","1.00","USD","-"
"XTSLA","BLK CSH FND TREASURY SL AGENCY","Cash and/or Derivatives","Money Market","500","0.20","500","500","1.00","United States","-","USD","1.00","USD","-"
"-","OMNIAB INC $15.00 VESTING Prvt","Health Care","Equity","1.31","0.00","1.31","130,676.00","0.00","United States","NO MARKET (E.G. UNLISTED)","USD","1.00","USD","-"
"CRDO","CREDO TECHNOLOGY","Information Technology","Equity","1","0.10","1","1","1","United States","NO MARKET (E.G. UNLISTED)","USD","1.00","USD","-"
"AKE","AKERO THERAPEUTICS CVR","Health Care","Equity","0","0.00","0","1","0","United States","NASDAQ","USD","1.00","USD","-"
"RTYZ6","RUSSELL 2000 EMINI CME DEC 26","Cash and/or Derivatives","Futures","0.00","0.00","287,359,650.00","2,010.00","2,859.30","-","Chicago Mercantile Exchange","USD","1.00","USD","-"
'''


def test_parse_ishares_holdings():
    assert ts.parse_ishares_holdings(ISHARES_CSV) == [
        ("TWST", "TWIST BIOSCIENCE"),
        ("MOG-A", "MOOG INC CLASS A"),
    ]


# ── --index: live list with bundled fallback ──────────────────────────────────
def test_load_index_uses_live_list(monkeypatch, capsys):
    monkeypatch.setitem(ts.INDEX_FETCHERS, "dow30", lambda: [("AAPL", "Apple"), ("NVDA", "NVIDIA")])
    assert ss.load_index("dow30") == ["AAPL", "NVDA"]
    assert "Loaded 2 current tickers" in capsys.readouterr().out


def test_load_index_falls_back_to_bundled(monkeypatch, capsys):
    def offline():
        raise OSError("no network")
    monkeypatch.setitem(ts.INDEX_FETCHERS, "dow30", offline)
    assert ss.load_index("dow30") == ss.DOW30_TICKERS
    out = capsys.readouterr().out
    assert "Live index fetch failed (no network)" in out
    assert "Using bundled list" in out


def test_every_live_index_has_a_fetcher():
    for key, meta in ss.INDICES.items():
        if meta.get("live"):
            assert key in ts.INDEX_FETCHERS
