"""Offline tests for --learn: learning a starter screen from example tickers."""

import json
import random

import pytest

import learn_screen as ls
import stock_screener as ss

try:
    import tomllib
except ModuleNotFoundError:                       # Python < 3.11
    import tomli as tomllib

FIELDS = {"market_cap": "$", "roe": "%", "pe": "x", "beta": "", "sector": "text"}
SECTORS = ["Technology", "Energy", "Utilities", "Healthcare", "Industrials"]


def universe(n=2000, seed=1):
    """Random 'other' stocks: market caps from 10M to 1T, ROE -20..60."""
    rnd = random.Random(seed)
    return [{"ticker": f"O{i}", "market_cap": 10 ** rnd.uniform(7, 12),
             "roe": rnd.uniform(-20, 60), "pe": rnd.uniform(5, 60),
             "beta": rnd.uniform(0.3, 2.0), "sector": rnd.choice(SECTORS)}
            for i in range(n)]


def examples(n=7, seed=2):
    """Big, very profitable stocks, P/E and beta unremarkable."""
    rnd = random.Random(seed)
    return [{"ticker": f"E{i}", "market_cap": rnd.uniform(200e9, 900e9),
             "roe": rnd.uniform(35, 60), "pe": rnd.uniform(5, 60),
             "beta": rnd.uniform(0.3, 2.0), "sector": rnd.choice(SECTORS)}
            for i in range(n)]


# ── Numbers ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("v, lo, hi", [
    (21.837, 21, 22), (0.137, 0.13, 0.14), (123.4e9, 120e9, 130e9),
    (-3.71, -3.8, -3.7), (15, 15, 15), (0, 0, 0), (1000, 1000, 1000),
])
def test_nice_rounding_is_outwards(v, lo, hi):
    assert ls.nice_floor(v) == pytest.approx(lo) and ls.nice_floor(v) <= v
    assert ls.nice_ceil(v) == pytest.approx(hi) and ls.nice_ceil(v) >= v


@pytest.mark.parametrize("v, unit, text", [
    (120e9, "$", "120B"), (2.5e6, "$", "2.5M"), (4.8e12, "$", "4.8T"), (30600, "shares", "30.6K"),
    (15.0, "%", "15"), (0.35, "", "0.35"), (-4.2, "pts", "-4.2"), (None, "", "–"),
])
def test_format_value(v, unit, text):
    assert ls.format_value(v, unit) == text


def test_formatted_thresholds_parse_as_where_conditions():
    for v, unit in ((123.4e9, "$"), (0.137, ""), (-3.71, "%"), (2.46e6, "shares")):
        c = ls.Cond("market_cap", ">=", ls.nice_floor(v), unit)
        assert ss.Condition(c.text).value == pytest.approx(c.value)


def test_rank_biserial():
    assert ls.rank_biserial([10, 11], [1, 2, 3]) == 1
    assert ls.rank_biserial([0], [1, 2, 3]) == -1
    assert ls.rank_biserial([2], [1, 2, 3]) == 0              # ties count half
    assert ls.rank_biserial([], [1]) is None


def test_percentile():
    assert ls.percentile([1, 2, 3, 4, 5], 0.5) == 3
    assert ls.percentile([0, 10], 0.1) == pytest.approx(1)
    assert ls.percentile([], 0.5) is None


# ── Learning ──────────────────────────────────────────────────────────────────
def test_finds_the_planted_pattern():
    res = ls.learn(examples(), universe(), FIELDS)
    used = {c.field for c in res.conditions}
    assert used <= {"market_cap", "roe"} and "market_cap" in used
    assert res.examples_kept == 7 and not res.misses
    assert all(all(c.matches(r) for c in res.conditions) for r in examples())
    stats = {s.field: s for s in res.stats}
    assert stats["market_cap"].r > 0.9 and stats["roe"].r > 0.5
    assert abs(stats["pe"].r) < 0.5 and abs(stats["beta"].r) < 0.5


def test_lookalikes_match_the_screen_and_exclude_examples():
    others = universe()
    res = ls.learn(examples(), others, FIELDS)
    by = {r["ticker"]: r for r in others}
    assert res.lookalikes and not set(res.lookalikes) & {r["ticker"] for r in examples()}
    assert all(all(c.matches(by[t]) for c in res.conditions) for t in res.lookalikes)
    assert len(res.lookalikes) == res.steps[-1].others_kept


def test_stops_before_too_few_lookalikes():
    res = ls.learn(examples(), universe(), FIELDS, min_others=50)
    assert res.steps[-1].others_kept >= 50
    res = ls.learn(examples(), universe(), FIELDS, max_conditions=1)
    assert len(res.steps) == 1


