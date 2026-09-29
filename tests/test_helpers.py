"""Offline tests for ticker parsing, output naming, RSI, fetch retries and the
signal filter. Network calls are replaced with fakes."""

import math

import pandas as pd
import pytest

import stock_screener as ss


# ── Ticker parsing ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw, expected", [
    ("BRK.B",   "BRK-B"),
    ("bf.b",    "BF-B"),
    ("BRK-B",   "BRK-B"),
    ("SHOP.TO", "SHOP.TO"),    # exchange suffix, not a share class
    ("VOD.L",   "VOD.L"),
    ("7203.T",  "7203.T"),
    (" aapl ",  "AAPL"),
])
def test_yahoo_symbol(raw, expected):
    assert ss.yahoo_symbol(raw) == expected


def test_parse_tickers_mixed_separators():
    text = "AAPL, MSFT GOOGL\nTSLA\tNVDA,,amzn\r\n"
    assert ss.parse_tickers(text) == ["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA", "AMZN"]


def test_parse_tickers_dedupes_after_normalising():
    assert ss.parse_tickers("BRK.B brk-b AAPL aapl") == ["BRK-B", "AAPL"]


def test_parse_tickers_empty():
    assert ss.parse_tickers("  \n , ") == []


def test_load_tickers_file(tmp_path):
    f = tmp_path / "list.txt"
    f.write_text("AAPL, MSFT\nGOOGL\n", encoding="utf-8")
    assert ss.load_tickers_file(str(f)) == ["AAPL", "MSFT", "GOOGL"]


def test_load_tickers_file_empty_exits(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        ss.load_tickers_file(str(f))


def test_bundled_lists_use_yahoo_symbols():
    for meta in ss.INDICES.values():
        for sym in meta["static"]:
            assert ss.yahoo_symbol(sym) == sym, sym


# ── Output filename ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("output, index, expected", [
    (None,                None,    None),
    ("daily.html",        "sp500", "daily-sp500.html"),
    ("daily",             "dow30", "daily-dow30.html"),
    ("report-{index}.html", "sp500", "report-sp500.html"),
    ("report-{index}.html", None,  "report-watchlist.html"),
    ("mine.html",         None,    "mine.html"),
])
def test_resolve_output_path(output, index, expected):
    assert ss.resolve_output_path(output, index) == expected


# ── RSI ───────────────────────────────────────────────────────────────────────
def test_rsi_needs_enough_history():
    assert math.isnan(ss.compute_rsi(pd.Series(range(10), dtype=float)))


def test_rsi_steady_fall_is_zero():
    assert ss.compute_rsi(pd.Series(range(60, 0, -1), dtype=float)) == 0.0


def test_rsi_balanced_moves_is_mid_range():
    prices = pd.Series([100 + (1 if i % 2 else -1) for i in range(60)], dtype=float)
    assert 40 <= ss.compute_rsi(prices) <= 60


def test_rsi_mostly_rising_is_high():
    prices = pd.Series([100 + i * 2 - (3 if i % 5 == 0 else 0) for i in range(60)], dtype=float)
    assert ss.compute_rsi(prices) > 70


# ── fetch_stock: skip reasons and rate-limit retries ──────────────────────────
@pytest.fixture
def no_sleep(monkeypatch):
    """Fake clock: sleep() records the delay and advances time() instead of
    waiting, so the shared rate-limit cooldown resolves instantly."""
    clock, sleeps = [1_000_000.0], []

    def fake_sleep(seconds):
        sleeps.append(round(seconds, 6))
        clock[0] += seconds

    monkeypatch.setattr(ss.time, "time", lambda: clock[0])
    monkeypatch.setattr(ss.time, "sleep", fake_sleep)
    monkeypatch.setattr(ss, "_cooldown_until", 0.0)
    return sleeps


def test_fetch_success(monkeypatch, no_sleep):
    monkeypatch.setattr(ss, "_fetch_stock_once", lambda t: {"ticker": t})
    assert ss.fetch_stock("AAPL") == ({"ticker": "AAPL"}, None)
    assert no_sleep == []


def test_fetch_skip_reason(monkeypatch, no_sleep):
    def skip(t):
        raise ss.SkipTicker("no price data")
    monkeypatch.setattr(ss, "_fetch_stock_once", skip)
    assert ss.fetch_stock("ZZZZ") == (None, "no price data")
    assert no_sleep == []


def test_fetch_other_error_is_reported_not_retried(monkeypatch, no_sleep):
    def boom(t):
        raise ValueError("bad   data\nhere")
    monkeypatch.setattr(ss, "_fetch_stock_once", boom)
    assert ss.fetch_stock("X") == (None, "ValueError: bad data here")
    assert no_sleep == []


def test_fetch_long_error_is_truncated(monkeypatch, no_sleep):
    def boom(t):
        raise RuntimeError("x" * 200)
    monkeypatch.setattr(ss, "_fetch_stock_once", boom)
    _, reason = ss.fetch_stock("X")
    assert len(reason) < 100
    assert reason.endswith("…")


