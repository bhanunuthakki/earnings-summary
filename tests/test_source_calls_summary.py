"""Tests for the source_calls read side (sources.registry.summarize_source_calls).

The aggregator is the consumer the registry docstring promised — it turns the
write-only provenance log into a per-(source, kind) operational readout
(call volume, cache-skip rate, error rate, latency percentiles) so external
fetch behaviour and cache effectiveness are measurable.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import sources.registry as registry
from sources.registry import (
    CallStatus,
    PendingSourceCall,
    cache_effectiveness_overview,
    log_calls_batch,
    summarize_source_calls,
)

_CREATE = """
CREATE TABLE source_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    ticker TEXT,
    called_at TEXT NOT NULL,
    latency_ms INTEGER,
    status TEXT NOT NULL,
    http_code INTEGER,
    record_count INTEGER,
    notes TEXT
)
"""


def _seed(db: Path, rows: list[tuple[str, str, str, int | None, str, int | None]]) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(_CREATE)
        conn.executemany(
            "INSERT INTO source_calls (source_name, kind, called_at, latency_ms, "
            "status, record_count) VALUES (?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def test_missing_db_returns_empty(tmp_path: Path) -> None:
    assert summarize_source_calls(db_path=tmp_path / "nope.db") == []


def test_log_calls_batch_records_fmp_cache_hits(tmp_path: Path) -> None:
    """The FMP fetcher's batched logger writes ok/skipped rows that make the
    FMP cache-skip rate measurable through the read side — the gap that left the
    dominant fetch path invisible in source_calls."""
    db = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(_CREATE)
        conn.commit()
    finally:
        conn.close()

    original = registry._DB_PATH
    registry.set_db_path(db)
    try:
        log_calls_batch(
            [
                # income-statement: 1 live fetch, 3 served from cache (skip)
                PendingSourceCall(
                    source_name="fmp",
                    kind="income-statement",
                    ticker="nu",
                    status=CallStatus.OK,
                    latency_ms=120,
                    http_code=200,
                    record_count=5,
                ),
                *[
                    PendingSourceCall(
                        source_name="fmp",
                        kind="income-statement",
                        ticker="nu",
                        status=CallStatus.SKIPPED,
                        notes="cache_hit",
                    )
                    for _ in range(3)
                ],
            ]
        )
        # An empty batch is a no-op (must not raise).
        log_calls_batch([])
    finally:
        registry.set_db_path(original)

    (summary,) = summarize_source_calls(db_path=db)
    assert (summary.source_name, summary.kind) == ("fmp", "income-statement")
    assert summary.total == 4
    assert summary.ok == 1
    assert summary.skipped == 3
    assert summary.cache_skip_rate == 0.75  # 3 of 4 requests saved by the cache


def test_aggregates_counts_rates_and_latency(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _seed(
        db,
        [
            # fmp:statements — 4 calls: 2 ok, 1 cache skip, 1 error
            ("fmp", "statements", "2026-05-10", 100, "ok", 5),
            ("fmp", "statements", "2026-05-11", 300, "ok", 5),
            ("fmp", "statements", "2026-05-12", None, "skipped", None),
            ("fmp", "statements", "2026-05-13", 900, "error", None),
            # yfinance:price — 1 ok (proves multi-group + sort by volume)
            ("yfinance", "price", "2026-05-10", 50, "ok", 1),
        ],
    )
    out = summarize_source_calls(db_path=db)

    # Sorted by descending total → fmp/statements (4) first.
    assert [(s.source_name, s.kind, s.total) for s in out] == [
        ("fmp", "statements", 4),
        ("yfinance", "price", 1),
    ]

    fmp = out[0]
    assert (fmp.ok, fmp.skipped, fmp.errors) == (2, 1, 1)
    assert fmp.cache_skip_rate == 0.25
    assert fmp.error_rate == 0.25
    assert fmp.total_records == 10  # 5 + 5 (skip/error contribute no records)
    # Latencies present are [100, 300, 900]; nearest-rank p50=300, p95=900.
    assert fmp.p50_latency_ms == 300
    assert fmp.p95_latency_ms == 900


def test_since_filters_by_called_at(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _seed(
        db,
        [
            ("fmp", "statements", "2026-04-01", 100, "ok", 1),
            ("fmp", "statements", "2026-05-02", 100, "ok", 1),
            ("fmp", "statements", "2026-05-03", 100, "ok", 1),
        ],
    )
    out = summarize_source_calls(since="2026-05-01", db_path=db)
    assert len(out) == 1
    assert out[0].total == 2  # the April row is excluded


# ---------------------------------------------------------------------------
# Cost / savings dimension + the cross-source overview (v6 Smart caching)
# ---------------------------------------------------------------------------


def test_calls_saved_equals_skips_and_cost_zero_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``calls_saved`` is the network calls the cache avoided; with no configured
    per-call cost (flat-rate sources), ``cost_saved_usd`` is 0.0."""
    monkeypatch.delenv("SOURCE_COST_PER_CALL_USD", raising=False)
    db = tmp_path / "portfolio.db"
    _seed(
        db,
        [
            ("fmp", "statements", "2026-05-10", 100, "ok", 5),
            ("fmp", "statements", "2026-05-11", None, "skipped", None),
            ("fmp", "statements", "2026-05-12", None, "skipped", None),
        ],
    )
    (s,) = summarize_source_calls(db_path=db)
    assert s.calls_saved == 2
    assert s.cost_saved_usd == 0.0


