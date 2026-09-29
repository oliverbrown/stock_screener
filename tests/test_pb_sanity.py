"""Offline tests for the P/B sanity check (values are real Yahoo data)."""

import pytest

import stock_screener as ss
from test_classify import make_stock


@pytest.mark.parametrize("info, pb, note", [
    # trustworthy
    ({"priceToBook": 45.978, "financialCurrency": "USD", "currency": "USD"}, 45.978, None),   # AAPL
    ({"priceToBook": 1.4436, "financialCurrency": "USD", "currency": "USD"}, 1.4436, None),   # BRK-A
    ({"priceToBook": 2.53},                                                2.53,   None),     # no currency info
    ({"priceToBook": 0.05, "financialCurrency": "USD", "currency": "USD"}, 0.05,   None),     # at the limit
    # share-class mix-up: book value per A share, price per B share
    ({"priceToBook": 0.00096, "financialCurrency": "USD", "currency": "USD"}, None,
     "Yahoo reports 0.00096x, not plausible"),                                               # BRK-B
    # foreign ADRs: book value in local currency per local share
    ({"priceToBook": 93.0, "financialCurrency": "TWD", "currency": "USD"}, None,
     "book value in TWD, price in USD"),                                                     # TSM
    ({"priceToBook": 15.31, "financialCurrency": "JPY", "currency": "USD"}, None,
     "book value in JPY, price in USD"),                                                     # TM
    # nothing reported
    ({}, None, None),
])
def test_sanitize_pb(info, pb, note):
    assert ss.sanitize_pb(info) == (pb, note)


def test_bad_pb_no_longer_makes_brk_b_look_cheap():
    # BRK-B: P/E 12.7 isn't cheap vs financials (~14 * 0.8 = 11.2); only the
    # bogus P/B < 1.5 made it "undervalued".
    pb, _ = ss.sanitize_pb({"priceToBook": 0.00096, "financialCurrency": "USD", "currency": "USD"})
    bogus = make_stock(sector="Financial Services", pe=12.7, pb=0.0)
    fixed = make_stock(sector="Financial Services", pe=12.7, pb=pb)
    assert "undervalued" in ss.classify_signal(bogus, None)[1]
    assert "undervalued" not in ss.classify_signal(fixed, None)[1]


def test_display(tmp_path, capsys):
    from test_cmf import _report_row
    row = _report_row(pb=None, pb_note="book value in TWD, price in USD")
    ss.print_report([row], 1)
    assert "P/B   : N/A (bad data)" in capsys.readouterr().out
    out = tmp_path / "r.html"
    ss.save_html_report([row], 1, str(out))
    assert ('N/A<div class="msub">ignored: book value in TWD, price in USD</div>'
            in out.read_text(encoding="utf-8"))


def test_display_without_note(tmp_path, capsys):
    from test_cmf import _report_row
    row = _report_row(pb=None)
    ss.print_report([row], 1)
    out = capsys.readouterr().out
    assert "P/B   : N/A" in out and "bad data" not in out
