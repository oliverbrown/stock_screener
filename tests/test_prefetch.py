"""Offline test of the prefetch-then-cache-only workflow through main()."""

import stock_screener as ss


def run_main(monkeypatch, tmp_path, *argv):
    monkeypatch.setattr(ss, "CACHE", None)
    monkeypatch.setattr(ss, "CACHE_ONLY", False)
    monkeypatch.setattr("sys.argv", ["stock_screener.py", *argv,
                                     "--cache-dir", str(tmp_path / "cache"),
                                     "--log-dir", str(tmp_path / "logs")])
    before = set((tmp_path / "logs").glob("*.log")) if (tmp_path / "logs").exists() else set()
    ss.main()
    (log,) = set((tmp_path / "logs").glob("*.log")) - before
    return log.read_text(encoding="utf-8")


def test_prefetch_then_cache_only(tmp_path, monkeypatch):
    from test_cmf import _report_row
    downloads = []

    def once(t):
        downloads.append(t)
        row = {k: v for k, v in _report_row().items() if k not in ("signal", "tags", "thesis")}
        return {**row, "ticker": t, "name": f"{t} Inc"}

    monkeypatch.setattr(ss, "_fetch_stock_once", once)

    log = run_main(monkeypatch, tmp_path, "--tickers", "AAA", "BBB", "--prefetch", "--quiet")
    assert sorted(downloads) == ["AAA", "BBB", "SPY"]          # benchmark too
    assert "Prefetch done: 2 ticker(s) cached" in log
    assert "STOCK SCREENER REPORT" not in log

    downloads.clear()
    log = run_main(monkeypatch, tmp_path, "--tickers", "AAA", "BBB", "--cache-only",
                   "--signal", "all", "--quiet")
    assert downloads == []                                      # all from the cache
    assert "Data: 2 ticker(s) from cache" in log
    assert "STOCK SCREENER REPORT" in log