def test_cost_saved_uses_env_per_call_cost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``SOURCE_COST_PER_CALL_USD`` translates avoided calls into dollars."""
    monkeypatch.setenv("SOURCE_COST_PER_CALL_USD", "0.01")
    db = tmp_path / "portfolio.db"
    _seed(
        db,
        [
            ("fmp", "statements", "2026-05-10", 100, "ok", 5),
            ("fmp", "statements", "2026-05-11", None, "skipped", None),
            ("fmp", "statements", "2026-05-12", None, "skipped", None),
            ("fmp", "statements", "2026-05-13", None, "skipped", None),
        ],
    )
    (s,) = summarize_source_calls(db_path=db)
    assert s.calls_saved == 3
    assert s.cost_saved_usd == pytest.approx(0.03)


def test_cache_effectiveness_overview_aggregates_across_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cross-source rollup sums calls / saved / errors and the dollar value."""
    monkeypatch.setenv("SOURCE_COST_PER_CALL_USD", "0.10")
    db = tmp_path / "portfolio.db"
    _seed(
        db,
        [
            # fmp:statements — 4 calls: 2 ok, 1 skip, 1 error
            ("fmp", "statements", "2026-05-10", 100, "ok", 5),
            ("fmp", "statements", "2026-05-11", 100, "ok", 5),
            ("fmp", "statements", "2026-05-12", None, "skipped", None),
            ("fmp", "statements", "2026-05-13", 900, "error", None),
            # yfinance:price — 2 calls: 1 skip, 1 ok
            ("yfinance", "price", "2026-05-10", 50, "ok", 1),
            ("yfinance", "price", "2026-05-11", None, "skipped", None),
        ],
    )
    ov = cache_effectiveness_overview(db_path=db)
    assert ov.total_calls == 6
    assert ov.calls_saved == 2  # one skip per source
    assert ov.network_calls == 4
    assert ov.cache_skip_rate == pytest.approx(2 / 6)
    assert ov.error_rate == pytest.approx(1 / 6)
    assert ov.cost_saved_usd == pytest.approx(0.20)  # 2 saved * $0.10
    assert len(ov.by_source) == 2


def test_overview_empty_db_is_all_zero(tmp_path: Path) -> None:
    ov = cache_effectiveness_overview(db_path=tmp_path / "nope.db")
    assert ov.total_calls == 0
    assert ov.calls_saved == 0
    assert ov.cache_skip_rate == 0.0
    assert ov.by_source == []
