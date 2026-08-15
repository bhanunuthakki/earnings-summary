# pyright: reportPrivateUsage=false
"""Deterministic New York Fed EFFR adapter and refresh contracts."""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

import execution.fetch_macro_series as macro
from macro_series import REGISTRY, MacroObservation, ProviderSpec, SeriesSpec
from macro_store import SeriesValue, upsert_series_values

FIXTURES = Path(__file__).parent / "fixtures" / "macro"
NOW = datetime(2026, 8, 14, 16, 0, tzinfo=UTC)
NYFED_PROVIDER = macro._NYFED_EFFR_PROVIDER


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
        _provider: ProviderSpec,
        *,
        observed_at: datetime,
        start_date: date,
        end_date: date,
        timeout_seconds: float,
    ) -> tuple[MacroObservation, ...]:
        del timeout_seconds
        return macro._parse_nyfed_effr_payload(
            _payload(name),
            series=series,
            observed_at=observed_at,
            start_date=start_date,
            end_date=end_date,
        )

    return load


def _refresh(db: Path, fixture: str) -> macro.MacroRefreshReceipt:
    return macro.refresh_series(
        series_ids=("fed_funds",),
        db_path=db,
        provider_overrides={"fed_funds": (NYFED_PROVIDER,)},
        provider_loaders={"nyfed_effr": _loader(fixture)},
        timeout_seconds=0.1,
        now=NOW,
    )


def test_default_loader_rejects_non_https_endpoint_before_network() -> None:
    forged = replace(NYFED_PROVIDER, path="file:///tmp/effr.json")

    with pytest.raises(macro.MacroProviderFetchError, match="approved HTTPS URL"):
        macro._default_nyfed_effr_loader(
            REGISTRY["fed_funds"],
            forged,
            observed_at=NOW,
            start_date=date(2026, 8, 13),
            end_date=date(2026, 8, 14),
            timeout_seconds=10.0,
        )


def test_default_registry_keeps_nyfed_unregistered_and_removes_treasury_proxy() -> None:
    providers = REGISTRY["fed_funds"].providers
    assert all(provider.kind != "nyfed_effr" for provider in providers)
    assert all(provider.value_key != "month1" for provider in providers)
    assert macro.DEFAULT_TIMEOUT_SECONDS == 10.0


def test_fresh_fixture_preserves_typed_provenance_and_refreshes(db: Path) -> None:
    parsed = macro._parse_nyfed_effr_payload(
        _payload("nyfed_effr_fresh.json"),
        series=REGISTRY["fed_funds"],
        observed_at=NOW,
        start_date=date(2024, 8, 14),
        end_date=date(2026, 8, 14),
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


@pytest.mark.parametrize("bad_value", [True, "3.63", float("nan"), float("inf")])
def test_macro_observation_ordinary_construction_requires_exact_finite_number(
    bad_value: object,
) -> None:
    with pytest.raises(ValidationError):
        MacroObservation(
            series_id="fed_funds",
            effective_date=date(2026, 8, 13),
            observed_at=NOW,
            value=cast("float", bad_value),
            units="pct",
            currency=None,
            source="new_york_fed",
        )


def test_macro_observation_rejects_naive_time_and_normalizes_aware_time_to_utc() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        MacroObservation(
            series_id="fed_funds",
            effective_date=date(2026, 8, 13),
            observed_at=datetime(2026, 8, 14, 9, 0),
            value=3.63,
            units="pct",
            currency=None,
            source="new_york_fed",
        )

    normalized = MacroObservation(
        series_id="fed_funds",
        effective_date=date(2026, 8, 13),
        observed_at=datetime(2026, 8, 14, 9, 0, tzinfo=timezone(timedelta(hours=-7))),
        value=3.63,
        units="pct",
        currency=None,
        source="new_york_fed",
    )
    assert normalized.observed_at == datetime(2026, 8, 14, 16, 0, tzinfo=UTC)
    assert normalized.observed_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    ("field_name", "coerced_value"),
    [
        ("effective_date", "2026-08-13"),
        ("effective_date", datetime(2026, 8, 13, 0, 0, tzinfo=UTC)),
        ("observed_at", "2026-08-14T16:00:00Z"),
        ("observed_at", 1_776_182_400),
        ("series_id", b"fed_funds"),
        ("units", b"pct"),
        ("source", b"new_york_fed"),
        ("currency", b"USD"),
        ("value", Decimal("3.63")),
    ],
)
def test_macro_observation_ordinary_construction_rejects_type_coercion(
    field_name: str,
    coerced_value: object,
) -> None:
    values: dict[str, object] = {
        "series_id": "fed_funds",
        "effective_date": date(2026, 8, 13),
        "observed_at": NOW,
        "value": 3.63,
        "units": "pct",
        "currency": None,
        "source": "new_york_fed",
    }
    values[field_name] = coerced_value
    with pytest.raises(ValidationError):
        MacroObservation(**cast("Any", values))


