"""Offline tests for the buy/sell rules in classify_signal and classify_fund.

Each test builds a made-up stock/fund dict with neutral defaults and changes
only the fields that matter, so a failing test points straight at the rule.
No network access.
"""

import math

import pytest

from stock_screener import classify_fund, classify_signal


def make_stock(**overrides) -> dict:
    """A Technology stock (sector P/E ~28) with nothing notable about it."""
    stock = {
        "sector":       "Technology",
        "pe":           28.0,
        "pb":           5.0,
        "peg":          1.5,
        "rsi":          50.0,
        "debt_equity":  50.0,
        "margin":       20.0,
        "pct_vs_200":   5.0,
        "pct_off_high": -10.0,
        "pct_off_low":  30.0,
        "trend":        "up",
    }
    stock.update(overrides)
    return stock


def make_fund(**overrides) -> dict:
    """A Large Blend fund (category P/E ~21) trading at NAV, cheap to own."""
    fund = {
        "category":     "Large Blend",
        "pe":           21.0,
        "rsi":          50.0,
        "nav_premium":  0.0,
        "expense":      0.05,
        "pct_vs_200":   5.0,
        "pct_off_high": -10.0,
        "pct_off_low":  30.0,
        "trend":        "up",
    }
    fund.update(overrides)
    return fund


# ── Stocks ────────────────────────────────────────────────────────────────────
class TestStockSignals:
    def test_nothing_notable_is_neutral(self):
        signal, tags, thesis = classify_signal(make_stock(), None)
        assert signal == "neutral"
        assert tags == []
        assert "No strong" in thesis

    def test_low_pe_vs_sector_is_undervalued(self):
        # Technology avg 28 -> undervalued below 28 * 0.8 = 22.4
        signal, tags, thesis = classify_signal(make_stock(pe=20.0), None)
        assert signal == "undervalued"
        assert tags == ["undervalued"]
        assert "P/E 20.0x vs sector avg ~28x" in thesis

    def test_pe_just_above_threshold_is_not_undervalued(self):
        signal, _, _ = classify_signal(make_stock(pe=22.5), None)
        assert signal == "neutral"

    def test_low_pb_is_undervalued_when_pe_missing(self):
        signal, tags, _ = classify_signal(make_stock(pe=None, pb=1.2), None)
        assert tags == ["undervalued"]
        assert signal == "undervalued"

    def test_low_pb_ignored_when_pe_above_sector(self):
        signal, _, _ = classify_signal(make_stock(pe=30.0, pb=1.2), None)
        assert signal == "neutral"

    def test_unknown_sector_uses_default_pe(self):
        # DEFAULT_SECTOR_PE is 20 -> undervalued below 16
        assert classify_signal(make_stock(sector="Unknown", pe=15.0), None)[0] == "undervalued"
        assert classify_signal(make_stock(sector="Unknown", pe=17.0), None)[0] == "neutral"

    def test_max_pe_suppresses_undervalued(self):
        signal, _, _ = classify_signal(make_stock(pe=20.0), max_pe=15)
        assert signal == "neutral"

    @pytest.mark.parametrize("rsi, expected", [(34.9, "oversold"), (35.0, "neutral")])
    def test_oversold_below_rsi_35(self, rsi, expected):
        assert classify_signal(make_stock(rsi=rsi), None)[0] == expected

    @pytest.mark.parametrize("rsi, expected", [(70.1, "overbought"), (70.0, "neutral")])
    def test_overbought_above_rsi_70(self, rsi, expected):
        assert classify_signal(make_stock(rsi=rsi), None)[0] == expected

    def test_nan_rsi_gives_no_momentum_signal(self):
        signal, tags, _ = classify_signal(make_stock(rsi=math.nan), None)
        assert signal == "neutral"
        assert tags == []

    def test_undervalued_and_oversold_is_buy(self):
        signal, tags, _ = classify_signal(make_stock(pe=20.0, rsi=30.0), None)
        assert signal == "buy"
        assert tags == ["undervalued", "oversold"]

    def test_high_pe_is_overvalued(self):
        # Technology avg 28 -> overvalued above 28 * 1.25 = 35
        signal, tags, _ = classify_signal(make_stock(pe=40.0), None)
        assert signal == "overvalued"
        assert tags == ["overvalued"]

    def test_high_peg_is_overvalued(self):
        signal, _, thesis = classify_signal(make_stock(peg=2.5), None)
        assert signal == "overvalued"
        assert "PEG 2.5x" in thesis

    def test_overextended_above_200dma(self):
        signal, tags, thesis = classify_signal(make_stock(pct_vs_200=25.0), None)
        assert tags == ["overextended"]
        # There's no separate "overextended" headline; it reports as overbought.
        assert signal == "overbought"
        assert "+25% above its 200-day average" in thesis

    def test_not_overextended_at_20_pct(self):
        assert classify_signal(make_stock(pct_vs_200=20.0), None)[0] == "neutral"

    @pytest.mark.parametrize("extra", [{"rsi": 75.0}, {"pct_vs_200": 25.0}])
    def test_overvalued_and_stretched_is_sell(self, extra):
        signal, _, _ = classify_signal(make_stock(pe=40.0, **extra), None)
        assert signal == "sell"

    def test_buy_and_sell_tags_together_is_mixed(self):
        signal, tags, _ = classify_signal(make_stock(pe=20.0, rsi=75.0), None)
        assert signal == "mixed"
        assert tags == ["undervalued", "overbought"]

    @pytest.mark.parametrize("extra, flag", [
        ({"debt_equity": 250.0}, "debt/equity 250%"),
        ({"margin": -5.0},       "profit margin -5.0%"),
    ])
    def test_value_trap_suppresses_undervalued(self, extra, flag):
        signal, tags, thesis = classify_signal(make_stock(pe=20.0, **extra), None)
        assert signal == "neutral"
        assert "undervalued" not in tags
        assert "value trap" in thesis
        assert flag in thesis

    def test_quality_watch_without_value_call(self):
        signal, _, thesis = classify_signal(make_stock(debt_equity=250.0), None)
        assert signal == "neutral"
        assert "Quality watch: debt/equity 250%" in thesis

    def test_trend_mentioned_only_when_flagged(self):
        assert "Trend:" in classify_signal(make_stock(rsi=30.0, trend="down"), None)[2]
        assert "below the 200-day MA" in classify_signal(make_stock(rsi=30.0, trend="down"), None)[2]
        assert "Trend:" not in classify_signal(make_stock(), None)[2]


