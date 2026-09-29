"""Offline tests for free-cash-flow yield and its role in the value-trap
check (values are real Yahoo data)."""

import pytest

import stock_screener as ss
from test_classify import make_stock


@pytest.mark.parametrize("info, expected", [
    ({"freeCashflow": 107721875456, "marketCap": 4938670276608,
      "financialCurrency": "USD", "currency": "USD"}, (2.2, None)),                 # AAPL
    ({"freeCashflow": -7940250112, "marketCap": 49366437888,
      "financialCurrency": "USD", "currency": "USD"}, (-16.1, None)),               # F
    ({"freeCashflow": -3599962472448, "marketCap": 222981734400,
      "financialCurrency": "JPY", "currency": "USD"},
     (None, "cash flow in JPY, price in USD")),                                    # TM
    ({"freeCashflow": None, "marketCap": 894718902272}, (None, None)),             # JPM (bank)
    ({"freeCashflow": 1_000_000, "marketCap": None}, (None, None)),
    ({"freeCashflow": 1_000_000, "marketCap": 0}, (None, None)),
])
def test_fcf_yield(info, expected):
    assert ss.fcf_yield(info) == expected


# ── Value-trap check ──────────────────────────────────────────────────────────
def test_cash_burn_makes_cheap_stock_a_value_trap():
    # Technology avg P/E 28: P/E 20 is undervalued on its own
    assert ss.classify_signal(make_stock(pe=20.0, fcf_yield=3.0), None)[0] == "undervalued"
    signal, tags, thesis = ss.classify_signal(make_stock(pe=20.0, fcf_yield=-4.5), None)
    assert signal == "neutral"
    assert "undervalued" not in tags
    assert "possible value trap (negative free cash flow (FCF yield -4.5%))" in thesis


def test_cash_burn_combines_with_other_flags():
    thesis = ss.classify_signal(make_stock(pe=20.0, fcf_yield=-4.5, margin=-3.0), None)[2]
    assert "profit margin -3.0%, negative free cash flow (FCF yield -4.5%)" in thesis


def test_cash_burn_without_value_call_is_a_quality_watch():
    signal, _, thesis = ss.classify_signal(make_stock(rsi=30.0, fcf_yield=-4.5), None)
    assert signal == "oversold"                      # momentum signals are unaffected
    assert "Quality watch: negative free cash flow (FCF yield -4.5%)." in thesis


@pytest.mark.parametrize("fcf", [0.0, 2.2, None])
def test_zero_positive_or_missing_fcf_is_fine(fcf):
    signal, _, thesis = ss.classify_signal(make_stock(pe=20.0, fcf_yield=fcf), None)
    assert signal == "undervalued"
    assert "free cash flow" not in thesis


def test_financials_are_exempt():
    # Financial Services avg P/E 14: P/E 10 is undervalued
    s = make_stock(sector="Financial Services", pe=10.0, fcf_yield=-8.0)
    signal, _, thesis = ss.classify_signal(s, None)
    assert signal == "undervalued"
    assert "free cash flow" not in thesis


def test_missing_field_on_old_data():
    s = make_stock(pe=20.0)
    s.pop("fcf_yield", None)
    assert ss.classify_signal(s, None)[0] == "undervalued"


# ── Display ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("fields, terminal, html", [
    ({"fcf_yield": -16.1}, "FCF Yld: -16.1%", '<span style="color:#dc2626">-16.1%</span>'),
    ({"fcf_yield": 2.2},   "FCF Yld: +2.2%",  '<span style="color:inherit">+2.2%</span>'),
    ({"fcf_yield": None, "fcf_note": "cash flow in JPY, price in USD"}, "FCF Yld: N/A (bad data)",
     'N/A<div class="msub">ignored: cash flow in JPY, price in USD</div>'),
    ({}, "FCF Yld: N/A", '<div class="ml">FCF yield</div><div class="mv">N/A</div>'),
])
def test_display(tmp_path, capsys, fields, terminal, html):
    from test_cmf import _report_row
    row = _report_row(**fields)
    ss.print_report([row], 1)
    assert terminal in capsys.readouterr().out
    out = tmp_path / "r.html"
    ss.save_html_report([row], 1, str(out))
    assert html in out.read_text(encoding="utf-8")
