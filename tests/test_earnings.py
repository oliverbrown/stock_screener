"""Offline tests for earnings dates: parsing Yahoo's fields, day counts,
thesis notes and display. "Today" is pinned so results don't drift."""

from datetime import date, datetime

import pytest

import stock_screener as ss
from test_classify import make_stock

TODAY = date(2026, 9, 28)


def ts(y, m, d, hh=16, mm=0):
    """Epoch seconds for a US/Eastern wall-clock time."""
    return datetime(y, m, d, hh, mm, tzinfo=ss.MARKET_TZ).timestamp()


@pytest.fixture(autouse=True)
def pin_today(monkeypatch):
    monkeypatch.setattr(ss, "market_today", lambda: TODAY)


# ── Parsing Yahoo's fields ────────────────────────────────────────────────────
def test_next_after_close_and_last_report():
    info = {"earningsTimestamp": ts(2026, 7, 30), "earningsTimestampStart": ts(2026, 10, 29),
            "earningsTimestampEnd": ts(2026, 10, 29), "isEarningsDateEstimate": False}
    nxt, last = ss.parse_earnings(info, TODAY)
    assert nxt == {"date": "2026-10-29", "end": "2026-10-29", "time": "after close", "estimate": False}
    assert last == "2026-07-30"


def test_before_open():
    info = {"earningsTimestampStart": ts(2026, 10, 13, 8, 30), "isEarningsDateEstimate": False}
    assert ss.parse_earnings(info, TODAY)[0]["time"] == "before open"


def test_midday_time_is_not_labelled():
    info = {"earningsTimestampStart": ts(2026, 12, 10, 15, 0), "isEarningsDateEstimate": False}
    assert ss.parse_earnings(info, TODAY)[0]["time"] is None


def test_estimate_has_no_time():
    info = {"earningsTimestampStart": ts(2026, 11, 7, 15), "isEarningsDateEstimate": True}
    nxt, _ = ss.parse_earnings(info, TODAY)
    assert nxt["estimate"] is True and nxt["time"] is None


def test_date_window():
    info = {"earningsTimestampStart": ts(2026, 10, 27), "earningsTimestampEnd": ts(2026, 11, 3),
            "isEarningsDateEstimate": True}
    nxt, _ = ss.parse_earnings(info, TODAY)
    assert (nxt["date"], nxt["end"]) == ("2026-10-27", "2026-11-03")


def test_report_today_counts_as_upcoming():
    info = {"earningsTimestamp": ts(2026, 9, 28, 8, 0), "earningsTimestampStart": ts(2026, 9, 28, 8, 0)}
    nxt, last = ss.parse_earnings(info, TODAY)
    assert nxt["date"] == "2026-09-28" and last is None


def test_only_past_dates():
    nxt, last = ss.parse_earnings({"earningsTimestamp": ts(2026, 9, 24)}, TODAY)
    assert nxt is None and last == "2026-09-24"


@pytest.mark.parametrize("info", [{}, {"earningsTimestamp": None}, {"earningsTimestamp": "garbage"}])
def test_no_earnings_data(info):
    assert ss.parse_earnings(info, TODAY) == (None, None)


# ── Day counts and formatting ─────────────────────────────────────────────────
def _with(next_date=None, last=None, time=None, estimate=False, end=None):
    stock = make_stock(rsi=30.0)                     # oversold -> a non-neutral signal
    stock["earnings_next"] = ({"date": next_date, "end": end or next_date,
                               "time": time, "estimate": estimate} if next_date else None)
    stock["earnings_last"] = last
    return stock


def test_days_and_text():
    s = _with("2026-10-29", time="after close")
    assert ss.earnings_days(s) == 31
    assert ss.earnings_when(s) == "Oct 29"
    assert ss.earnings_when(s, year=True) == "Thu Oct 29, 2026"
    assert ss.earnings_details(s) == ["in 31 days", "after close"]


