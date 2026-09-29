"""Offline tests for parallel screening (--workers), the shared rate-limit
cooldown, and files that parallel runs must not collide on."""

import os
import random
import threading
import time

import pytest

import stock_screener as ss
from test_helpers import UNIVERSE


@pytest.fixture
def slow_fake_fetch(monkeypatch):
    """UNIVERSE tickers with random delays, so parallel runs finish out of order."""
    seen_threads = set()

    def fetch(t):
        seen_threads.add(threading.get_ident())
        time.sleep(random.uniform(0, 0.02))
        return (dict(UNIVERSE[t]), None) if t in UNIVERSE else (None, "unknown")

    monkeypatch.setattr(ss, "fetch_stock", fetch)
    return seen_threads


TICKERS = list(UNIVERSE) + ["NOPE"]


@pytest.mark.parametrize("signal", ["all", "buy", "sell", "hold"])
def test_parallel_matches_sequential(slow_fake_fetch, signal):
    seq = ss.run_screen(TICKERS, signal_filter=signal, verbose=False, workers=1)
    par = ss.run_screen(TICKERS, signal_filter=signal, verbose=False, workers=4)
    assert [s["ticker"] for s in par[0]] == [s["ticker"] for s in seq[0]]   # input order kept
    assert par[1] == seq[1]


def test_parallel_uses_several_threads(slow_fake_fetch):
    ss.run_screen(TICKERS * 1, signal_filter="all", verbose=False, workers=4)
    assert len(slow_fake_fetch) > 1


def test_parallel_output_lines_are_whole(slow_fake_fetch, capsys):
    ss.run_screen(TICKERS, signal_filter="all", verbose=True, workers=4)
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("  [")]
    assert len(lines) == len(TICKERS)
    assert sorted(int(l.split("/")[0].strip(" [")) for l in lines) == list(range(1, len(TICKERS) + 1))
    assert any("skipped: unknown" in l and "NOPE" in l for l in lines)


def test_parallel_skips_listed_in_input_order(slow_fake_fetch, capsys):
    ss.run_screen(["NOPE", "CHEAP", "ZZZ"], signal_filter="all", verbose=True, workers=3)
    out = capsys.readouterr().out
    assert out.index("    NOPE ") < out.index("    ZZZ ")


# ── Shared rate-limit cooldown ────────────────────────────────────────────────
def test_rate_limit_pauses_every_thread(monkeypatch):
    """After one thread is rate-limited, other threads wait out the same
    cooldown before their next request instead of hammering Yahoo."""
    monkeypatch.setattr(ss, "_cooldown_until", 0.0)
    monkeypatch.setattr(ss, "RATE_LIMIT_BACKOFF", (0.2,))
    start_times, first = {}, threading.Event()

    def once(t):
        start_times.setdefault(t, []).append(time.time())
        if t == "A" and len(start_times[t]) == 1:
            first.set()
            raise ss.YFRateLimitError()
        return {"ticker": t}

    monkeypatch.setattr(ss, "_fetch_stock_once", once)
    t0 = time.time()
    ta = threading.Thread(target=ss.fetch_stock, args=("A",))
    ta.start()
    first.wait(1)
    time.sleep(0.01)                                 # A has set the cooldown
    ss.fetch_stock("B")
    ta.join()
    assert start_times["B"][0] - t0 >= 0.19          # B waited for A's cooldown
    assert len(start_times["A"]) == 2                 # A retried after it


# ── Files parallel runs share ─────────────────────────────────────────────────
def test_same_second_logs_never_collide(tmp_path, monkeypatch):
    class Frozen(ss.datetime):
        @classmethod
        def now(cls, tz=None):
            return ss.datetime(2026, 9, 29, 8, 0, 0)

    monkeypatch.setattr(ss, "datetime", Frozen)
    handles, paths = [], []
    lock = threading.Lock()

    def open_one():
        f, p = ss._open_run_log(str(tmp_path))
        with lock:
            handles.append(f)
            paths.append(p)

    threads = [threading.Thread(target=open_one) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for f in handles:
        f.close()
    assert len(set(paths)) == 8
    assert "screener_20260929_080000.log" in {os.path.basename(p) for p in paths}


def test_parallel_cache_writes_are_safe(tmp_path):
    cache = ss.TickerCache(str(tmp_path), max_age_min=30)
    threads = [threading.Thread(target=cache.put, args=("AAPL", {"ticker": "AAPL", "n": i}))
               for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert os.listdir(tmp_path) == ["AAPL.json"]     # no leftover temp files
    assert cache.get("AAPL")["ticker"] == "AAPL"