@pytest.mark.parametrize("exc", [
    ss.YFRateLimitError(),
    Exception("429 Client Error: Too Many Requests"),
])
def test_fetch_retries_rate_limit_then_succeeds(monkeypatch, no_sleep, exc):
    calls = []

    def flaky(t):
        calls.append(t)
        if len(calls) < 3:
            raise exc
        return {"ticker": t}

    monkeypatch.setattr(ss, "_fetch_stock_once", flaky)
    assert ss.fetch_stock("AAPL") == ({"ticker": "AAPL"}, None)
    assert no_sleep == list(ss.RATE_LIMIT_BACKOFF[:2])


def test_fetch_gives_up_after_retries(monkeypatch, no_sleep):
    def always(t):
        raise ss.YFRateLimitError()
    monkeypatch.setattr(ss, "_fetch_stock_once", always)
    data, reason = ss.fetch_stock("AAPL")
    assert data is None
    assert "rate-limited" in reason
    assert no_sleep == list(ss.RATE_LIMIT_BACKOFF)


# ── run_screen: signal and asset-type filters ─────────────────────────────────
def _row(ticker, quote_type="EQUITY", **fields):
    base = {
        "ticker": ticker, "quote_type": quote_type, "is_fund": quote_type != "EQUITY",
        "sector": "Technology", "category": "Large Blend",
        "pe": 28.0, "pb": 5.0, "peg": 1.5, "rsi": 50.0,
        "debt_equity": 50.0, "margin": 20.0, "nav_premium": 0.0, "expense": 0.05,
        "pct_vs_200": 5.0, "pct_off_high": -10.0, "pct_off_low": 30.0, "trend": "up",
    }
    base.update(fields)
    return base


UNIVERSE = {
    "CHEAP": _row("CHEAP", pe=20.0),                       # undervalued
    "HOT":   _row("HOT", rsi=75.0),                        # overbought
    "RUN":   _row("RUN", pct_vs_200=25.0),                 # overextended
    "MEH":   _row("MEH"),                                  # neutral
    "FUND":  _row("FUND", "ETF", pe=21.0, nav_premium=-2.0),  # undervalued ETF
    "IDX":   _row("IDX", "MUTUALFUND", pe=21.0),           # hold, not an ETF
}


@pytest.fixture
def fake_fetch(monkeypatch):
    monkeypatch.setattr(ss, "fetch_stock",
                        lambda t: (dict(UNIVERSE[t]), None) if t in UNIVERSE else (None, "unknown"))


def _screen(signal="all", asset_type="all"):
    matched, screened = ss.run_screen(list(UNIVERSE) + ["NOPE"], signal_filter=signal,
                                      asset_type=asset_type, verbose=False)
    return [s["ticker"] for s in matched], screened


@pytest.mark.parametrize("signal, expected", [
    ("all",          ["CHEAP", "HOT", "RUN", "MEH", "FUND", "IDX"]),
    ("flagged",      ["CHEAP", "HOT", "RUN", "FUND"]),
    ("buy",          ["CHEAP", "FUND"]),
    ("sell",         ["HOT", "RUN"]),
    ("hold",         ["MEH", "IDX"]),
    ("overbought",   ["HOT"]),
    ("overextended", ["RUN"]),
    ("undervalued",  ["CHEAP", "FUND"]),
])
def test_signal_filter(fake_fetch, signal, expected):
    tickers, screened = _screen(signal)
    assert tickers == expected
    assert screened == 6        # NOPE was skipped, not screened


@pytest.mark.parametrize("asset_type, expected", [
    ("stock", ["CHEAP", "HOT", "RUN", "MEH"]),
    ("etf",   ["FUND"]),
])
def test_asset_type_filter(fake_fetch, asset_type, expected):
    assert _screen("all", asset_type)[0] == expected


# ── Every headline signal has display text in every output ────────────────────
def test_overextended_has_display_everywhere(tmp_path, capsys):
    stock = {**_row("RUN", pct_vs_200=25.0), "name": "Run Inc", "price": 100.0,
             "change1d": 1.0, "wk_high": 110.0, "wk_low": 70.0, "pct_short": None,
             "div_yield": None, "target_price": None, "target_upside": None,
             "target_low": None, "target_high": None, "num_analysts": None,
             "rating": None, "ext_session": None, "ext_price": None, "ext_change": None}
    stock["signal"], stock["tags"], stock["thesis"] = ss.classify_signal(stock, None)
    assert stock["signal"] == "overextended"

    ss.print_report([stock], 1)
    assert "Signal : OVEREXTENDED" in capsys.readouterr().out

    out = tmp_path / "r.html"
    ss.save_html_report([stock], 1, str(out))
    assert ">Overextended</span>" in out.read_text(encoding="utf-8")
