"""
learn_screen.py — learn a starter screen from a handful of example tickers.

Given a few tickers you like (the "examples", typically 5-10) and a
universe of other tickers, find a short list of --where conditions that
the examples pass and most of the universe doesn't. The result is a normal
saved screen to refine by hand, plus everything learned along the way
(per-field statistics, correlations, other conditions that would also fit)
so you can see which knobs to turn.

This module is pure: rows are plain dicts of field -> value (already
extracted with stock_screener.FIELDS), so it needs no downloads and is easy
to test. stock_screener.py --learn does the data loading and printing.

How a screen is learned
  1. Every numeric field gets candidate conditions "field>=X" and
     "field<=X", with X taken from the examples' own values, rounded to two
     significant figures (outwards, so the rounding never drops an example).
     A sector shortlist is a candidate when the examples share 3 or fewer.
  2. Greedily add the condition that narrows the universe the most for
     the examples it keeps (the highest lift), as long as the screen still
     keeps at least --min-coverage of the examples.
  3. Stop at --max-conditions, when no condition narrows the universe by
     much any more, or before fewer than --min-lookalikes others would be left.

By default every example must pass (--min-coverage 100): with 5-10
tickers one odd value then rules a field out rather than costing a ticker.
A lower coverage lets the screen drop an outlier or two for a tighter fit.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

# Fields describing today's snapshot rather than what kind of stock it is:
# a screen learned from them stops matching your examples within days.
# Left out unless asked for with --learn-fields.
SNAPSHOT_FIELDS = ("price", "change1d", "chg_5d", "rsi", "rvol", "cmf", "earnings_days")

# Fields that can't sensibly become a threshold
NOT_LEARNABLE = ("ticker", "name", "signal", "tag", "quote_type", "category")

MIN_LIFT = 1.2          # stop when the best next condition removes < ~17% of the rest
MIN_OTHERS = 10         # a starter screen should leave some look-alikes to look at
TIGHTEN_MIN_R = 0.3     # suggest tightening only on fields at least this correlated
MAX_SECTORS = 3         # a sector shortlist only if the examples share this few
MONEY_UNITS = ("$", "shares")   # values shown with K / M / B / T


# ── Numbers ───────────────────────────────────────────────────────────────────
def _nice(v: float, up: bool, digits: int = 2) -> float:
    """Round to `digits` significant figures, towards +inf (up) or -inf."""
    if v == 0 or not math.isfinite(v):
        return v
    mag = 10 ** (math.floor(math.log10(abs(v))) - digits + 1)
    q = v / mag
    q = math.ceil(q - 1e-9) if up else math.floor(q + 1e-9)
    return round(q * mag, 12)


def nice_floor(v: float) -> float:
    return _nice(v, up=False)


def nice_ceil(v: float) -> float:
    return _nice(v, up=True)


def format_value(v: float | None, unit: str = "") -> str:
    """A threshold as --where accepts it: 120B, 2.5M, 15, 0.35, -4.2."""
    if v is None:
        return "–"
    if unit in MONEY_UNITS:
        for suffix, size in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
            if abs(v) >= size:
                return f"{v / size:.3g}{suffix}"
    if v != 0 and abs(v) < 0.01:
        return f"{v:.2g}"
    return f"{v:.4g}" if abs(v) >= 1000 else f"{round(v, 2):g}"


def percentile(sorted_vals: list[float], q: float) -> float | None:
    """q in 0..1, linear interpolation, on an already sorted list."""
    if not sorted_vals:
        return None
    pos = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def rank_biserial(examples: list[float], others: list[float]) -> float | None:
    """Rank-biserial correlation between "is one of the examples" and a
    field's value: +1 = every example is above every other ticker, -1 =
    every example below, 0 = no difference. Equal to 2·AUC − 1 (from the
    Mann-Whitney U), so unlike Pearson's r it isn't squashed towards zero
    when there are 8 examples and 5,000 others."""
    n1, n2 = len(examples), len(others)
    if not n1 or not n2:
        return None
    srt = sorted(others)
    wins = 0.0
    for x in examples:
        below = bisect_left(srt, x)
        ties = bisect_right(srt, x) - below
        wins += below + 0.5 * ties
    return round(2 * wins / (n1 * n2) - 1, 3)


def _num(v):
    """A usable number, or None for missing / NaN / text."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


