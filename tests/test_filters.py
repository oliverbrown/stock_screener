"""Offline tests for --where filters, sorting, --top, watch-list and CSV
export, --cache-only, and the new data fields."""

import csv
import math

import pytest

import stock_screener as ss


def stock(**f):
    base = {"ticker": "X", "name": "X Corp", "sector": "Technology", "quote_type": "EQUITY",
            "signal": "neutral", "tags": [], "pe": 20.0, "rsi": 50.0, "market_cap": 5_000_000_000,
            "returns": {"3m": 1.0, "6m": 2.0, "1y": 3.0}, "rel": {"3m": -1.0, "6m": 0.5, "1y": 4.0}}
    base.update(f)
    return base


# ── Parsing ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text, field, op, value", [
    ("pe<20",               "pe",         "<",  20.0),
    (" rsi <= 35 ",         "rsi",        "<=", 35.0),
    ("market_cap>=2B",      "market_cap", ">=", 2e9),
    ("dollar_volume>500k",  "dollar_volume", ">", 5e5),
    ("fcf_yield>=3%",       "fcf_yield",  ">=", 3.0),
    ("market_cap>1,000,000","market_cap", ">",  1e6),
    ("pct_off_high>-10",    "pct_off_high", ">", -10.0),
    ("PE==15",              "pe",         "=",  15.0),
    ("sector=Technology",   "sector",     "=",  "technology"),
    ("sector!='Real Estate'", "sector",   "!=", "real estate"),
])
def test_parse(text, field, op, value):
    c = ss.Condition(text)
    assert (c.field, c.op, c.value) == (field, op, value)


@pytest.mark.parametrize("text, message", [
    ("pe",            "can't parse"),
    ("pee<3",         "did you mean pe"),
    ("sector>3",      "use = or !="),
    ("pe<cheap",      "isn't a number"),
])
def test_parse_errors(text, message):
    with pytest.raises(ValueError, match=message):
        ss.Condition(text)


# ── Matching ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cond, fields, ok", [
    ("pe<25",              {},                          True),
    ("pe<15",              {},                          False),
    ("pe<15",              {"pe": None},                False),     # missing never matches
    ("pe!=15",             {"pe": None},                False),
    ("rsi<35",             {"rsi": math.nan},           False),     # NaN counts as missing
    ("market_cap>=2B",     {},                          True),
    ("sector=technology",  {},                          True),      # case-insensitive
    ("sector!=Technology", {},                          False),
    ("tag=oversold",       {"tags": ["oversold"]},      True),
    ("tag=oversold",       {"tags": ["overbought"]},    False),
    ("tag!=overvalued",    {"tags": ["oversold"]},      True),
    ("rel_1y>0",           {},                          True),
    ("ret_3m>5",           {},                          False),
    ("rel_1y>0",           {"rel": None},               False),
])
def test_matches(cond, fields, ok):
    assert ss.Condition(cond).matches(stock(**fields)) is ok


def test_earnings_days_field(monkeypatch):
    from datetime import date
    monkeypatch.setattr(ss, "market_today", lambda: date(2026, 9, 29))
    s = stock(earnings_next={"date": "2026-10-02", "end": "2026-10-02", "time": None, "estimate": False})
    assert ss.Condition("earnings_days<=7").matches(s)
    assert not ss.Condition("earnings_days>7").matches(s)
    assert not ss.Condition("earnings_days>7").matches(stock())   # no date: excluded


def test_every_field_getter_runs():
    for name, (_, _, _, getter) in ss.FIELDS.items():
        getter(stock())                              # must not raise on a typical row


# ── Filtering, sorting, top ───────────────────────────────────────────────────
ROWS = [stock(ticker="A", pe=30.0), stock(ticker="B", pe=10.0), stock(ticker="C", pe=None),
        stock(ticker="D", pe=15.0), stock(ticker="E", pe=12.0, sector="Energy")]


def test_all_conditions_must_match():
    out = ss.apply_filters(ROWS, [ss.Condition("pe<20"), ss.Condition("sector=Technology")])
    assert [s["ticker"] for s in out] == ["B", "D"]


@pytest.mark.parametrize("spec, order", [
    ("pe",        ["B", "E", "D", "A", "C"]),       # missing sorts last
    ("pe:desc",   ["A", "D", "E", "B", "C"]),       # ...either way
    ("ticker:desc", ["E", "D", "C", "B", "A"]),
])
def test_sort(spec, order):
    assert [s["ticker"] for s in ss.apply_filters(ROWS, sort=ss.parse_sort(spec))] == order


def test_top():
    out = ss.apply_filters(ROWS, sort=ss.parse_sort("pe"), top=2)
    assert [s["ticker"] for s in out] == ["B", "E"]


@pytest.mark.parametrize("spec", ["nope", "tag", "pe:sideways"])
def test_sort_errors(spec):
    with pytest.raises(ValueError):
        ss.parse_sort(spec)


