# Contributing

Thanks for your interest in improving stock_screener. Bug reports, ideas,
new saved screens, documentation fixes and code are all welcome.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to help

- **Report a bug** — open an issue with the *Bug report* form. The exact
  command and the relevant lines of the run log (`./logs/`) are the most
  useful things to include.
- **Suggest a feature or indicator** — open an issue with the *Feature
  request* form, or start a thread in
  [Discussions](https://github.com/oliverbrown/stock_screener/discussions)
  if it's more of a question or an open-ended idea.
- **Share a saved screen** — a new entry for [`screens.toml`](screens.toml)
  is an easy first contribution.
- **Report a security problem** — see [SECURITY.md](SECURITY.md); please
  don't open a public issue.

## Setting up

Python 3.10 or newer.

```bash
git clone https://github.com/oliverbrown/stock_screener.git
cd stock_screener
python3 -m venv .venv && source .venv/bin/activate   # optional
pip install -r requirements-dev.txt
python3 -m pytest
```

The test suite is offline and takes a few seconds.

## Project layout

| Path | What it is |
|---|---|
| `stock_screener.py` | The screener: downloads, indicators, signals, filters, reports, CLI |
| `learn_screen.py` | `--learn`: learns a starter screen from example tickers (pure, no downloads) |
| `ticker_sources.py` | Live index / exchange ticker sources |
| `build_ticker_lists.py` | Writes reference ticker lists to `ticker-lists/` |
| `screens.toml` | Bundled saved screens |
| `run-screener.sh` | Example runner script |
| `tests/` | Offline tests; `tests/fake_yahoo.py` fakes Yahoo and counts requests |

## Making a change

1. Create a branch from `main`.
2. Make the change, with tests (see below).
3. Run `python3 -m pytest` — CI runs the same on Python 3.10, 3.11 and 3.12.
4. Update `README.md` if behaviour or options changed.
5. Open a pull request describing what changed and why.

### Tests must stay offline

Tests never contact Yahoo or any other site: CI has no reliable access to
them, and Yahoo rate-limits. Use the existing helpers instead:

- `tests/fake_yahoo.py` — `FakeYahoo` replaces the quote, fundamentals, PEG
  and price-history downloads, and records every request so tests can assert
  how many were made.
- `make_stock` / `make_fund` in `tests/test_classify.py` — made-up rows for
  testing signal rules.
- `monkeypatch` `stock_screener._fetch_stock_once` or `fetch_stock` for
  anything above the download layer.

If you verified a change against live data, say so in the pull request, but
keep the test itself offline.

### Conventions the code relies on

- **Changing what `fetch_stock` returns** (adding, removing or changing the
  meaning of a field): bump `TickerCache.VERSION`, so older cache entries
  are ignored rather than mixed with new ones.
- **New data fields** should be added to the `FIELDS` registry so they work
  with `--where`, `--sort-by`, `--csv` and `--list-fields`.
- **Indicators are context, not signals**, unless there's a strong reason:
  money flow, earnings dates, relative strength and volume add notes to the
  explanation but don't change the buy/sell signal. A change to the signal
  rules themselves should come with tests for the thresholds on both sides.
- **Distrust Yahoo's data.** Values can be missing, the text `"Infinity"`,
  in the wrong currency (ADRs), or per the wrong share class. Treat anything
  implausible as missing rather than letting it produce a signal, and add a
  test using the real value you saw.
- **Be gentle with Yahoo.** Avoid adding requests per ticker; prefer data
  already downloaded, batch where the endpoint allows it, and cache what
  changes slowly.
- **Match the surrounding style.** There's no formatter or linter
  configured; keep to the existing naming, comment density and layout.

### Personal files

Ticker lists, watch lists, reports, logs, caches and runner scripts named
`my-*` / `run-my-*` are gitignored on purpose. Please don't commit personal
watch lists or generated output.

## Not financial advice

The signals are rule-of-thumb heuristics. Contributions should keep the
README's disclaimer intact and avoid wording that presents output as a
recommendation to buy or sell.

## License

By contributing you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
