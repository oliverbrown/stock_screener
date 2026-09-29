"""Offline tests for the ticker data cache and run-log cleanup."""

import math
import os
import time

import pytest

import stock_screener as ss


@pytest.fixture
def cache(tmp_path, monkeypatch):
    c = ss.TickerCache(str(tmp_path / "cache"), max_age_min=30)
    monkeypatch.setattr(ss, "CACHE", c)
    return c


@pytest.fixture
def fake_download(monkeypatch):
    calls = []

    def once(ticker):
        calls.append(ticker)
        return {"ticker": ticker, "price": 100.0, "rsi": math.nan, "pe": None}

    monkeypatch.setattr(ss, "_fetch_stock_once", once)
    return calls


def test_second_fetch_comes_from_cache(cache, fake_download):
    first, _ = ss.fetch_stock("AAPL")
    second, _ = ss.fetch_stock("AAPL")
    assert fake_download == ["AAPL"]                 # downloaded once
    assert "cached_min" not in first
    assert second["cached_min"] == 0
    assert second["price"] == 100.0 and second["pe"] is None
    assert math.isnan(second["rsi"])                 # NaN survives the JSON round trip
    assert (cache.hits, cache.misses) == (1, 1)


def test_expired_entry_is_downloaded_again(cache, fake_download, monkeypatch):
    ss.fetch_stock("AAPL")
    real_time = time.time
    monkeypatch.setattr(ss.time, "time", lambda: real_time() + 31 * 60)
    data, _ = ss.fetch_stock("AAPL")
    assert fake_download == ["AAPL", "AAPL"]
    assert "cached_min" not in data


def test_cache_age_is_reported_in_minutes(cache, fake_download, monkeypatch):
    ss.fetch_stock("AAPL")
    real_time = time.time
    monkeypatch.setattr(ss.time, "time", lambda: real_time() + 12 * 60 + 5)
    assert ss.fetch_stock("AAPL")[0]["cached_min"] == 12


def test_old_version_entry_is_ignored(cache, fake_download, monkeypatch):
    ss.fetch_stock("AAPL")
    monkeypatch.setattr(ss.TickerCache, "VERSION", ss.TickerCache.VERSION + 1)
    ss.fetch_stock("AAPL")
    assert fake_download == ["AAPL", "AAPL"]


def test_corrupt_entry_is_ignored(cache, fake_download):
    with open(cache._path("AAPL"), "w") as f:
        f.write("{not json")
    data, _ = ss.fetch_stock("AAPL")
    assert fake_download == ["AAPL"]
    assert data["ticker"] == "AAPL"


def test_skips_are_not_cached(cache, monkeypatch):
    calls = []

    def skip(t):
        calls.append(t)
        raise ss.SkipTicker("no price data")

    monkeypatch.setattr(ss, "_fetch_stock_once", skip)
    ss.fetch_stock("ZZZZ")
    ss.fetch_stock("ZZZZ")
    assert calls == ["ZZZZ", "ZZZZ"]


def test_odd_ticker_symbols_get_safe_filenames(cache, fake_download):
    for t in ("CL=F", "BTC-USD", "^GSPC", "BRK-B"):
        ss.fetch_stock(t)
        assert ss.fetch_stock(t)[0]["cached_min"] == 0
    assert all(os.sep not in f for f in os.listdir(cache.dir))


def test_no_cache_always_downloads(fake_download, monkeypatch):
    monkeypatch.setattr(ss, "CACHE", None)
    ss.fetch_stock("AAPL")
    ss.fetch_stock("AAPL")
    assert fake_download == ["AAPL", "AAPL"]


def test_prune_removes_day_old_entries(cache, fake_download):
    ss.fetch_stock("OLD")
    ss.fetch_stock("NEW")
    two_days_ago = time.time() - 2 * 86400
    os.utime(cache._path("OLD"), (two_days_ago, two_days_ago))
    assert cache.prune() == 1
    assert sorted(os.listdir(cache.dir)) == ["NEW.json"]


def test_run_screen_pauses_only_after_real_downloads(cache, fake_download, monkeypatch):
    sleeps = []
    monkeypatch.setattr(ss.time, "sleep", sleeps.append)
    monkeypatch.setattr(ss, "classify_signal", lambda s, m: ("neutral", [], ""))
    monkeypatch.setattr(ss, "_fetch_stock_once", lambda t: {
        "ticker": t, "quote_type": "EQUITY", "is_fund": False, "pe": None, "rsi": None})
    tickers = [f"T{i}" for i in range(5)]
    ss.run_screen(tickers, signal_filter="all", verbose=False, batch_size=2)
    assert len(sleeps) == 2                          # after downloads 2 and 4
    sleeps.clear()
    ss.run_screen(tickers, signal_filter="all", verbose=False, batch_size=2)
    assert sleeps == []                              # all from cache: no pauses


# ── Run-log cleanup ───────────────────────────────────────────────────────────
def _touch(path, days_old):
    path.write_text("x")
    t = time.time() - days_old * 86400
    os.utime(path, (t, t))


def test_prune_old_logs(tmp_path):
    _touch(tmp_path / "screener_20260101_080000.log", 40)
    _touch(tmp_path / "screener_20260901_080000.log", 5)
    _touch(tmp_path / "notes.log", 400)              # not a screener log: kept
    _touch(tmp_path / "screener_old.txt", 400)       # wrong extension: kept
    assert ss.prune_old_logs(str(tmp_path), keep_days=30) == 1
    assert sorted(os.listdir(tmp_path)) == [
        "notes.log", "screener_20260901_080000.log", "screener_old.txt"]


def test_keep_logs_zero_keeps_everything(tmp_path):
    _touch(tmp_path / "screener_20200101_080000.log", 2000)
    assert ss.prune_old_logs(str(tmp_path), keep_days=0) == 0
    assert os.listdir(tmp_path) == ["screener_20200101_080000.log"]


def test_prune_old_logs_missing_dir(tmp_path):
    assert ss.prune_old_logs(str(tmp_path / "nope"), keep_days=30) == 0
