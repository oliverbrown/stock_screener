"""Shared fake for Yahoo downloads: replaces the quote, fundamentals and
PEG requests and yfinance's price history, and counts every request."""

import pandas as pd

import stock_screener as ss


def flat_history(price=100.0, days=60, volume=50_000):
    return pd.DataFrame({"Open": [price] * days, "High": [price * 1.01] * days,
                         "Low": [price * 0.99] * days, "Close": [price] * days,
                         "Volume": [volume] * days},
                        index=pd.date_range("2026-06-01", periods=days, tz="America/New_York"))


class FakeYahoo:
    """quotes / summaries / pegs: ticker -> dict (or value). Mutate them
    between calls to simulate the market moving."""

    def __init__(self, monkeypatch, quotes, summaries=None, pegs=None, history=None):
        self.quotes, self.summaries, self.pegs = quotes, summaries or {}, pegs or {}
        self.history = history if history is not None else flat_history()
        self.calls = []                               # ("quote", [tickers]) / ("summary", t) / ...
        monkeypatch.setattr(ss, "_QUOTES", {})
        monkeypatch.setattr(ss, "_fetch_quote_batch", self._quote)
        monkeypatch.setattr(ss, "_fetch_summary", self._summary)
        monkeypatch.setattr(ss, "_fetch_trailing_peg", self._peg)
        fake = self

        class Ticker:
            def __init__(self, t):
                self.t = t

            def history(self, **k):
                fake.calls.append(("history", self.t))
                return fake.history

            @property
            def info(self):
                raise AssertionError("yfinance .info should no longer be used")

        monkeypatch.setattr(ss.yf, "Ticker", Ticker)

    def _quote(self, symbols):
        self.calls.append(("quote", list(symbols)))
        return {s: dict(self.quotes[s]) for s in symbols if s in self.quotes}

    def _summary(self, symbol):
        self.calls.append(("summary", symbol))
        return dict(self.summaries.get(symbol, {}))

    def _peg(self, symbol):
        self.calls.append(("peg", symbol))
        return self.pegs.get(symbol)

    def count(self, kind):
        return sum(1 for c in self.calls if c[0] == kind)
