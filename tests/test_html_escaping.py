"""The HTML report must treat text from Yahoo and ticker files as untrusted."""

import pytest

import stock_screener as ss
from test_cmf import _report_row

EVIL = '<script>alert("x")</script>'


def render(tmp_path, rows, **kw):
    out = tmp_path / "r.html"
    ss.save_html_report(rows, len(rows), str(out), **kw)
    return out.read_text(encoding="utf-8")


@pytest.mark.parametrize("quote_type", ["EQUITY", "ETF"])
def test_hostile_text_is_escaped_everywhere(tmp_path, quote_type):
    row = _report_row(quote_type=quote_type, pb=None, fcf_yield=None, target_price=100.0,
                      target_upside=1.0, num_analysts=3)
    row.update(ticker='X"><img src=x onerror=alert(1)>', name=EVIL, sector=EVIL, category=EVIL,
               thesis=EVIL, pb_note=EVIL, fcf_note=EVIL, rating=EVIL)
    html = render(tmp_path, [row], index_label=EVIL, criteria=EVIL)
    assert "<script>" not in html and "<img" not in html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in html
    assert 'onerror=alert(1)>' not in html.replace("&gt;", "")   # can't break out of the link
    assert "https://finance.yahoo.com/quote/X%22%3E%3Cimg%20src%3Dx%20onerror%3Dalert%281%29%3E" in html


def test_fund_quote_type_is_escaped(tmp_path):
    row = _report_row(quote_type="ETF")
    row["quote_type"] = EVIL
    row["is_fund"] = True
    assert "<script>" not in render(tmp_path, [row])


def test_normal_text_reads_correctly(tmp_path):
    row = _report_row()
    row.update(ticker="BRK-B", name="AT&T Inc.", sector="Consumer Cyclical",
               thesis="Money flow: still distribution; selling pressure hasn't eased.")
    html = render(tmp_path, [row], index_label="S&P 500")
    assert "AT&amp;T Inc. &middot; Consumer Cyclical" in html       # & shown correctly
    assert "S&amp;P 500 &middot; Generated" in html
    assert "hasn&#x27;t eased" in html                               # renders as hasn't
    assert '<a class="tkr" href="https://finance.yahoo.com/quote/BRK-B"' in html
    assert ">BRK-B</a>" in html


def test_special_ticker_links(tmp_path):
    row = _report_row()
    row["ticker"] = "CL=F"
    html = render(tmp_path, [row])
    assert "https://finance.yahoo.com/quote/CL%3DF" in html and ">CL=F</a>" in html


# ── Ticker files: a name must never spill onto a second line ──────────────────
def test_watch_list_name_with_line_break(tmp_path):
    path = tmp_path / "w.txt"
    ss.save_ticker_list([{"ticker": "AAA", "name": "Evil Corp\nSPY QQQ\r\nTSLA"},
                         {"ticker": "BBB", "name": None}], str(path))
    assert ss.load_tickers_file(str(path)) == ["AAA", "BBB"]
    assert "AAA # Evil Corp SPY QQQ TSLA\n" in path.read_text(encoding="utf-8")


def test_built_list_name_with_line_break(tmp_path):
    import build_ticker_lists as btl
    path = tmp_path / "l.txt"
    btl.write_list(str(path), "T", "test", [("AAA", "Evil Corp\nSPY"), ("BBB", "Fine Inc")])
    assert ss.load_tickers_file(str(path)) == ["AAA", "BBB"]