def test_model_copy_bypass_is_rejected_again_at_provider_boundary(db: Path) -> None:
    valid = MacroObservation(
        series_id="fed_funds",
        effective_date=date(2026, 8, 13),
        observed_at=NOW,
        value=3.63,
        units="pct",
        currency=None,
        source="new_york_fed",
    )
    forged = valid.model_copy(update={"effective_date": "2026-08-13"})

    def forged_loader(
        _series: SeriesSpec,
        _provider: ProviderSpec,
        *,
        observed_at: datetime,
        start_date: date,
        end_date: date,
        timeout_seconds: float,
    ) -> tuple[MacroObservation, ...]:
        del observed_at, start_date, end_date, timeout_seconds
        return (forged,)

    receipt = macro.refresh_series(
        series_ids=("fed_funds",),
        db_path=db,
        provider_overrides={"fed_funds": (NYFED_PROVIDER,)},
        provider_loaders={"nyfed_effr": forged_loader},
        now=NOW,
    )

    assert receipt.status is macro.RefreshStatus.FAILED
    assert receipt.series[0].attempts[0].outcome is macro.AttemptOutcome.ERROR
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM macro_series").fetchone()[0] == 0


def test_unknown_effr_row_field_remains_rejected() -> None:
    with pytest.raises(ValidationError, match="unexpectedMetadata"):
        macro._parse_nyfed_effr_payload(
            _payload("nyfed_effr_unknown_field.json"),
            series=REGISTRY["fed_funds"],
            observed_at=NOW,
            start_date=date(2024, 8, 14),
            end_date=date(2026, 8, 14),
        )


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        ("percentRate", True),
        ("percentRate", "3.63"),
        ("percentRate", float("nan")),
        ("percentRate", float("inf")),
        ("percentPercentile1", True),
    ],
)
def test_effr_json_number_is_exact_and_finite(field_name: str, bad_value: object) -> None:
    payload_object = deepcopy(_payload("nyfed_effr_fresh.json"))
    assert isinstance(payload_object, dict)
    payload = cast("dict[str, object]", payload_object)
    rows_object = payload["refRates"]
    assert isinstance(rows_object, list) and isinstance(rows_object[0], dict)
    first_row = cast("dict[str, object]", rows_object[0])
    first_row[field_name] = bad_value
    with pytest.raises((ValidationError, ValueError)):
        macro._parse_nyfed_effr_payload(
            payload,
            series=REGISTRY["fed_funds"],
            observed_at=NOW,
            start_date=date(2024, 8, 14),
            end_date=date(2026, 8, 14),
        )


@pytest.mark.parametrize(
    "bad_date",
    ["08/13/2026", "2026-08-13T00:00:00Z", "2026-08-15", "2024-08-13"],
)
def test_effr_date_is_exact_business_date_inside_requested_range(bad_date: str) -> None:
    payload_object = deepcopy(_payload("nyfed_effr_fresh.json"))
    assert isinstance(payload_object, dict)
    payload = cast("dict[str, object]", payload_object)
    rows_object = payload["refRates"]
    assert isinstance(rows_object, list) and isinstance(rows_object[0], dict)
    first_row = cast("dict[str, object]", rows_object[0])
    first_row["effectiveDate"] = bad_date
    with pytest.raises((ValidationError, ValueError)):
        macro._parse_nyfed_effr_payload(
            payload,
            series=REGISTRY["fed_funds"],
            observed_at=NOW,
            start_date=date(2024, 8, 14),
            end_date=date(2026, 8, 14),
        )


@pytest.mark.parametrize("holiday", ["2026-06-19", "2027-07-05", "2026-11-26"])
def test_effr_rejects_authoritative_new_york_fed_holidays(holiday: str) -> None:
    payload = {
        "refRates": [
            {
                "effectiveDate": holiday,
                "type": "EFFR",
                "percentRate": 3.63,
                "revisionIndicator": "",
            }
        ]
    }
    with pytest.raises((ValidationError, ValueError), match="business date"):
        macro._parse_nyfed_effr_payload(
            payload,
            series=REGISTRY["fed_funds"],
            observed_at=NOW,
            start_date=date(2026, 1, 1),
            end_date=date(2027, 12, 31),
        )