def test_describe_filters():
    conds = [ss.Condition("pe<20"), ss.Condition("rsi < 35")]
    assert ss.describe_filters(conds, ("fcf_yield", True), 10) == \
        "pe<20, rsi < 35, sorted by fcf_yield desc, top 10"


# ── Watch list and CSV ────────────────────────────────────────────────────────
def test_save_tickers_round_trip(tmp_path):
    path = tmp_path / "watch.txt"
    ss.save_ticker_list([stock(ticker="BRK-B", name="Berkshire Hathaway"), stock(ticker="KO", name="Coca-Cola")],
                        str(path), "pe<20")
    text = path.read_text(encoding="utf-8")
    assert "# Criteria: pe<20" in text
    assert "BRK-B # Berkshire Hathaway" in text
    assert ss.load_tickers_file(str(path)) == ["BRK-B", "KO"]


def test_csv(tmp_path):
    path = tmp_path / "out.csv"
    ss.save_csv([stock(ticker="A", tags=["oversold", "undervalued"], thesis="x",
                       earnings_next={"date": "2026-10-02"})], str(path))
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert rows[0]["ticker"] == "A" and rows[0]["pe"] == "20.0"
    assert rows[0]["rel_1y"] == "4.0" and rows[0]["peg"] == ""
    assert rows[0]["tags"] == "oversold undervalued" and rows[0]["earnings_date"] == "2026-10-02"
    assert "tag" not in rows[0]


# ── --cache-only ──────────────────────────────────────────────────────────────
def test_cache_only_never_downloads(tmp_path, monkeypatch):
    cache = ss.TickerCache(str(tmp_path), max_age_min=24 * 60)
    cache.put("AAPL", {"ticker": "AAPL"})
    monkeypatch.setattr(ss, "CACHE", cache)
    monkeypatch.setattr(ss, "CACHE_ONLY", True)
    monkeypatch.setattr(ss, "_fetch_stock_once", lambda t: pytest.fail("downloaded in --cache-only"))
    assert ss.fetch_stock("AAPL")[0]["ticker"] == "AAPL"
    assert ss.fetch_stock("MSFT") == (None, "not in cache")


def test_many_skips_are_grouped(monkeypatch, capsys):
    monkeypatch.setattr(ss, "fetch_stock", lambda t: (None, "not in cache"))
    ss.run_screen([f"T{i}" for i in range(40)], signal_filter="all", verbose=True, progress=False)
    out = capsys.readouterr().out
    assert "Skipped 40 of 40" in out
    assert "40 × not in cache: T0, T1" in out and "(+32)" in out
    assert "[   1/40]" not in out                    # progress=False: no per-ticker lines


# ── New data fields from Yahoo's quote info ───────────────────────────────────
def test_new_fields_from_info(monkeypatch):
    from fake_yahoo import FakeYahoo
    quote = {"regularMarketPrice": 100.0, "quoteType": "EQUITY", "marketCap": 2_000_000_000,
             "averageDailyVolume3Month": 50_000, "forwardPE": 18.44,
             "financialCurrency": "USD", "currency": "USD"}
    summary = {"returnOnEquity": 0.1776, "enterpriseToEbitda": 12.34}
    FakeYahoo(monkeypatch, {"X": quote}, {"X": summary})
    d = ss._fetch_stock_once("X")
    assert (d["market_cap"], d["avg_volume"], d["dollar_volume"]) == (2_000_000_000, 50_000, 5_000_000)
    assert (d["forward_pe"], d["roe"], d["ev_ebitda"]) == (18.4, 17.8, 12.3)
    quote.update(financialCurrency="JPY")
    ss._QUOTES.clear()
    assert ss._fetch_stock_once("X")["ev_ebitda"] is None     # ADR currency mismatch


# ── Bad numbers from Yahoo ("Infinity" text, inf, NaN) ───────────────────────
def test_clean_info():
    raw = {"forwardPE": "Infinity", "trailingPE": float("inf"), "priceToBook": float("nan"),
           "pegRatio": "-Infinity", "beta": 1.2, "longName": "Infinity Corp", "sector": "NaN-ish"}
    out = ss.clean_info(raw)
    assert out["forwardPE"] is None and out["trailingPE"] is None
    assert out["priceToBook"] is None and out["pegRatio"] is None
    assert out["beta"] == 1.2 and out["longName"] == "Infinity Corp" and out["sector"] == "NaN-ish"


def test_infinity_forward_pe_does_not_crash(monkeypatch):
    """Regression: ANTA, BCHT, BDRX, CTSO were skipped with
    "TypeError: '>' not supported between instances of 'str' and 'int'"."""
    from fake_yahoo import FakeYahoo, flat_history
    FakeYahoo(monkeypatch,
              {"ANTA": {"regularMarketPrice": 10.0, "quoteType": "EQUITY",
                        "forwardPE": "Infinity", "trailingPE": "Infinity"}},
              {"ANTA": {"enterpriseToEbitda": "Infinity"}}, history=flat_history(10.0))
    d = ss._fetch_stock_once("ANTA")
    assert d["forward_pe"] is None and d["pe"] is None and d["ev_ebitda"] is None
