"""Offline tests for batched quotes and the fundamentals cache: how many
requests each ticker costs, and that cached fundamentals stay correct."""

import pytest

import stock_screener as ss
from fake_yahoo import FakeYahoo

DAY = 86400


@pytest.fixture
def clock(monkeypatch):
    now = [2_000_000_000.0]
    monkeypatch.setattr(ss.time, "time", lambda: now[0])
    return now


@pytest.fixture
def fund_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "FUND_CACHE_DIR", str(tmp_path / "fundamentals"))
    monkeypatch.setattr(ss, "FUNDAMENTALS_DAYS", 7)
    monkeypatch.setattr(ss, "CACHE", None)
    monkeypatch.setattr(ss, "CACHE_ONLY", False)
    return tmp_path / "fundamentals"


def stock_quote(price=100.0, **f):
    return {"regularMarketPrice": price, "quoteType": "EQUITY", "marketCap": int(price * 1e7),
            "trailingPE": price / 5, "currency": "USD", "financialCurrency": "USD", **f}


SUMMARY = {"sector": "Technology", "profitMargins": 0.2, "marketCap": 1_000_000_000,
           "enterpriseValue": 1_200_000_000, "enterpriseToEbitda": 12.0, "freeCashflow": 50_000_000}


def fetch(t):
    ss._QUOTES.clear()                                # a new run: no batched quotes yet
    return ss._fetch_stock_once(t)


# ── Requests per ticker ───────────────────────────────────────────────────────
def test_first_download_then_cached_fundamentals(fund_cache, clock, monkeypatch):
    y = FakeYahoo(monkeypatch, {"AAA": stock_quote()}, {"AAA": SUMMARY}, {"AAA": 1.5})
    fetch("AAA")
    assert [c[0] for c in y.calls] == ["quote", "summary", "peg", "history"]

    y.calls.clear()
    clock[0] += 3 * DAY                               # three days later
    fetch("AAA")
    assert [c[0] for c in y.calls] == ["quote", "history"]   # fundamentals from the cache


def test_stale_fundamentals_are_refetched(fund_cache, clock, monkeypatch):
    y = FakeYahoo(monkeypatch, {"AAA": stock_quote()}, {"AAA": SUMMARY})
    fetch("AAA")
    clock[0] += 8 * DAY
    y.calls.clear()
    fetch("AAA")
    assert y.count("summary") == 1 and y.count("peg") == 1


def test_funds_refresh_daily_and_skip_peg(fund_cache, clock, monkeypatch):
    q = {"regularMarketPrice": 500.0, "quoteType": "ETF", "currency": "USD"}
    y = FakeYahoo(monkeypatch, {"VOO": q}, {"VOO": {"navPrice": 500.1, "category": "Large Blend"}})
    fetch("VOO")
    assert y.count("peg") == 0                        # funds have no PEG
    clock[0] += 0.5 * DAY
    y.calls.clear()
    fetch("VOO")
    assert y.count("summary") == 0                    # same day: cached
    clock[0] += 1 * DAY
    y.calls.clear()
    fetch("VOO")
    assert y.count("summary") == 1                    # NAV is daily: refetched next day


def test_zero_days_always_refetches(fund_cache, clock, monkeypatch):
    monkeypatch.setattr(ss, "FUNDAMENTALS_DAYS", 0)
    y = FakeYahoo(monkeypatch, {"AAA": stock_quote()}, {"AAA": SUMMARY})
    fetch("AAA")
    fetch("AAA")
    assert y.count("summary") == 2


def test_no_fundamentals_cache_dir(clock, monkeypatch):
    monkeypatch.setattr(ss, "FUND_CACHE_DIR", None)   # --no-cache
    y = FakeYahoo(monkeypatch, {"AAA": stock_quote()}, {"AAA": SUMMARY})
    fetch("AAA")
    fetch("AAA")
    assert y.count("summary") == 2


def test_corrupt_fundamentals_entry_is_refetched(fund_cache, clock, monkeypatch):
    y = FakeYahoo(monkeypatch, {"AAA": stock_quote()}, {"AAA": SUMMARY})
    fetch("AAA")
    (fund_cache / "AAA.json").write_text("{oops")
    y.calls.clear()
    fetch("AAA")
    assert y.count("summary") == 1


