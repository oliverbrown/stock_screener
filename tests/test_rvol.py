"""Offline tests for relative volume: the calculation, partial-day
handling, labels, thesis notes and display."""

import pandas as pd
import pytest

import stock_screener as ss
from test_classify import make_stock


def bars(recent_vol=1000, base_vol=1000, n_recent=5, n_base=50, extra=10,
         start_close=100.0, end_close=100.0):
    """`extra` older bars, then n_base bars, then n_recent bars. Closes stay
    at start_close until the final bar, which closes at end_close."""
    n = extra + n_base + n_recent
    vols = [base_vol] * (extra + n_base) + [recent_vol] * n_recent
    closes = [start_close] * (n - 1) + [end_close]
    return pd.DataFrame({"Close": closes, "Volume": vols})


# ── Calculation ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("recent, base, expected", [(1800, 1000, 1.8), (500, 1000, 0.5), (1000, 1000, 1.0)])
def test_rvol_ratio(recent, base, expected):
    assert ss.compute_rvol(bars(recent, base))[0] == expected


def test_price_change_over_the_window():
    assert ss.compute_rvol(bars(end_close=92.0))[1] == -8.0


def test_only_the_50_sessions_before_count_as_normal():
    h = bars(recent_vol=1000, base_vol=1000, extra=10)
    h.loc[:9, "Volume"] = 1_000_000                  # ancient spike, outside the baseline
    assert ss.compute_rvol(h)[0] == 1.0


def test_partial_day_is_dropped():
    h = pd.concat([bars(recent_vol=2000, base_vol=1000),
                   pd.DataFrame({"Close": [100.0], "Volume": [50]})], ignore_index=True)
    assert ss.compute_rvol(h, drop_last=True)[0] == 2.0
    assert ss.compute_rvol(h, drop_last=False)[0] < 2.0   # the partial bar drags it down


def test_zero_volume_days_are_ignored():
    h = bars(recent_vol=1000, base_vol=1000)
    h.loc[20, "Volume"] = 0
    assert ss.compute_rvol(h)[0] == 1.0


def test_not_enough_history():
    assert ss.compute_rvol(bars(n_base=40, extra=0)) == (None, None)


def test_no_volume_at_all():
    assert ss.compute_rvol(bars(recent_vol=0, base_vol=0)) == (None, None)


@pytest.mark.parametrize("rvol, label", [
    (2.5, "very heavy"), (2.0, "very heavy"), (1.5, "heavy"), (1.2, "normal"),
    (0.71, "normal"), (0.7, "light"), (None, "N/A"),
])
def test_label(rvol, label):
    assert ss.rvol_label(rvol) == label


# ── Thesis notes (one-sided signals; never change the signal) ─────────────────
@pytest.mark.parametrize("fields, rvol, chg, expected", [
    ({"rsi": 30.0}, 1.8, -9.0,
     "Volume: sell-off (-9% in 5 days) on heavy volume (1.8× normal), so sellers are committed, or capitulating."),
    ({"rsi": 30.0}, 0.6, -6.0,
     "Volume: sell-off (-6% in 5 days) on light volume (0.6× normal), so the move lacks conviction."),
    ({"rsi": 75.0}, 2.2, 12.0,
     "Volume: rally (+12% in 5 days) on heavy volume (2.2× normal), so buyers are committed."),
    ({"rsi": 75.0}, 0.5, 4.0,
     "Volume: rally (+4% in 5 days) on light volume (0.5× normal), so the move lacks conviction."),
])
def test_volume_note(fields, rvol, chg, expected):
    s = make_stock(rvol=rvol, chg_5d=chg, **fields)
    base = ss.classify_signal(make_stock(**fields), None)
    signal, tags, thesis = ss.classify_signal(s, None)
    assert expected in thesis
    assert (signal, tags) == base[:2]


@pytest.mark.parametrize("fields, rvol, chg", [
    ({"rsi": 30.0}, 1.2, -9.0),                      # normal volume
    ({"rsi": 30.0}, 2.0, -0.5),                      # barely moved
    ({}, 2.0, -9.0),                                 # neutral signal
    ({"pe": 20.0, "rsi": 75.0}, 2.0, -9.0),          # mixed signal
    ({"rsi": 30.0}, None, None),                     # no data
])
def test_no_volume_note(fields, rvol, chg):
    assert "Volume:" not in ss.classify_signal(make_stock(rvol=rvol, chg_5d=chg, **fields), None)[2]


# ── Display ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("quote_type", ["EQUITY", "ETF"])
def test_display(tmp_path, capsys, quote_type):
    from test_cmf import _report_row
    row = _report_row(quote_type=quote_type, rvol=1.84, chg_5d=-7.2)
    ss.print_report([row], 1)
    assert "1.84× normal (heavy), price -7.2% over 5 days" in capsys.readouterr().out
    out = tmp_path / "r.html"
    ss.save_html_report([row], 1, str(out))
    assert ('<span style="color:#b45309">1.84×</span><div class="msub">heavy &middot; price -7.2%</div>'
            in out.read_text(encoding="utf-8"))


def test_display_unavailable(tmp_path, capsys):
    from test_cmf import _report_row
    ss.print_report([_report_row()], 1)
    assert "Volume : N/A" in capsys.readouterr().out