def test_window_text():
    s = _with("2026-10-27", end="2026-11-03", estimate=True)
    assert ss.earnings_when(s) == "Oct 27–Nov 3"
    assert ss.earnings_details(s) == ["in 29 days", "estimated"]


@pytest.mark.parametrize("d, text", [("2026-09-28", "today"), ("2026-09-29", "tomorrow"), ("2026-10-02", "in 4 days")])
def test_in_days(d, text):
    assert ss.earnings_details(_with(d))[0] == text


def test_cached_data_counts_from_today_not_download_day(monkeypatch):
    s = _with("2026-10-12")
    assert ss.earnings_days(s) == 14
    monkeypatch.setattr(ss, "market_today", lambda: date(2026, 10, 10))
    assert ss.earnings_days(s) == 2


# ── Thesis notes ──────────────────────────────────────────────────────────────
def test_upcoming_report_warns():
    thesis = ss.classify_signal(_with("2026-10-02"), None)[2]
    assert "Earnings in 4 days (Oct 2): the report can quickly reverse this signal." in thesis


def test_estimated_report_says_so():
    thesis = ss.classify_signal(_with("2026-10-05", estimate=True), None)[2]
    assert "Earnings in 7 days (Oct 5, estimated)" in thesis


def test_report_beyond_14_days_is_quiet():
    assert "Earnings" not in ss.classify_signal(_with("2026-10-13"), None)[2]


def test_neutral_signal_gets_no_note():
    s = _with("2026-09-29")
    s["rsi"] = 50.0
    assert "Earnings" not in ss.classify_signal(s, None)[2]


@pytest.mark.parametrize("last, text", [
    ("2026-09-28", "Reported earnings today"), ("2026-09-27", "Reported earnings yesterday"),
    ("2026-09-23", "Reported earnings 5 days ago"),
])
def test_recent_report_note(last, text):
    assert text in ss.classify_signal(_with(last=last), None)[2]


def test_older_report_is_quiet():
    assert "Reported" not in ss.classify_signal(_with(last="2026-09-22"), None)[2]


def test_signal_unchanged():
    base = ss.classify_signal(make_stock(rsi=30.0), None)
    s = ss.classify_signal(_with("2026-09-29", last="2026-09-27"), None)
    assert s[:2] == base[:2]


# ── Display ───────────────────────────────────────────────────────────────────
def _report(stock):
    from test_cmf import _report_row
    row = _report_row()
    row.update(earnings_next=stock["earnings_next"], earnings_last=stock["earnings_last"])
    return row


def test_display_soon(tmp_path, capsys):
    row = _report(_with("2026-10-02", time="before open"))
    ss.print_report([row], 1)
    assert "Earning: ⚠ Fri Oct 2, 2026 (in 4 days, before open)" in capsys.readouterr().out
    out = tmp_path / "r.html"
    ss.save_html_report([row], 1, str(out))
    html = out.read_text(encoding="utf-8")
    assert '<span class="earn">📅 Earnings in 4 days</span>' in html
    assert '<span class="warn">Oct 2</span><div class="msub">in 4 days &middot; before open</div>' in html


def test_display_later(tmp_path, capsys):
    row = _report(_with("2026-10-29", time="after close"))
    ss.print_report([row], 1)
    assert "Earning: Thu Oct 29, 2026 (in 31 days, after close)" in capsys.readouterr().out
    out = tmp_path / "r.html"
    ss.save_html_report([row], 1, str(out))
    html = out.read_text(encoding="utf-8")
    assert 'class="earn"' not in html
    assert '<span class="">Oct 29</span>' in html


def test_display_none(tmp_path, capsys):
    row = _report(_with())
    ss.print_report([row], 1)
    assert "Earning:" not in capsys.readouterr().out
    out = tmp_path / "r.html"
    ss.save_html_report([row], 1, str(out))
    assert '<div class="ml">Next earnings</div><div class="mv">N/A</div>' in out.read_text(encoding="utf-8")