def test_friday_before_saturday_holiday_remains_a_nyfed_business_date() -> None:
    assert macro._is_nyfed_business_date(date(2026, 7, 3)) is True
    assert macro._is_nyfed_business_date(date(2026, 7, 4)) is False


def test_refresh_rejects_naive_as_of_before_provider_io(db: Path) -> None:
    calls = 0

    def counted_loader(*_args: object, **_kwargs: object) -> tuple[MacroObservation, ...]:
        nonlocal calls
        calls += 1
        return ()

    with pytest.raises(ValueError, match="timezone-aware"):
        macro.refresh_series(
            series_ids=("fed_funds",),
            db_path=db,
            provider_overrides={"fed_funds": (NYFED_PROVIDER,)},
            provider_loaders={"nyfed_effr": counted_loader},
            now=datetime(2026, 8, 14, 16, 0),
        )
    assert calls == 0


def test_refresh_normalizes_as_of_to_utc_before_request_dates(db: Path) -> None:
    captured: list[tuple[datetime, date, date]] = []

    def capture_loader(
        _series: SeriesSpec,
        _provider: ProviderSpec,
        *,
        observed_at: datetime,
        start_date: date,
        end_date: date,
        timeout_seconds: float,
    ) -> tuple[MacroObservation, ...]:
        del timeout_seconds
        captured.append((observed_at, start_date, end_date))
        return ()

    macro.refresh_series(
        series_ids=("fed_funds",),
        db_path=db,
        provider_overrides={"fed_funds": (NYFED_PROVIDER,)},
        provider_loaders={"nyfed_effr": capture_loader},
        now=datetime(2026, 8, 14, 20, 30, tzinfo=timezone(timedelta(hours=-7))),
    )

    observed_at, start_date, end_date = captured[0]
    assert observed_at == datetime(2026, 8, 15, 3, 30, tzinfo=UTC)
    assert observed_at.utcoffset() == timedelta(0)
    assert start_date == date(2024, 8, 15)
    assert end_date == date(2026, 8, 15)


def test_default_refresh_does_not_register_or_call_nyfed(db: Path) -> None:
    calls = 0

    def forbidden_loader(
        _series: SeriesSpec,
        _provider: ProviderSpec,
        *,
        observed_at: datetime,
        start_date: date,
        end_date: date,
        timeout_seconds: float,
    ) -> tuple[MacroObservation, ...]:
        nonlocal calls
        del observed_at, start_date, end_date, timeout_seconds
        calls += 1
        return ()

    receipt = macro.refresh_series(
        series_ids=("fed_funds",),
        db_path=db,
        provider_loaders={"nyfed_effr": forbidden_loader},
        now=NOW,
    )

    assert calls == 0
    assert receipt.status is macro.RefreshStatus.FAILED
    assert macro._exit_code(receipt.status) != 0


def test_duplicate_series_ids_are_rejected_before_provider_io(db: Path) -> None:
    calls = 0

    def counted_loader(
        series: SeriesSpec,
        provider: ProviderSpec,
        *,
        observed_at: datetime,
        start_date: date,
        end_date: date,
        timeout_seconds: float,
    ) -> tuple[MacroObservation, ...]:
        nonlocal calls
        calls += 1
        return _loader("nyfed_effr_fresh.json")(
            series,
            provider,
            observed_at=observed_at,
            start_date=start_date,
            end_date=end_date,
            timeout_seconds=timeout_seconds,
        )

    with pytest.raises(ValueError, match="duplicate series"):
        macro.refresh_series(
            series_ids=("fed_funds", "fed_funds"),
            db_path=db,
            provider_overrides={"fed_funds": (NYFED_PROVIDER,)},
            provider_loaders={"nyfed_effr": counted_loader},
            now=NOW,
        )
    assert calls == 0


def test_duplicate_injected_providers_are_rejected_before_io(db: Path) -> None:
    calls = 0

    def counted_loader(*_args: object, **_kwargs: object) -> tuple[MacroObservation, ...]:
        nonlocal calls
        calls += 1
        return ()

    with pytest.raises(ValueError, match="provider request budget"):
        macro.refresh_series(
            series_ids=("fed_funds",),
            db_path=db,
            provider_overrides={"fed_funds": (NYFED_PROVIDER, NYFED_PROVIDER)},
            provider_loaders={"nyfed_effr": counted_loader},
            now=NOW,
        )
    assert calls == 0


