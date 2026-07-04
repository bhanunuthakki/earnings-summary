"""Tests for the stage_0_news timeout fix — bounded concurrency in
execution/fetch_news.py's primary-feed collection.

Context: FMP's stock-news endpoint now 402s for the whole tracked book
(verified live 2026-07-03), so the ``auto`` source policy falls back to the
WebSearch+Opus path (~55s/ticker) for EVERY ticker. The dispatcher's primary
loop used to be fully sequential -- across a ~100-ticker book that is ~90
minutes of work crammed into a 900s stage budget, which is exactly why
stage_0_news timed out three days running with a completely empty log section
(subprocess.run(capture_output=True) discards the buffer on TimeoutExpired, so
none of the per-ticker events the child printed ever reached the cron log).

``collect_primary`` fixes this two ways, both pinned here:
  1. Bounded concurrency (``_PRIMARY_WORKERS`` at once) instead of one ticker
     at a time, so wall-clock is ceil(n/workers) calls instead of n.
  2. A hard per-ticker wall-clock budget (``_TICKER_TIMEOUT_S``): a ticker
     whose collection (FMP + optional websearch fallback) hangs past the
     budget degrades to "no rows this run" rather than blocking the others.

Hermetic — no real FMP/LLM/network calls; ``_collect_for_ticker`` is
monkeypatched with fakes that sleep to simulate a hang.
"""

from __future__ import annotations

import time

import pytest

import execution.fetch_news as fetch_news
from news.store import NewsRow


def _row(ticker: str, n: int = 1) -> NewsRow:
    return NewsRow(
        ticker=ticker,
        headline=f"{ticker} story {n}",
        url=f"https://news.example/{ticker.lower()}-{n}",
        published_at="2026-07-01 10:00:00",
        source_feed="fmp_stock_news",
    )


# ---------------------------------------------------------------------------
# Bounded concurrency: every ticker's rows still make it through.
# ---------------------------------------------------------------------------


def test_collect_primary_returns_rows_from_every_ticker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal run (no hangs) collects every ticker's rows regardless of the
    concurrent-batch grouping — parallelizing the loop must not drop tickers."""

    def _fake_collect(ticker: str, **_kwargs: object) -> list[NewsRow]:
        return [_row(ticker)]

    monkeypatch.setattr(fetch_news, "_collect_for_ticker", _fake_collect)

    tickers = [f"T{i}" for i in range(20)]
    rows = fetch_news.collect_primary(
        tickers, source="auto", db_path=":memory:", days=2, limit=50, workers=4
    )

    assert {r.ticker for r in rows} == set(tickers)
    assert len(rows) == 20


# ---------------------------------------------------------------------------
# Per-ticker timeout: one stuck ticker degrades, doesn't block the others.
# ---------------------------------------------------------------------------


def test_one_hung_ticker_times_out_without_blocking_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ticker whose collection hangs past the per-ticker budget contributes
    no rows (degrades like any other failure) — it must NOT prevent the other
    tickers' rows from being collected, and the whole call must return well
    under "hang forever"."""

    def _fake_collect(ticker: str, **_kwargs: object) -> list[NewsRow]:
        if ticker == "HUNG":
            time.sleep(5.0)  # longer than the test's timeout budget below
            return [_row("HUNG")]  # pragma: no cover — never reached in time
        return [_row(ticker)]

    monkeypatch.setattr(fetch_news, "_collect_for_ticker", _fake_collect)

    tickers = ["AAA", "HUNG", "BBB", "CCC"]
    t0 = time.monotonic()
    rows = fetch_news.collect_primary(
        tickers,
        source="auto",
        db_path=":memory:",
        days=2,
        limit=50,
        workers=4,
        per_ticker_timeout_s=0.2,
    )
    elapsed = time.monotonic() - t0

    tickers_seen = {r.ticker for r in rows}
    assert tickers_seen == {"AAA", "BBB", "CCC"}  # HUNG contributed nothing
    assert elapsed < 4.0  # bounded by the 0.2s budget, not the 5s sleep


def test_hung_ticker_logs_a_timeout_event(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A timed-out ticker is logged by name so a killed stage's cron log names
    the culprit instead of showing an empty section."""

    def _fake_collect(ticker: str, **_kwargs: object) -> list[NewsRow]:
        if ticker == "SLOW":
            time.sleep(2.0)
            return []  # pragma: no cover
        return [_row(ticker)]

    monkeypatch.setattr(fetch_news, "_collect_for_ticker", _fake_collect)

    fetch_news.collect_primary(
        ["FAST", "SLOW"],
        source="auto",
        db_path=":memory:",
        days=2,
        limit=50,
        workers=2,
        per_ticker_timeout_s=0.2,
    )

    err = capsys.readouterr().err
    assert '"event": "news_primary_ticker_timeout"' in err
    assert '"ticker": "SLOW"' in err


def test_ticker_raising_degrades_instead_of_propagating(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Belt-and-suspenders: even if _collect_for_ticker itself raises (it
    shouldn't — its own callees already degrade), the batch collector must not
    let one ticker's exception kill the whole run."""

    def _fake_collect(ticker: str, **_kwargs: object) -> list[NewsRow]:
        if ticker == "BOOM":
            raise RuntimeError("unexpected")
        return [_row(ticker)]

    monkeypatch.setattr(fetch_news, "_collect_for_ticker", _fake_collect)

    rows = fetch_news.collect_primary(
        ["OK", "BOOM"], source="auto", db_path=":memory:", days=2, limit=50, workers=2
    )

    assert {r.ticker for r in rows} == {"OK"}
    err = capsys.readouterr().err
    assert '"event": "news_primary_ticker_error"' in err


# ---------------------------------------------------------------------------
# Progress logging: start/per-ticker/done events are all flushed.
# ---------------------------------------------------------------------------


def test_progress_events_cover_start_each_ticker_and_done(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _fake_collect(ticker: str, **_kwargs: object) -> list[NewsRow]:
        return [_row(ticker)]

    monkeypatch.setattr(fetch_news, "_collect_for_ticker", _fake_collect)

    fetch_news.collect_primary(
        ["AAA", "BBB"], source="auto", db_path=":memory:", days=2, limit=50, workers=2
    )

    err = capsys.readouterr().err
    assert '"event": "news_primary_start"' in err
    assert '"event": "news_primary_ticker_done"' in err
    assert '"event": "news_primary_done"' in err
    # Every ticker gets its own per-ticker completion line (not just a summary).
    assert err.count('"event": "news_primary_ticker_done"') == 2