# ── Cached fundamentals + a fresh price stay correct ──────────────────────────
def test_prices_and_price_ratios_are_fresh(fund_cache, clock, monkeypatch):
    quotes = {"AAA": stock_quote(100.0)}
    FakeYahoo(monkeypatch, quotes, {"AAA": SUMMARY}, {"AAA": 1.5})
    first = fetch("AAA")
    assert (first["price"], first["peg"], first["ev_ebitda"]) == (100.0, 1.5, 12.0)

    clock[0] += 2 * DAY
    quotes["AAA"] = stock_quote(110.0)                # price up 10%; market cap 1.1B
    d = fetch("AAA")
    assert d["price"] == 110.0                        # from the fresh quote
    assert d["pe"] == 22.0                            # P/E comes with the quote
    assert d["peg"] == 1.65                           # cached PEG scaled with the price
    # EV moves with market cap (1.0B -> 1.1B): EBITDA 100M, EV 1.3B
    assert d["ev_ebitda"] == 13.0
    assert d["fcf_yield"] == 4.5                      # cached FCF / fresh market cap
    assert d["margin"] == 20.0 and d["sector"] == "Technology"


def test_merge_info_prefers_quote_fields():
    info = ss.merge_info({"trailingPE": 10.0, "currentPrice": 50.0, "averageVolume": 1},
                         {"trailingPE": 12.0, "regularMarketPrice": 60.0,
                          "averageDailyVolume3Month": 99}, None, None)
    assert (info["trailingPE"], info["currentPrice"], info["averageVolume"]) == (12.0, 60.0, 99)


# ── Batched quotes ────────────────────────────────────────────────────────────
def test_quotes_are_batched_and_unknown_tickers_cost_nothing_more(fund_cache, clock, monkeypatch):
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)          # skip the batch pauses
    tickers = [f"T{i}" for i in range(250)]
    y = FakeYahoo(monkeypatch, {t: stock_quote() for t in tickers[:-1]}, {})   # last one unknown
    matched, screened = ss.run_screen(tickers, signal_filter="all", verbose=False)
    quote_calls = [c for c in y.calls if c[0] == "quote"]
    assert [len(c[1]) for c in quote_calls[:3]] == [100, 100, 51]   # 250 tickers + SPY
    assert screened == 249
    assert ("history", "T249") not in y.calls         # unknown: skipped before its history
    assert y.count("quote") == 3                      # no one-by-one retries for SPY / T249


def test_cached_tickers_get_no_quote(fund_cache, clock, monkeypatch, tmp_path):
    cache = ss.TickerCache(str(tmp_path / "cache"), 30)
    cache.put("OLD", {"ticker": "OLD"})
    monkeypatch.setattr(ss, "CACHE", cache)
    seen = []
    monkeypatch.setattr(ss, "prefetch_quotes", lambda syms, verbose=False: seen.extend(syms))
    monkeypatch.setattr(ss, "fetch_stock", lambda t: (None, "x"))
    ss.run_screen(["OLD", "NEW"], signal_filter="all", verbose=False)
    assert seen == ["SPY", "NEW"]


# ── Fallback if yfinance's internals change ───────────────────────────────────
def test_falls_back_to_yfinance_info(monkeypatch):
    monkeypatch.setattr(ss, "YfData", None)

    class T:
        info = {"regularMarketPrice": 5.0, "forwardPE": "Infinity"}

    assert ss.get_info("X", T()) == {"regularMarketPrice": 5.0, "forwardPE": None}


def test_prune_fundamentals(fund_cache, monkeypatch):
    import os, time as real_time
    fund_cache.mkdir()
    for name, age_days in (("OLD.json", 9), ("NEW.json", 2)):
        p = fund_cache / name
        p.write_text("{}")
        t = real_time.time() - age_days * DAY
        os.utime(p, (t, t))
    assert ss.prune_fundamentals(8) == 1
    assert [p.name for p in fund_cache.iterdir()] == ["NEW.json"]