def test_structurally_equal_provider_spec_is_not_authorized(db: Path) -> None:
    forged = ProviderSpec(
        kind=NYFED_PROVIDER.kind,
        path=NYFED_PROVIDER.path,
        params=dict(NYFED_PROVIDER.params),
        date_key=NYFED_PROVIDER.date_key,
        value_key=NYFED_PROVIDER.value_key,
        source=NYFED_PROVIDER.source,
    )
    with pytest.raises(ValueError, match="approved source"):
        macro.refresh_series(
            series_ids=("fed_funds",),
            db_path=db,
            provider_overrides={"fed_funds": (forged,)},
            provider_loaders={"nyfed_effr": _loader("nyfed_effr_fresh.json")},
            now=NOW,
        )


def test_forged_provider_identity_is_rejected_without_persistence(db: Path) -> None:
    def forged_loader(
        _series: SeriesSpec,
        _provider: ProviderSpec,
        *,
        observed_at: datetime,
        start_date: date,
        end_date: date,
        timeout_seconds: float,
    ) -> tuple[MacroObservation, ...]:
        del observed_at, start_date, end_date, timeout_seconds
        return (
            MacroObservation.model_construct(
                series_id="vix",
                effective_date=date(2026, 8, 13),
                observed_at=NOW + timedelta(seconds=1),
                value=3.63,
                units="level",
                currency="USD",
                source="attacker",
            ),
        )

    receipt = macro.refresh_series(
        series_ids=("fed_funds",),
        db_path=db,
        provider_overrides={"fed_funds": (NYFED_PROVIDER,)},
        provider_loaders={"nyfed_effr": forged_loader},
        now=NOW,
    )

    assert receipt.status is macro.RefreshStatus.FAILED
    assert receipt.series[0].attempts[0].outcome is macro.AttemptOutcome.ERROR
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM macro_series").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("effective_date", "value"),
    [
        (date(2026, 8, 13), True),
        (date(2026, 8, 13), "3.63"),
        (date(2026, 8, 13), float("nan")),
        (datetime(2026, 8, 13, tzinfo=UTC), 3.63),
        (date(2026, 8, 15), 3.63),
        (date(2024, 8, 13), 3.63),
    ],
)
def test_provider_result_reparse_rejects_bad_value_or_date(
    db: Path,
    effective_date: object,
    value: object,
) -> None:
    def forged_loader(
        _series: SeriesSpec,
        _provider: ProviderSpec,
        *,
        observed_at: datetime,
        start_date: date,
        end_date: date,
        timeout_seconds: float,
    ) -> tuple[MacroObservation, ...]:
        del start_date, end_date, timeout_seconds
        return (
            MacroObservation.model_construct(
                series_id="fed_funds",
                effective_date=effective_date,
                observed_at=observed_at,
                value=value,
                units="pct",
                currency=None,
                source="new_york_fed",
            ),
        )

    receipt = macro.refresh_series(
        series_ids=("fed_funds",),
        db_path=db,
        provider_overrides={"fed_funds": (NYFED_PROVIDER,)},
        provider_loaders={"nyfed_effr": forged_loader},
        now=NOW,
    )

    assert receipt.status is macro.RefreshStatus.FAILED
    assert receipt.series[0].attempts[0].outcome is macro.AttemptOutcome.ERROR
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM macro_series").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("second_value", "outcome", "row_count"), [(3.63, "ok", 1), (3.64, "error", 0)]
)
def test_provider_result_duplicate_dates_dedupe_or_reject(
    db: Path,
    second_value: float,
    outcome: str,
    row_count: int,
) -> None:
    def duplicate_loader(
        _series: SeriesSpec,
        _provider: ProviderSpec,
        *,
        observed_at: datetime,
        start_date: date,
        end_date: date,
        timeout_seconds: float,
    ) -> tuple[MacroObservation, ...]:
        del start_date, end_date, timeout_seconds
        return (
            MacroObservation(
                series_id="fed_funds",
                effective_date=date(2026, 8, 13),
                observed_at=observed_at,
                value=3.63,
                units="pct",
                currency=None,
                source="new_york_fed",
            ),
            MacroObservation(
                series_id="fed_funds",
                effective_date=date(2026, 8, 13),
                observed_at=observed_at,
                value=second_value,
                units="pct",
                currency=None,
                source="new_york_fed",
            ),
        )

    receipt = macro.refresh_series(
        series_ids=("fed_funds",),
        db_path=db,
        provider_overrides={"fed_funds": (NYFED_PROVIDER,)},
        provider_loaders={"nyfed_effr": duplicate_loader},
        now=NOW,
    )

    assert receipt.series[0].attempts[0].outcome.value == outcome
    assert (
        sqlite3.connect(db).execute("SELECT COUNT(*) FROM macro_series").fetchone()[0] == row_count
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
        provider_overrides={"fed_funds": (NYFED_PROVIDER,)},
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