# ── ETFs / funds ──────────────────────────────────────────────────────────────
class TestFundSignals:
    def test_nothing_notable_is_hold(self):
        signal, tags, thesis = classify_fund(make_fund(), None)
        assert signal == "hold"
        assert tags == []
        assert "hold" in thesis

    @pytest.mark.parametrize("prem, expected", [(-2.0, "undervalued"), (-1.5, "hold")])
    def test_discount_to_nav_is_undervalued(self, prem, expected):
        assert classify_fund(make_fund(nav_premium=prem), None)[0] == expected

    @pytest.mark.parametrize("prem, expected", [(2.0, "overvalued"), (1.5, "hold")])
    def test_premium_to_nav_is_overvalued(self, prem, expected):
        assert classify_fund(make_fund(nav_premium=prem), None)[0] == expected

    def test_low_holdings_pe_vs_category_is_undervalued(self):
        # Large Blend avg 21 -> cheap below 16.8
        signal, _, thesis = classify_fund(make_fund(pe=16.0), None)
        assert signal == "undervalued"
        assert "Large Blend avg ~21x" in thesis

    def test_high_holdings_pe_vs_category_is_overvalued(self):
        # Large Blend avg 21 -> rich above 26.25
        assert classify_fund(make_fund(pe=27.0), None)[0] == "overvalued"

    def test_missing_category_uses_default_pe(self):
        # DEFAULT_CATEGORY_PE is 20 -> cheap below 16
        assert classify_fund(make_fund(category=None, pe=15.0), None)[0] == "undervalued"
        assert classify_fund(make_fund(category=None, pe=17.0), None)[0] == "hold"

    def test_bond_fund_without_pe_uses_nav_only(self):
        assert classify_fund(make_fund(pe=None), None)[0] == "hold"
        assert classify_fund(make_fund(pe=None, nav_premium=-2.0), None)[0] == "undervalued"

    def test_max_pe_suppresses_cheap_holdings_pe(self):
        assert classify_fund(make_fund(pe=16.0), max_pe=15)[0] == "hold"

    def test_expensive_fund_is_not_a_bargain(self):
        signal, tags, thesis = classify_fund(make_fund(nav_premium=-2.0, expense=0.80), None)
        assert signal == "hold"
        assert "undervalued" not in tags
        assert "expense ratio 0.80% is high" in thesis

    def test_cost_watch_keeps_signal(self):
        signal, _, thesis = classify_fund(make_fund(nav_premium=-2.0, expense=0.60), None)
        assert signal == "undervalued"
        assert "Cost watch: expense ratio 0.60%" in thesis

    def test_discount_and_oversold_is_buy(self):
        assert classify_fund(make_fund(nav_premium=-2.0, rsi=30.0), None)[0] == "buy"

    def test_premium_and_overbought_is_sell(self):
        assert classify_fund(make_fund(nav_premium=2.0, rsi=75.0), None)[0] == "sell"

    def test_discount_and_overbought_is_mixed(self):
        assert classify_fund(make_fund(nav_premium=-2.0, rsi=75.0), None)[0] == "mixed"

    def test_overextended_fund(self):
        signal, tags, _ = classify_fund(make_fund(pct_vs_200=25.0), None)
        assert tags == ["overextended"]
        assert signal == "overbought"