def test_an_outlier_rules_a_field_out_unless_coverage_allows():
    ex = examples()
    ex[0]["market_cap"] = 50e6                       # one tiny company among giants
    strict = ls.learn(ex, universe(), FIELDS)
    assert strict.examples_kept == 7
    assert all(c.matches(ex[0]) for c in strict.conditions)
    loose = ls.learn(ex, universe(), FIELDS, min_coverage=0.8)
    assert loose.examples_kept == 6
    assert "E0" in loose.misses and any("market_cap" in f for f in loose.misses["E0"])


def test_missing_values_rule_a_field_out():
    ex = examples()
    ex[3]["market_cap"] = None                       # like --where, missing never matches
    res = ls.learn(ex, universe(), FIELDS)
    assert "market_cap" not in {c.field for c in res.conditions}
    assert res.examples_kept == 7


def test_sector_shortlist():
    ex = examples()
    for i, r in enumerate(ex):
        r["sector"] = "Energy" if i % 2 else "Utilities"
        r["market_cap"] = 10 ** random.Random(i).uniform(7, 11)   # nothing else stands out
        r["roe"] = random.Random(i + 9).uniform(-20, 40)
    res = ls.learn(ex, universe(), FIELDS)
    (sec,) = [c for c in res.conditions if c.op == "in"]
    assert sec.value == ("Energy", "Utilities") and res.sectors == {"Utilities": 4, "Energy": 3}


def test_nothing_to_learn():
    """Examples spread across the whole range: any condition keeping them all keeps everyone."""
    ex = [{"ticker": f"E{i}", "pe": pe, "beta": beta}
          for i, (pe, beta) in enumerate([(5, 1), (60, 1), (20, 0.3), (30, 2.0), (40, 1), (50, 1)])]
    res = ls.learn(ex, universe(500), {"pe": "x", "beta": ""})
    assert not res.steps and res.examples_kept == 6
    assert "No condition separates" in ls.report_text(res)


def test_tighten_suggestions_point_the_right_way():
    res = ls.learn(examples(), universe(), FIELDS, max_conditions=1)
    r = {s.field: s.r for s in res.stats}
    for c, kept, others in res.tighten:
        assert (r[c.field] if c.op == ">=" else -r[c.field]) >= ls.TIGHTEN_MIN_R
        assert kept == res.examples_kept and others < res.steps[-1].others_kept


# ── Output ────────────────────────────────────────────────────────────────────
def test_report_and_verbose_table():
    res = ls.learn(examples(), universe(), FIELDS)
    text = ls.report_text(res)
    assert "Learned screen" in text and "Keeps 7 of your 7" in text and "Look-alikes" in text
    assert "Field correlations" not in text
    verbose = ls.report_text(res, verbose=True)
    assert "Field correlations" in verbose and "rank-biserial" in verbose
    table = verbose[verbose.index("Field correlations"):].splitlines()
    rows = [l.split()[0] for l in table if l.startswith("  ") and l.split()[0] in FIELDS]
    assert rows[0] == "market_cap"                  # most correlated first


def test_screen_toml_loads_as_a_saved_screen(tmp_path):
    res = ls.learn(examples(), universe(), FIELDS, max_conditions=1)
    assert res.tighten
    text = ls.screen_toml("my-picks", res, 'Learned from "picks"', "stock", "learned/my-picks.json")
    path = tmp_path / "my-screens.toml"
    path.write_text(text, encoding="utf-8")
    sc = ss.load_screens([str(path)])["my-picks"]
    assert sc["asset_type"] == "stock" and sc["description"] == 'Learned from "picks"'
    assert [ss.Condition(w).field for w in sc["where"]] == [c.field for c in res.conditions]
    # The commented "to tighten" lines are valid once uncommented
    uncommented = text.replace('    # "', '    "')
    more = tomllib.loads(uncommented)["my-picks"]["where"]
    assert len(more) == len(res.conditions) + len(res.tighten)
    for w in more:
        ss.Condition(w)


def test_screen_toml_sector_goes_in_sector_list():
    ex = examples()
    for r in ex:
        r["sector"] = "Energy"
    res = ls.learn(ex, universe(), FIELDS)
    sc = tomllib.loads(ls.screen_toml("x", res, "d"))["x"]
    assert sc["sector"] == ["Energy"]
    assert all("sector" not in w for w in sc["where"])