# ── Conditions ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Cond:
    field: str
    op: str                          # ">=", "<=" or "in" (sector shortlist)
    value: float | tuple[str, ...]
    unit: str = ""

    @property
    def text(self) -> str:
        if self.op == "in":
            return f"{self.field} in ({', '.join(self.value)})"
        return f"{self.field}{self.op}{format_value(self.value, self.unit)}"

    def matches(self, row: dict) -> bool:
        got = row.get(self.field)
        if self.op == "in":
            return got is not None and str(got).lower() in {v.lower() for v in self.value}
        got = _num(got)
        if got is None:                           # like --where: missing never matches
            return False
        return got >= self.value if self.op == ">=" else got <= self.value


@dataclass
class Step:
    """One condition of the learned screen and its effect when added."""
    cond: Cond
    examples_kept: int               # examples still matching after this step
    others_kept: int                 # universe tickers still matching after this step
    lift: float


@dataclass
class FieldStats:
    field: str
    unit: str
    examples_have: int               # examples with a value
    ex_min: float | None
    ex_median: float | None
    ex_max: float | None
    others_have: int
    o_p10: float | None
    o_median: float | None
    o_p90: float | None
    r: float | None                  # rank-biserial correlation
    best: Cond | None = None         # best single condition on this field
    best_examples: int = 0
    best_others_pct: float | None = None


@dataclass
class Learned:
    examples: list[str]
    others_total: int
    steps: list[Step] = field(default_factory=list)
    tighten: list[tuple[Cond, int, int]] = field(default_factory=list)   # (cond, ex kept, others kept)
    stats: list[FieldStats] = field(default_factory=list)
    sectors: dict[str, int] = field(default_factory=dict)
    misses: dict[str, list[str]] = field(default_factory=dict)          # ticker -> failed conditions
    lookalikes: list[str] = field(default_factory=list)                # most similar first

    @property
    def conditions(self) -> list[Cond]:
        return [s.cond for s in self.steps]

    @property
    def examples_kept(self) -> int:
        return self.steps[-1].examples_kept if self.steps else len(self.examples)


# ── Learning ──────────────────────────────────────────────────────────────────
def _candidates(fields: dict[str, str], ex_rows: list[dict], need: int) -> list[Cond]:
    """Conditions keeping at least `need` of ex_rows, thresholds from their values."""
    out = []
    for name, unit in fields.items():
        if unit == "text":
            continue
        vals = sorted(v for v in (_num(r.get(name)) for r in ex_rows) if v is not None)
        if len(vals) < need:
            continue
        # The k-th lowest value as a floor keeps len-k examples, so only the
        # lowest (len - need + 1) values are worth trying, and likewise up top
        spare = len(vals) - need
        for v in vals[:spare + 1]:
            out.append(Cond(name, ">=", nice_floor(v), unit))
        for v in vals[len(vals) - spare - 1:]:
            out.append(Cond(name, "<=", nice_ceil(v), unit))
    if "sector" in fields:
        secs = {}
        for r in ex_rows:
            if r.get("sector"):
                secs[r["sector"]] = secs.get(r["sector"], 0) + 1
        if secs and len(secs) <= MAX_SECTORS and sum(secs.values()) >= need:
            out.append(Cond("sector", "in", tuple(sorted(secs)), "text"))
    return list(dict.fromkeys(out))                # de-duplicate, keep order


def _counter(rows: list[dict]):
    """count(cond) -> how many rows match, using per-field sorted values so
    hundreds of candidate thresholds cost a binary search each."""
    cache = {}

    def count(cond: Cond) -> int:
        if cond.op == "in":
            return sum(1 for r in rows if cond.matches(r))
        if cond.field not in cache:
            cache[cond.field] = sorted(v for v in (_num(r.get(cond.field)) for r in rows)
                                       if v is not None)
        vals = cache[cond.field]
        if cond.op == ">=":
            return len(vals) - bisect_left(vals, cond.value)
        return bisect_right(vals, cond.value)
    return count


