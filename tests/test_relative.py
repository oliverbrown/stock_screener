"""Offline tests for performance vs the S&P 500: return windows, relative
points, the benchmark fetch, thesis notes and display."""

import pandas as pd
import pytest

import stock_screener as ss
from test_classify import make_stock


def closes(start_price, end_price, days=365, end="2026-09-28"):
    """Daily closes rising linearly from start_price to end_price."""
    idx = pd.date_range(end=end, periods=days, freq="D", tz="America/New_York")
    step = (end_price - start_price) / (days - 1)
    return pd.Series([start_price + i * step for i in range(days)], index=idx)


# ── Return windows ────────────────────────────────────────────────────────────
def test_compute_returns():
    s = pd.Series([100.0, 110.0, 120.0, 150.0], index=pd.to_datetime(
        ["2025-09-28", "2026-03-28", "2026-06-28", "2026-09-28"]).tz_localize("America/New_York"))
    assert ss.compute_returns(s) == {"3m": 25.0, "6m": 36.4, "1y": 50.0}


def test_window_start_on_a_weekend_uses_next_trading_day():
    s = closes(100, 200)
    s = s[s.index.dayofweek < 5]                     # drop weekends
    r = ss.compute_returns(s)
    assert r["1y"] is not None and r["3m"] is not None


def test_short_history_gives_none():
    r = ss.compute_returns(closes(100, 120, days=100))
    assert r["3m"] is not None
    assert r["6m"] is None and r["1y"] is None


def test_empty_history():
    assert ss.compute_returns(pd.Series(dtype=float)) == {"3m": None, "6m": None, "1y": None}


# ── Relative points ───────────────────────────────────────────────────────────
def test_relative_returns():
    stock = {"3m": 5.0, "6m": -2.0, "1y": 30.0}
    bench = {"3m": 3.0, "6m": 4.5, "1y": 18.2}
    assert ss.relative_returns(stock, bench) == {"3m": 2.0, "6m": -6.5, "1y": 11.8}


def test_relative_returns_partial():
    assert ss.relative_returns({"3m": 5.0, "6m": None, "1y": None},
                               {"3m": 3.0, "6m": 4.0, "1y": 18.0}) == {"3m": 2.0, "6m": None, "1y": None}


@pytest.mark.parametrize("stock, bench", [
    (None, {"1y": 1.0}), ({"1y": 1.0}, None), ({"3m": None, "6m": None, "1y": None}, {"1y": 1.0}),
])
def test_relative_returns_unavailable(stock, bench):
    assert ss.relative_returns(stock, bench) is None


# ── Benchmark fetched once per run and applied to every ticker ────────────────
def test_run_screen_adds_relative_performance(monkeypatch):
    calls = []

    def fake(t):
        calls.append(t)
        rets = {"SPY": {"3m": 3.0, "6m": 6.0, "1y": 18.0},
                "AAA": {"3m": 1.0, "6m": 10.0, "1y": 40.0}}.get(t, {"3m": 0.0, "6m": 0.0, "1y": 0.0})
        return {"ticker": t, "quote_type": "EQUITY", "is_fund": False, "returns": rets}, None

    monkeypatch.setattr(ss, "fetch_stock", fake)
    monkeypatch.setattr(ss, "classify_signal", lambda s, m: ("neutral", [], ""))
    matched, _ = ss.run_screen(["AAA", "BBB"], signal_filter="all", verbose=False)
    assert calls == ["SPY", "AAA", "BBB"]
    assert matched[0]["rel"] == {"3m": -2.0, "6m": 4.0, "1y": 22.0}
    assert matched[1]["rel"] == {"3m": -3.0, "6m": -6.0, "1y": -18.0}


def test_run_screen_without_benchmark(monkeypatch):
    def fake(t):
        if t == "SPY":
            return None, "rate-limited"
        return {"ticker": t, "quote_type": "EQUITY", "is_fund": False,
                "returns": {"3m": 1.0, "6m": 1.0, "1y": 1.0}}, None

    monkeypatch.setattr(ss, "fetch_stock", fake)
    monkeypatch.setattr(ss, "classify_signal", lambda s, m: ("neutral", [], ""))
    matched, _ = ss.run_screen(["AAA"], signal_filter="all", verbose=False)
    assert matched[0]["rel"] is None


# ── Thesis notes (buy side only; never change the signal) ────────────────────
def _stock(rel, **fields):
    s = make_stock(**{"rsi": 30.0, **fields})       # oversold by default
    s["rel"] = rel
    return s


def test_laggard_note():
    thesis = ss.classify_signal(_stock({"3m": -8.0, "6m": -15.0, "1y": -25.0}), None)[2]
    assert "Long-term laggard: -25 pts vs the S&P 500 over 1 year" in thesis


def test_leader_pullback_note():
    thesis = ss.classify_signal(_stock({"3m": -7.0, "6m": 2.0, "1y": 15.0}), None)[2]
    assert "Pullback in a leader: +15 pts vs the S&P 500 over 1 year, -7 over 3 months." in thesis


@pytest.mark.parametrize("rel", [
    {"3m": -3.0, "6m": -5.0, "1y": -19.0},           # lagging, but not by 20
    {"3m": -4.0, "6m": 0.0, "1y": 15.0},             # leader, but no real pullback
    {"3m": -7.0, "6m": 0.0, "1y": 9.0},              # pullback, but not a leader
    {"3m": -7.0, "6m": None, "1y": None},            # no 1y history
    None,
])
def test_no_note(rel):
    thesis = ss.classify_signal(_stock(rel), None)[2]
    assert "laggard" not in thesis and "leader" not in thesis


def test_sell_side_gets_no_note():
    thesis = ss.classify_signal(_stock({"3m": -8.0, "6m": -15.0, "1y": -25.0}, rsi=75.0), None)[2]
    assert "laggard" not in thesis


def test_signal_unchanged():
    base = ss.classify_signal(make_stock(rsi=30.0), None)
    assert ss.classify_signal(_stock({"3m": -8.0, "6m": -15.0, "1y": -25.0}), None)[:2] == base[:2]


# ── Display ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("quote_type", ["EQUITY", "ETF"])
def test_display(tmp_path, capsys, quote_type):
    from test_cmf import _report_row
    row = _report_row(quote_type=quote_type, rel={"3m": -2.0, "6m": 4.5, "1y": -12.3})
    ss.print_report([row], 1)
    assert "3m -2.0  6m +4.5  1y -12.3 pts" in capsys.readouterr().out
    out = tmp_path / "r.html"
    ss.save_html_report([row], 1, str(out))
    html = out.read_text(encoding="utf-8")
    assert '<div class="ml">vs S&amp;P 500</div>' in html
    assert ('<span style="color:#dc2626">-12.3 pts</span>'
            '<div class="msub">1y &middot; 3m -2 &middot; 6m +4</div>') in html


def test_display_unavailable(tmp_path, capsys):
    from test_cmf import _report_row
    row = _report_row()
    ss.print_report([row], 1)
    assert "vs S&P : N/A" in capsys.readouterr().out
    out = tmp_path / "r.html"
    ss.save_html_report([row], 1, str(out))
    assert '<div class="ml">vs S&amp;P 500</div><div class="mv">N/A</div>' in out.read_text(encoding="utf-8")
