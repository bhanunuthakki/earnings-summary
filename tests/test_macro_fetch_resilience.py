# pyright: reportPrivateUsage=false
"""Offline resilience contracts for the macro-series acquisition job."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, datetime
from pathlib import Path

import pytest

import execution.fetch_macro_series as macro
from macro_series import REGISTRY
from macro_store import SeriesValue, upsert_series_values


def _schema(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE macro_series (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id TEXT NOT NULL,
                rate_date TEXT NOT NULL,
                value REAL NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(series_id, rate_date)
            );
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
            );
            CREATE TABLE provider_circuit_state (
                provider TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                next_probe_at TEXT,
                last_reason_code TEXT,
                last_success_at TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "macro.db"
    _schema(path)
    return path


def test_batch_write_is_value_change_aware_true_noop(db: Path) -> None:
    point = SeriesValue(
        series_id="vix",
        rate_date=date(2026, 8, 11),
        value=18.25,
        source="yfinance",
    )
    first = upsert_series_values((point,), db_path=db)
    before = (
        sqlite3.connect(db)
        .execute("SELECT id,created_at,value,source FROM macro_series")
        .fetchone()
    )
    second = upsert_series_values((point,), db_path=db)
    after = (
        sqlite3.connect(db)
        .execute("SELECT id,created_at,value,source FROM macro_series")
        .fetchone()
    )

    assert (first.inserted, first.updated, first.unchanged) == (1, 0, 0)
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 1)
    assert after == before


def test_batch_write_rolls_back_all_rows_on_mid_transaction_failure(db: Path) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """
            CREATE TRIGGER reject_bad_macro BEFORE INSERT ON macro_series
            WHEN NEW.series_id = 'bad'
            BEGIN SELECT RAISE(ABORT, 'reject bad macro'); END
            """
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(Exception, match="reject bad macro"):
        upsert_series_values(
            (
                SeriesValue(
                    series_id="vix",
                    rate_date=date(2026, 8, 11),
                    value=18.25,
                    source="yfinance",
                ),
                SeriesValue(
                    series_id="bad",
                    rate_date=date(2026, 8, 11),
                    value=1.0,
                    source="fixture",
                ),
            ),
            db_path=db,
        )
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM macro_series").fetchone()[0] == 0


def test_network_loader_runs_without_database_write_lock(db: Path) -> None:
    observed = {"write_lock_available": False}

    def loader(_symbol: str, *, timeout_seconds: float) -> list[tuple[date, float]]:
        conn = sqlite3.connect(db, timeout=0.05)
        try:
            conn.execute("BEGIN IMMEDIATE")
            observed["write_lock_available"] = True
            conn.rollback()
        finally:
            conn.close()
        return [(date(2026, 8, 11), 18.25)]

    receipt = macro.refresh_series(
        series_ids=("vix",),
        db_path=db,
        yfinance_loader=loader,
        timeout_seconds=0.5,
        now=datetime(2026, 8, 12, 12, 0, 0),
    )

    assert observed["write_lock_available"] is True
    assert receipt.status is macro.RefreshStatus.FRESH


def test_yfinance_call_is_timeout_bounded_and_receipted(db: Path) -> None:
    release = threading.Event()

    def loader(_symbol: str, *, timeout_seconds: float) -> list[tuple[date, float]]:
        release.wait(5.0)
        return [(date(2026, 8, 11), 18.25)]

    started = time.monotonic()
    try:
        receipt = macro.refresh_series(
            series_ids=("vix",),
            db_path=db,
            yfinance_loader=loader,
            timeout_seconds=0.05,
            now=datetime(2026, 8, 12, 12, 0, 0),
        )
    finally:
        release.set()

    assert time.monotonic() - started < 1.0
    assert receipt.status is macro.RefreshStatus.FAILED
    assert receipt.series[0].attempts[0].outcome is macro.AttemptOutcome.TIMEOUT
    row = (
        sqlite3.connect(db)
        .execute(
            "SELECT source_name,status,notes FROM source_calls WHERE source_name='yfinance' LIMIT 1"
        )
        .fetchone()
    )
    assert row[0:2] == ("yfinance", "error")
    assert "timeout" in row[2]


@pytest.mark.parametrize("circuit_state", [None, "OPEN", "HALF_OPEN", "CLOSED"])
def test_fmp_candidates_are_explicitly_disabled_without_provider_calls(
    db: Path, monkeypatch: pytest.MonkeyPatch, circuit_state: str | None
) -> None:
    if circuit_state is not None:
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO provider_circuit_state(provider,state) VALUES ('fmp',?)",
                (circuit_state,),
            )
            conn.commit()
        finally:
            conn.close()

    receipt = macro.refresh_series(
        series_ids=("fed_funds",),
        db_path=db,
        yfinance_loader=lambda *_a, **_k: [],
        timeout_seconds=0.05,
        now=datetime(2026, 8, 12, 12, 0, 0),
    )

    attempt = receipt.series[0].attempts[0]
    assert attempt.source == "FMP"
    assert attempt.outcome is macro.AttemptOutcome.DISABLED
    assert attempt.reason in {
        "circuit_open",
        "circuit_half_open",
        "circuit_unverified",
        "shared_recovery_only",
    }


def test_cached_degraded_is_explicit_and_partial_is_not_compute_eligible(db: Path) -> None:
    upsert_series_values(
        (
            SeriesValue(
                series_id="vix",
                rate_date=date(2026, 8, 10),
                value=20.0,
                source="yfinance",
            ),
        ),
        db_path=db,
    )

    cached = macro.refresh_series(
        series_ids=("vix",),
        db_path=db,
        yfinance_loader=lambda *_a, **_k: [],
        timeout_seconds=0.05,
        now=datetime(2026, 8, 12, 12, 0, 0),
    )
    partial = macro.refresh_series(
        series_ids=("vix", "fed_funds"),
        db_path=db,
        yfinance_loader=lambda *_a, **_k: [],
        timeout_seconds=0.05,
        now=datetime(2026, 8, 12, 12, 0, 0),
    )

    assert cached.status is macro.RefreshStatus.CACHED_DEGRADED
    assert cached.compute_eligible is True
    assert partial.status is macro.RefreshStatus.PARTIAL
    assert partial.compute_eligible is False


def test_successful_yahoo_payload_older_than_freshness_guard_is_not_fresh(db: Path) -> None:
    receipt = macro.refresh_series(
        series_ids=("vix",),
        db_path=db,
        yfinance_loader=lambda *_a, **_k: [(date(2026, 6, 1), 18.25)],
        timeout_seconds=0.05,
        now=datetime(2026, 8, 12, 12, 0, 0),
    )

    assert receipt.series[0].attempts[0].outcome is macro.AttemptOutcome.OK
    assert receipt.series[0].availability is macro.SeriesAvailability.UNAVAILABLE
    assert receipt.series[0].cache_age_days == 72
    assert receipt.status is macro.RefreshStatus.FAILED
    assert receipt.compute_eligible is False


def test_fed_funds_sixty_day_cache_is_stale_under_canonical_guard(db: Path) -> None:
    upsert_series_values(
        (
            SeriesValue(
                series_id="fed_funds",
                rate_date=date(2026, 6, 13),
                value=4.5,
                source="FMP",
            ),
        ),
        db_path=db,
    )

    receipt = macro.refresh_series(
        series_ids=("fed_funds",),
        db_path=db,
        yfinance_loader=lambda *_a, **_k: [],
        timeout_seconds=0.05,
        now=datetime(2026, 8, 12, 12, 0, 0),
    )

    assert receipt.series[0].cache_age_days == 60
    assert receipt.series[0].availability is macro.SeriesAvailability.UNAVAILABLE
    assert receipt.status is macro.RefreshStatus.FAILED
    assert receipt.compute_eligible is False


def test_registry_still_has_yfinance_first_and_fmp_fallbacks() -> None:
    assert REGISTRY["vix"].providers[0].kind == "yfinance"
    assert any(p.kind.startswith("fmp_") for p in REGISTRY["vix"].providers)


def test_typed_exit_codes_preserve_degradation() -> None:
    assert macro._exit_code(macro.RefreshStatus.FRESH) == 0
    assert macro._exit_code(macro.RefreshStatus.CACHED_DEGRADED) == 2
    assert macro._exit_code(macro.RefreshStatus.PARTIAL) == 3
    assert macro._exit_code(macro.RefreshStatus.FAILED) == 1
