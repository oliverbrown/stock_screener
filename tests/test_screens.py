"""Offline tests for --sector / --exclude-sector / --no-earnings-within and
saved screens (screens.toml, my-screens.toml, --screen, --list-screens)."""

import argparse
import os
from datetime import date

import pytest

import stock_screener as ss

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def row(**f):
    base = {"ticker": "X", "sector": "Technology", "earnings_next": None}
    base.update(f)
    return base


# ── Shortcut filters ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("values, exclude, sector, ok", [
    (["Technology"],           False, "Technology", True),
    (["technology", "Energy"], False, "Energy",     True),       # any of several, any case
    (["Energy"],               False, "Technology", False),
    (["Energy"],               False, None,         False),      # ETF: no sector
    (["Financial Services"],   True,  "Financial Services", False),
    (["Financial Services"],   True,  "Technology", True),
    (["Financial Services"],   True,  None,         True),       # ETFs survive an exclude
])
def test_one_of(values, exclude, sector, ok):
    assert ss.OneOf("sector", values, exclude).matches(row(sector=sector)) is ok


def test_one_of_text():
    assert ss.OneOf("sector", ["Energy", "Utilities"]).text == "sector in (Energy, Utilities)"
    assert ss.OneOf("sector", ["Energy"], exclude=True).text == "sector not in (Energy)"


@pytest.mark.parametrize("report, ok", [
    ("2026-10-02", False),    # in 3 days
    ("2026-10-06", False),    # in exactly 7
    ("2026-10-07", True),     # in 8
    (None,         True),     # ETF / no date: kept (unlike --where "earnings_days>7")
    ("2026-09-28", True),     # stale cached date in the past
])
def test_no_earnings_within(monkeypatch, report, ok):
    monkeypatch.setattr(ss, "market_today", lambda: date(2026, 9, 29))
    nxt = {"date": report, "end": report, "time": None, "estimate": False} if report else None
    assert ss.NoEarningsWithin(7).matches(row(earnings_next=nxt)) is ok


# ── Loading screen files ──────────────────────────────────────────────────────
def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_later_file_overrides(tmp_path):
    a = write(tmp_path, "screens.toml", '[cheap]\nwhere = ["pe<15"]\n[other]\ntop = 5\n')
    b = write(tmp_path, "my-screens.toml", '[cheap]\nwhere = ["pe<10"]\n')
    screens = ss.load_screens([a, b, str(tmp_path / "missing.toml")])
    assert screens["cheap"]["where"] == ["pe<10"] and screens["cheap"]["_source"] == "my-screens.toml"
    assert screens["other"]["top"] == 5


@pytest.mark.parametrize("text, message", [
    ('[x]\nwher = ["pe<1"]\n',   "unknown setting 'wher'"),
    ('[x]\ntop = "ten"\n',       "top has the wrong type"),
    ('[x]\ntop = true\n',        "top has the wrong type"),
    ('x = 1\n',                  "must be a table"),
    ('[x\n',                     "screens.toml"),
])
def test_bad_screen_files(tmp_path, text, message):
    with pytest.raises(ValueError, match=message):
        ss.load_screens([write(tmp_path, "screens.toml", text)])


def test_bundled_screens_are_valid():
    """Every example in screens.toml uses real fields and valid settings."""
    screens = ss.load_screens([os.path.join(REPO, "screens.toml")])
    assert len(screens) >= 5
    for name, sc in screens.items():
        for cond in sc.get("where", []):
            ss.Condition(cond)
        if "sort_by" in sc:
            ss.parse_sort(sc["sort_by"])
        assert sc.get("signal", "all") in ss.SIGNAL_CHOICES, name
        assert sc.get("asset_type", "all") in ("all", "stock", "etf"), name
        assert sc.get("description"), f"{name} needs a description"


# ── Merging a screen with command-line options ────────────────────────────────
def args(**kw):
    base = dict(where=[], sector=[], exclude_sector=[], signal=None, asset_type=None,
                sort_by=None, top=None, no_earnings_within=None, max_pe=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_apply_screen_adds_lists_and_fills_unset():
    a = args(where=["rsi<30"], top=5)
    ss.apply_screen(a, {"where": ["pe<20"], "exclude_sector": ["Energy"], "top": 25,
                        "sort_by": "pe", "no_earnings_within": 7})
    assert a.where == ["pe<20", "rsi<30"]               # combined
    assert a.exclude_sector == ["Energy"]
    assert a.top == 5                                    # command line wins
    assert (a.sort_by, a.no_earnings_within) == ("pe", 7)


def test_list_screens_text():
    text = ss.list_screens_text({"cheap": {"description": "Low P/E", "where": ["pe<15"],
                                           "top": 10, "_source": "screens.toml"}})
    assert "cheap  [screens.toml]" in text and "Low P/E" in text and "pe<15; top=10" in text
    assert "No saved screens" in ss.list_screens_text({})


# ── End to end through main(), offline (--cache-only) ─────────────────────────
def test_main_runs_a_screen_offline(tmp_path, monkeypatch):
    from test_cmf import _report_row
    cache = ss.TickerCache(str(tmp_path / "cache"), 60)
    stocks = {
        "CHEAP": _report_row(pe=12.0, sector="Energy"),
        "PRICY": _report_row(pe=40.0, sector="Energy"),
        "BANK":  _report_row(pe=9.0,  sector="Financial Services"),
    }
    for t, s in stocks.items():
        s = {k: v for k, v in s.items() if k not in ("signal", "tags", "thesis")}
        cache.put(t, {**s, "ticker": t, "name": f"{t} Inc"})
    screens = write(tmp_path, "screens.toml",
                    '[cheap]\ndescription = "x"\nwhere = ["pe<20"]\nexclude_sector = ["Financial Services"]\n')
    monkeypatch.setattr(ss, "SCREEN_FILES", (screens,))
    monkeypatch.setattr(ss, "CACHE", None)
    monkeypatch.setattr(ss, "CACHE_ONLY", False)
    watch = tmp_path / "watch.txt"
    monkeypatch.setattr("sys.argv", ["stock_screener.py", "--tickers", "CHEAP", "PRICY", "BANK",
                                     "--screen", "cheap", "--cache-only", "--quiet",
                                     "--cache-dir", str(tmp_path / "cache"),
                                     "--log-dir", str(tmp_path / "logs"),
                                     "--save-tickers", str(watch)])
    ss.main()
    # main() writes to the real stdout and the run log (not pytest's capture)
    logs = list((tmp_path / "logs").iterdir())
    out = logs[0].read_text(encoding="utf-8")
    assert "Filters : screen cheap: pe<20, sector not in (Financial Services)" in out
    assert "Filters: 3 → 1 ticker(s)" in out
    assert ss.load_tickers_file(str(watch)) == ["CHEAP"]