def test_profile_is_json():
    res = ls.learn(examples(), universe(), FIELDS)
    data = json.loads(json.dumps(ls.profile(res, {"name": "x"})))
    assert data["name"] == "x" and data["examples"][0] == "E0"
    assert data["screen"][0]["examples_kept"] == 7
    assert data["fields"][0]["field"] == "market_cap" and data["fields"][0]["best_condition"]
    assert data["lookalikes"] == res.lookalikes


def test_save_learned_screen_appends_safely(tmp_path):
    path = tmp_path / "my-screens.toml"
    path.write_text('[old]\ndescription = "x"\nwhere = ["pe<20"]', encoding="utf-8")   # no final newline
    ss.save_learned_screen("new", '[new]\ndescription = "y"\nwhere = ["roe>=15"]\n', str(path))
    assert set(ss.load_screens([str(path)])) == {"old", "new"}
    with pytest.raises(tomllib.TOMLDecodeError):
        ss.save_learned_screen("bad", '[bad]\nwhere = [\n', str(path))
    assert set(ss.load_screens([str(path)])) == {"old", "new"}           # untouched


def test_learn_fields():
    f = ss.learn_fields()
    assert "rsi" not in f and "chg_5d" not in f and "earnings_days" not in f
    assert f["market_cap"] == "$" and f["sector"] == "text" and "ticker" not in f
    assert ss.learn_fields(["rsi", "sector"]) == {"rsi": "", "sector": "text"}
    assert ss.parse_learn_fields("roe, PE") == ["roe", "pe"]
    for bad in ("name", "tag", "nope"):
        with pytest.raises(Exception):
            ss.parse_learn_fields(bad)


# ── End to end through main(), offline (--cache-only) ─────────────────────────
def test_main_learns_offline(tmp_path, monkeypatch):
    from test_cmf import _report_row
    cache = ss.TickerCache(str(tmp_path / "cache"), 60)
    rows = {r["ticker"]: r for r in examples() + universe(300)}
    for t, r in rows.items():
        s = {k: v for k, v in _report_row(pe=r["pe"], sector=r["sector"]).items()
             if k not in ("signal", "tags", "thesis")}
        cache.put(t, {**s, "ticker": t, "name": f"{t} Inc", "market_cap": r["market_cap"],
                      "roe": r["roe"], "beta": r["beta"]})
    (tmp_path / "universe.txt").write_text("\n".join(rows), encoding="utf-8")
    screens = tmp_path / "my-screens.toml"
    monkeypatch.setattr(ss, "SCREEN_FILES", (str(screens),))
    monkeypatch.setattr(ss, "CACHE", None)
    monkeypatch.setattr(ss, "CACHE_ONLY", False)
    monkeypatch.chdir(tmp_path)                      # learned/<name>.json lands here
    monkeypatch.setattr("sys.argv", ["stock_screener.py", "--learn", "--cache-only", "--quiet",
                                     "--tickers", *[f"E{i}" for i in range(7)],
                                     "--universe", "universe.txt", "-v",
                                     "--save-screen", "giants", "--save-tickers", "alike.txt",
                                     "--cache-dir", "cache", "--log-dir", "logs"])
    ss.main()
    out = next((tmp_path / "logs").iterdir()).read_text(encoding="utf-8")
    assert "LEARNED SCREEN" in out and "Keeps 7 of your 7" in out and "Field correlations" in out
    sc = ss.load_screens([str(screens)])["giants"]
    assert sc["asset_type"] == "stock" and any(w.startswith("market_cap>=") for w in sc["where"])
    profile = json.loads((tmp_path / "learned" / "giants.json").read_text(encoding="utf-8"))
    assert profile["name"] == "giants" and "fund_assets" not in profile["fields_used"]
    assert ss.load_tickers_file("alike.txt") == profile["lookalikes"]


@pytest.mark.parametrize("extra, msg", [
    (["--where", "pe<20"], "can't be combined with --where"),
    ([], "needs your example tickers"),
    (["--tickers", "A", "--min-coverage", "0"], "--min-coverage"),
    (["--tickers", "A", "--save-screen", "bad name"], "--save-screen"),
])
def test_learn_option_errors(monkeypatch, capsys, extra, msg):
    monkeypatch.setattr("sys.argv", ["stock_screener.py", "--learn", *extra])
    with pytest.raises(SystemExit):
        ss.main()
    assert msg in capsys.readouterr().err


def test_learn_options_need_learn(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["stock_screener.py", "--tickers", "A", "--save-screen", "x"])
    with pytest.raises(SystemExit):
        ss.main()
    assert "only work with --learn" in capsys.readouterr().err