def _stats(name: str, unit: str, ex_rows: list[dict], other_rows: list[dict]) -> FieldStats:
    ex = sorted(v for v in (_num(r.get(name)) for r in ex_rows) if v is not None)
    ot = sorted(v for v in (_num(r.get(name)) for r in other_rows) if v is not None)
    return FieldStats(
        field=name, unit=unit,
        examples_have=len(ex), ex_min=ex[0] if ex else None,
        ex_median=percentile(ex, 0.5), ex_max=ex[-1] if ex else None,
        others_have=len(ot), o_p10=percentile(ot, 0.1),
        o_median=percentile(ot, 0.5), o_p90=percentile(ot, 0.9),
        r=rank_biserial(ex, ot),
    )


def _similarity_order(rows: list[dict], ex_rows: list[dict], other_rows: list[dict],
                      stats: list[FieldStats]) -> list[dict]:
    """Sort rows by how close they sit to the examples' medians, measured in
    universe percentiles, over the fields that set the examples apart."""
    useful = [s for s in stats if s.r is not None and abs(s.r) >= 0.3 and s.ex_median is not None]
    if not useful:
        return sorted(rows, key=lambda r: r["ticker"])
    pool = ex_rows + other_rows
    sorted_by = {s.field: sorted(v for v in (_num(r.get(s.field)) for r in pool) if v is not None)
                 for s in useful}

    def pct(name, v):
        srt = sorted_by[name]
        return bisect_left(srt, v) / max(1, len(srt) - 1)

    target = {s.field: pct(s.field, s.ex_median) for s in useful}

    def distance(r):
        d, w = 0.0, 0.0
        for s in useful:
            v = _num(r.get(s.field))
            weight = abs(s.r)
            d += weight * (abs(pct(s.field, v) - target[s.field]) if v is not None else 1.0)
            w += weight
        return d / w

    return sorted(rows, key=lambda r: (distance(r), r["ticker"]))


def learn(ex_rows: list[dict], other_rows: list[dict], fields: dict[str, str],
          max_conditions: int = 4, min_coverage: float = 1.0,
          min_others: int = MIN_OTHERS, min_lift: float = MIN_LIFT) -> Learned:
    """Learn a screen. ex_rows / other_rows are dicts with "ticker" plus
    one key per field; `fields` maps each field to learn from to its unit,
    or "text" for sector. Examples must not also appear in other_rows."""
    n = len(ex_rows)
    result = Learned(examples=[r["ticker"] for r in ex_rows], others_total=len(other_rows))
    if not n:
        return result
    need = max(1, math.ceil(min_coverage * n - 1e-9))

    result.stats = [_stats(f, u, ex_rows, other_rows) for f, u in fields.items() if u != "text"]
    for r in ex_rows:
        if r.get("sector"):
            result.sectors[r["sector"]] = result.sectors.get(r["sector"], 0) + 1

    # Best single condition per field, against the whole universe
    by_field = {s.field: s for s in result.stats}
    count_ex, count_ot = _counter(ex_rows), _counter(other_rows)
    best_lift = {}
    for c in _candidates(fields, ex_rows, need):
        st = by_field.get(c.field)
        if st is None:
            continue
        kept, others = count_ex(c), count_ot(c)
        lift = (kept / n) / ((others + 1) / (len(other_rows) + 1))
        if lift > best_lift.get(c.field, 0):
            best_lift[c.field] = lift
            st.best, st.best_examples = c, kept
            st.best_others_pct = round(100 * others / max(1, len(other_rows)), 1)

    # Greedy selection
    cur_ex, cur_ot = list(ex_rows), list(other_rows)
    used = set()                                   # (field, op) already in the screen
    while len(result.steps) < max_conditions:
        best = None
        count_ex, count_ot = _counter(cur_ex), _counter(cur_ot)
        for c in _candidates(fields, cur_ex, need):
            if (c.field, c.op) in used:
                continue
            kept = count_ex(c)
            if kept < need:
                continue
            others = count_ot(c)
            if others < min(min_others, len(cur_ot)):
                continue                           # would leave too few look-alikes
            lift = (kept / len(cur_ex)) / ((others + 1) / (len(cur_ot) + 1))
            key = (lift, kept, -others)
            if best is None or key > best[0]:
                best = (key, c)
        if best is None or best[0][0] < min_lift:
            break
        c = best[1]
        cur_ex = [r for r in cur_ex if c.matches(r)]
        cur_ot = [r for r in cur_ot if c.matches(r)]
        used.add((c.field, c.op))
        result.steps.append(Step(c, len(cur_ex), len(cur_ot), round(best[0][0], 2)))

    # Further conditions that keep every example still matching: ways to tighten
    # Only in the direction that sets the examples apart (a floor where they
    # sit high, a cap where they sit low), so no "num_analysts<=54" noise
    r_of = {st.field: st.r for st in result.stats}
    count_ot = _counter(cur_ot)
    for c in _candidates(fields, cur_ex, len(cur_ex)) if cur_ex else []:
        r = r_of.get(c.field)
        if (c.field, c.op) in used or c.op == "in" or r is None \
           or (r if c.op == ">=" else -r) < TIGHTEN_MIN_R:
            continue
        others = count_ot(c)
        if others < len(cur_ot) * 0.9:             # removes at least 10% of the rest
            result.tighten.append((c, len(cur_ex), others))
    result.tighten.sort(key=lambda t: (t[2], t[0].field))
    seen, unique = set(), []
    for t in result.tighten:                      # the strongest per field and direction
        if (t[0].field, t[0].op) not in seen:
            seen.add((t[0].field, t[0].op))
            unique.append(t)
    result.tighten = unique[:8]

    for r in ex_rows:
        failed = [c.text for c in result.conditions if not c.matches(r)]
        if failed:
            result.misses[r["ticker"]] = failed
    result.lookalikes = [r["ticker"] for r in
                         _similarity_order(cur_ot, ex_rows, other_rows, result.stats)]
    return result


