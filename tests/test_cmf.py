"""Offline tests for Chaikin Money Flow: the calculation, its labels, the
explanation text it adds to stock signals, and how it's displayed."""

import pandas as pd
import pytest

import stock_screener as ss
from test_classify import make_fund, make_stock


def bars(closes_at, n=20, volume=1000):
    """n daily bars with high 110 / low 90, closing at `closes_at`
    (a number, or a list of one close per day)."""
    closes = closes_at if isinstance(closes_at, list) else [closes_at] * n
    return pd.DataFrame({
        "High": [110.0] * len(closes), "Low": [90.0] * len(closes),
        "Close": [float(c) for c in closes], "Volume": [volume] * len(closes),
    })


# ── Calculation ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("close, expected", [(110, 1.0), (90, -1.0), (100, 0.0), (105, 0.5)])
def test_cmf_position_in_daily_range(close, expected):
    assert ss.compute_cmf(bars(close)) == expected


def test_cmf_is_volume_weighted():
    h = bars([110] * 10 + [90] * 10)
    h.loc[:9, "Volume"] = 3000                       # heavy volume on the up days
    assert ss.compute_cmf(h) == pytest.approx(0.5)   # (3*10 - 1*10) / (3*10 + 1*10)


def test_cmf_uses_only_the_last_20_days():
    h = bars([90] * 30 + [110] * 20, n=50)
    assert ss.compute_cmf(h) == 1.0


def test_cmf_flat_day_counts_as_neutral():
    h = bars(110)
    h.loc[0, ["High", "Low", "Close"]] = 100.0       # high == low
    assert ss.compute_cmf(h) == pytest.approx(19 / 20)


def test_cmf_needs_20_days():
    assert ss.compute_cmf(bars(110, n=19)) is None


def test_cmf_no_volume():
    assert ss.compute_cmf(bars(110, volume=0)) is None


@pytest.mark.parametrize("cmf, label", [
    (0.30, "strong accumulation"), (0.25, "strong accumulation"), (0.10, "accumulation"),
    (0.05, "accumulation"), (0.0, "neutral"), (-0.049, "neutral"), (-0.05, "distribution"),
    (-0.30, "strong distribution"), (None, "N/A"),
])
def test_cmf_label(cmf, label):
    assert ss.cmf_label(cmf) == label


# ── Explanation text (context only; never changes the signal) ────────────────
@pytest.mark.parametrize("stock_fields, cmf, expected", [
    ({"rsi": 30.0}, 0.12,  "Money flow: accumulation (CMF +0.12) supports the buy case."),
    ({"rsi": 30.0}, 0.30,  "Money flow: strong accumulation (CMF +0.30) supports the buy case."),
    ({"rsi": 30.0}, -0.10, "Money flow: still distribution (CMF -0.10); selling pressure hasn't eased."),
    ({"rsi": 75.0}, -0.10, "Money flow: distribution (CMF -0.10) supports the sell case."),
    ({"rsi": 75.0}, 0.20,  "Money flow: still accumulation (CMF +0.20); buyers remain active."),
])
def test_money_flow_line(stock_fields, cmf, expected):
    without = ss.classify_signal(make_stock(**stock_fields), None)
    signal, tags, thesis = ss.classify_signal(make_stock(cmf=cmf, **stock_fields), None)
    assert expected in thesis
    assert (signal, tags) == without[:2]


@pytest.mark.parametrize("stock_fields, cmf", [
    ({"rsi": 30.0}, 0.02),                           # inside the neutral band
    ({}, 0.40),                                      # neutral signal
    ({"pe": 20.0, "rsi": 75.0}, 0.40),               # mixed signal
    ({"rsi": 30.0}, None),                           # not enough history
])
def test_no_money_flow_line(stock_fields, cmf):
    assert "Money flow" not in ss.classify_signal(make_stock(cmf=cmf, **stock_fields), None)[2]


def test_funds_show_cmf_but_get_no_money_flow_line():
    assert "Money flow" not in ss.classify_fund(make_fund(rsi=30.0, cmf=0.3), None)[2]


# ── Display ───────────────────────────────────────────────────────────────────
def _report_row(**fields):
    from test_helpers import _row
    s = {**_row("TST", **fields), "name": "Test Co", "price": 100.0, "change1d": 1.0,
         "wk_high": 110.0, "wk_low": 70.0, "pct_short": None, "div_yield": None,
         "target_price": None, "target_upside": None, "target_low": None, "target_high": None,
         "num_analysts": None, "rating": None, "ext_session": None, "ext_price": None,
         "ext_change": None, "fund_yield": None, "beta": None}
    s["signal"], s["tags"], s["thesis"] = "neutral", [], ""
    return s


@pytest.mark.parametrize("quote_type", ["EQUITY", "ETF"])
def test_cmf_in_terminal_and_html(tmp_path, capsys, quote_type):
    s = _report_row(quote_type=quote_type, cmf=-0.31)
    ss.print_report([s], 1)
    assert "-0.31 (strong distribution)" in capsys.readouterr().out
    out = tmp_path / "r.html"
    ss.save_html_report([s], 1, str(out))
    html = out.read_text(encoding="utf-8")
    assert "Money flow (CMF 20d)" in html
    assert '<span style="color:#dc2626">-0.31</span>' in html


def test_missing_cmf_shows_na(tmp_path, capsys):
    s = _report_row()                                # no "cmf" key at all
    ss.print_report([s], 1)
    assert "MnyFlow: N/A" in capsys.readouterr().out
