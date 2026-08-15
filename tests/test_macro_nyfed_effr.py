# pyright: reportPrivateUsage=false
"""Deterministic New York Fed EFFR adapter and refresh contracts."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import execution.fetch_macro_series as macro
from macro_series import REGISTRY, MacroObservation, SeriesSpec
from macro_store import SeriesValue, upsert_series_values

FIXTURES = Path(__file__).parent / "fixtures" / "macro"
NOW = datetime(2026, 8, 14, 16, 0, tzinfo=UTC)


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


def _payload(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _loader(name: str):
    def load(
        series: SeriesSpec,
        _provider: object,
        *,
        observed_at: datetime,
        start_date: date,
        end_date: date,
        timeout_seconds: float,
    ) -> tuple[MacroObservation, ...]:
        del start_date, end_date, timeout_seconds
        return macro._parse_nyfed_effr_payload(
            _payload(name),
            series=series,
            observed_at=observed_at,
        )

    return load


def _refresh(db: Path, fixture: str) -> macro.MacroRefreshReceipt:
    return macro.refresh_series(
        series_ids=("fed_funds",),
        db_path=db,
        provider_loaders={"nyfed_effr": _loader(fixture)},
        timeout_seconds=0.1,
        now=NOW,
    )


def test_registry_routes_fed_funds_to_authoritative_non_fmp_source_first() -> None:
    first = REGISTRY["fed_funds"].providers[0]
    assert first.kind == "nyfed_effr"
    assert first.source == "new_york_fed"
    assert "newyorkfed.org" in first.path
    assert all(
        not provider.path.startswith("/api/v3") for provider in REGISTRY["fed_funds"].providers
    )
    assert macro.DEFAULT_TIMEOUT_SECONDS == 10.0


def test_fresh_fixture_preserves_typed_provenance_and_refreshes(db: Path) -> None:
    parsed = macro._parse_nyfed_effr_payload(
        _payload("nyfed_effr_fresh.json"),
        series=REGISTRY["fed_funds"],
        observed_at=NOW,
    )

    assert parsed[-1] == MacroObservation(
        series_id="fed_funds",
        effective_date=date(2026, 8, 13),
        observed_at=NOW,
        value=3.63,
        units="pct",
        currency=None,
        source="new_york_fed",
    )
    receipt = _refresh(db, "nyfed_effr_fresh.json")
    assert receipt.status is macro.RefreshStatus.FRESH
    assert receipt.compute_eligible is True
    assert receipt.series[0].cached_through == date(2026, 8, 13)
    assert receipt.series[0].attempts[0].source == "new_york_fed"


def test_unknown_effr_row_field_remains_rejected() -> None:
    with pytest.raises(ValidationError, match="unexpectedMetadata"):
        macro._parse_nyfed_effr_payload(
            _payload("nyfed_effr_unknown_field.json"),
            series=REGISTRY["fed_funds"],
            observed_at=NOW,
        )


def test_unchanged_fixture_is_a_true_noop(db: Path) -> None:
    upsert_series_values(
        (
            SeriesValue(
                series_id="fed_funds",
                rate_date=date(2026, 8, 13),
                value=3.63,
                source="new_york_fed",
            ),
        ),
        db_path=db,
    )
    receipt = _refresh(db, "nyfed_effr_unchanged.json")
    assert receipt.status is macro.RefreshStatus.FRESH
    assert receipt.write.model_dump() == {"inserted": 0, "updated": 0, "unchanged": 1}


def test_stale_fixture_remains_explicitly_unavailable(db: Path) -> None:
    receipt = _refresh(db, "nyfed_effr_stale.json")
    assert receipt.status is macro.RefreshStatus.FAILED
    assert receipt.compute_eligible is False
    assert receipt.series[0].availability is macro.SeriesAvailability.UNAVAILABLE
    assert receipt.series[0].cache_age_days == 74


def test_malformed_fixture_is_receipted_without_persistence(db: Path) -> None:
    receipt = _refresh(db, "nyfed_effr_malformed.json")
    assert receipt.status is macro.RefreshStatus.FAILED
    assert receipt.series[0].attempts[0].outcome is macro.AttemptOutcome.ERROR
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM macro_series").fetchone()[0] == 0


def test_unavailable_fixture_falls_through_and_stays_nonzero(db: Path) -> None:
    receipt = _refresh(db, "nyfed_effr_unavailable.json")
    assert receipt.status is macro.RefreshStatus.FAILED
    assert receipt.series[0].attempts[0].outcome is macro.AttemptOutcome.EMPTY
    assert receipt.series[0].attempts[-1].outcome is macro.AttemptOutcome.DISABLED
    assert macro._exit_code(receipt.status) != 0


def test_bounded_all_series_canary_does_not_regress_other_eleven(db: Path) -> None:
    other_ids = tuple(series_id for series_id in REGISTRY if series_id != "fed_funds")
    upsert_series_values(
        tuple(
            SeriesValue(
                series_id=series_id,
                rate_date=date(2026, 8, 13),
                value=10.0,
                source="yfinance",
            )
            for series_id in other_ids
        ),
        db_path=db,
    )
    before = (
        sqlite3.connect(db)
        .execute(
            "SELECT series_id,rate_date,value,source FROM macro_series "
            "WHERE series_id != 'fed_funds' ORDER BY series_id,rate_date"
        )
        .fetchall()
    )

    def yahoo(symbol: str, *, timeout_seconds: float) -> list[tuple[date, float]]:
        del timeout_seconds
        raw_value = 100.0 if symbol == "^TNX" else 10.0
        return [(date(2026, 8, 13), raw_value)]

    receipt = macro.refresh_series(
        series_ids=tuple(REGISTRY),
        db_path=db,
        yfinance_loader=yahoo,
        provider_loaders={"nyfed_effr": _loader("nyfed_effr_fresh.json")},
        timeout_seconds=0.1,
        now=NOW,
    )
    after = (
        sqlite3.connect(db)
        .execute(
            "SELECT series_id,rate_date,value,source FROM macro_series "
            "WHERE series_id != 'fed_funds' ORDER BY series_id,rate_date"
        )
        .fetchall()
    )

    assert receipt.status is macro.RefreshStatus.FRESH
    assert receipt.write.unchanged == 11
    assert before == after
    fed = next(item for item in receipt.series if item.series_id == "fed_funds")
    assert fed.availability is macro.SeriesAvailability.FRESH