# ── Output ────────────────────────────────────────────────────────────────────
def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "–"


def report_text(res: Learned, verbose: bool = False, show: int = 25) -> str:
    n, total = len(res.examples), res.others_total
    lines = []
    if not res.steps:
        lines.append("  No condition separates these tickers from the universe well enough.")
        lines.append("  Try fewer or more alike tickers, or a lower --min-coverage.")
    else:
        lines.append(f"  {'Learned screen':<36}{'yours':>7}{'others left':>14}")
        lines.append(f"  {'(start)':<36}{f'{n}/{n}':>7}{total:>14,}")
        for s in res.steps:
            lines.append(f"  {s.cond.text:<36}{f'{s.examples_kept}/{n}':>7}"
                         f"{s.others_kept:>14,}  ({_pct(s.others_kept, total)})")
        lines.append("")
        lines.append(f"  Keeps {res.examples_kept} of your {n} tickers; "
                     f"{len(res.lookalikes):,} other ticker(s) match.")
    if res.misses:
        lines.append("")
        lines.append("  Not matched by the screen:")
        for t, failed in res.misses.items():
            lines.append(f"    {t:<8} fails {', '.join(failed)}")
    if res.tighten:
        lines.append("")
        lines.append("  To tighten (each keeps all your matched tickers):")
        for c, kept, others in res.tighten:
            lines.append(f"    {c.text:<30} others left {others:>6,}")
    if res.lookalikes:
        lines.append("")
        more = f" (first {show}, most similar first)" if len(res.lookalikes) > show else " (most similar first)"
        lines.append(f"  Look-alikes{more}:")
        shown = res.lookalikes[:show]
        for i in range(0, len(shown), 10):
            lines.append("    " + "  ".join(f"{t:<6}" for t in shown[i:i + 10]).rstrip())
    if verbose:
        lines.append("")
        lines += field_table(res)
    return "\n".join(lines)


def field_table(res: Learned) -> list[str]:
    """Per-field statistics, the fields that set the examples apart first."""
    n = len(res.examples)
    lines = ["  Field correlations (r: rank-biserial, +1 = yours all higher than the rest,",
             "  -1 = all lower, 0 = no different):", "",
             f"  {'field':<14}{'r':>6}  {'yours: min / median / max':<28}"
             f"{'others: 10% / median / 90%':<30}{'yours have':>10}"]
    order = sorted((s for s in res.stats if s.examples_have),
                   key=lambda s: (-(abs(s.r) if s.r is not None else -1), s.field))
    for s in order:
        f = lambda v: format_value(v, s.unit)
        r = f"{s.r:+.2f}" if s.r is not None else "–"
        yours = f"{f(s.ex_min)} / {f(s.ex_median)} / {f(s.ex_max)}" if s.examples_have else "–"
        others = f"{f(s.o_p10)} / {f(s.o_median)} / {f(s.o_p90)}" if s.others_have else "–"
        lines.append(f"  {s.field:<14}{r:>6}  {yours:<28}{others:<30}{f'{s.examples_have}/{n}':>10}")
    if res.sectors:
        lines.append("")
        lines.append("  Sectors: " + ", ".join(f"{k} {v}" for k, v in
                                               sorted(res.sectors.items(), key=lambda kv: -kv[1])))
    return lines


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def screen_toml(name: str, res: Learned, description: str,
                asset_type: str | None = None, profile_path: str | None = None) -> str:
    """The learned screen as a table for my-screens.toml. Each condition
    notes how many of your tickers and others it keeps; the ways to tighten
    are included as commented-out lines, ready to uncomment."""
    n, total = len(res.examples), res.others_total
    out = [f"[{name}]", f"description = {_toml_str(description)}"]
    if asset_type and asset_type != "all":
        out.append(f"asset_type = {_toml_str(asset_type)}")
    where = [s for s in res.steps if s.cond.op != "in"]
    sector = next((s.cond for s in res.steps if s.cond.op == "in"), None)
    if sector:
        out.append("sector = [" + ", ".join(_toml_str(v) for v in sector.value) + "]")
    out.append("where = [")
    for s in where:
        item = f"    {_toml_str(s.cond.text)},"
        out.append(f"{item:<34}# yours {s.examples_kept}/{n}, others left {s.others_kept:,}")
    if res.tighten:
        out.append("    # To tighten, uncomment (each keeps all your matched tickers):")
        for c, _, others in res.tighten:
            if c.op == "in":
                continue
            item = f"    # {_toml_str(c.text)},"
            out.append(f"{item:<34}# others left {others:,}")
    out.append("]")
    if res.misses:
        out.append("# Not matched: " + "; ".join(f"{t} ({', '.join(f)})" for t, f in res.misses.items()))
    if profile_path:
        out.append(f"# Learned details: {profile_path}")
    return "\n".join(out) + "\n"


def profile(res: Learned, meta: dict | None = None) -> dict:
    """Everything learned, as JSON-ready data, for refining the screen later."""
    def cond(c):
        return {"text": c.text, "field": c.field, "op": c.op,
                "value": list(c.value) if c.op == "in" else c.value}
    return {
        **(meta or {}),
        "examples": res.examples,
        "others_total": res.others_total,
        "screen": [{**cond(s.cond), "examples_kept": s.examples_kept,
                    "others_kept": s.others_kept, "lift": s.lift} for s in res.steps],
        "misses": res.misses,
        "tighten": [{**cond(c), "others_kept": o} for c, _, o in res.tighten],
        "sectors": res.sectors,
        "fields": [{
            "field": s.field, "unit": s.unit, "r": s.r,
            "examples": {"have": s.examples_have, "min": s.ex_min,
                         "median": s.ex_median, "max": s.ex_max},
            "others": {"have": s.others_have, "p10": s.o_p10,
                       "median": s.o_median, "p90": s.o_p90},
            "best_condition": ({**cond(s.best), "examples_kept": s.best_examples,
                                "others_pct": s.best_others_pct} if s.best else None),
        } for s in sorted(res.stats, key=lambda s: -(abs(s.r) if s.r is not None else -1))],
        "lookalikes": res.lookalikes,
    }
