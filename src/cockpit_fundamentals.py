"""Canonical per-ticker fundamentals for the materialized cockpit cache."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, model_validator

from materialized_cache import cache_metadata, read_fresh_payload, write_payload_atomically
from sources.canonical_financial_series import (
    CanonicalFinancialObservation,
    CanonicalFinancialReadError,
    CanonicalFinancialSeries,
    CanonicalFinancialSeriesReader,
    FinancialCadence,
    SeriesContinuity,
    discover_canonical_financial_tickers,
)

__all__ = [
    "compute_from_db",
    "compute_snapshot",
    "materialize_fundamentals",
    "read_materialized_fundamentals",
]

_CACHE_REL: tuple[str, ...] = ("data", "cockpit_fundamentals.json")
_CACHE_SCHEMA = "cockpit-fundamentals-canonical/v2"
_SOURCE_AUTHORITY = "canonical_fact_resolution"
_METRICS = ("revenue", "free_cash_flow", "operating_cash_flow", "capital_expenditure")
_CADENCES = (
    FinancialCadence.QUARTERLY,
    FinancialCadence.SEMIANNUAL,
    FinancialCadence.REPORTED_TTM,
)
_SEMI_ANNUAL_GAP_DAYS = (175, 200)


class CockpitMetricResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["available", "unavailable"]
    value_pct: float | None = None
    reason_code: str | None = None
    calculation_kind: Literal["reported", "calculated", "unavailable"]
    lineage: dict[str, object]
    source_manifests: dict[str, object]

    @model_validator(mode="after")
    def _status_shape(self) -> CockpitMetricResult:
        if self.status == "available":
            if self.value_pct is None or self.reason_code is not None or not self.lineage:
                raise ValueError("available cockpit metric requires value and lineage")
        elif self.value_pct is not None or self.reason_code is None:
            raise ValueError("unavailable cockpit metric requires one reason and no value")
        if not self.source_manifests:
            raise ValueError("cockpit metric requires canonical source manifests")
        return self


class CockpitTickerFundamentals(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    status: Literal["available", "partial", "unavailable"]
    revenue_yoy: CockpitMetricResult
    fcf_margin: CockpitMetricResult


class CockpitFundamentalsSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["canonical-cockpit-fundamentals/v2"] = (
        "canonical-cockpit-fundamentals/v2"
    )
    cutoff: datetime
    source_authority: Literal["canonical_fact_resolution"] = _SOURCE_AUTHORITY
    status: Literal["complete"] = "complete"
    ticker_universe: tuple[str, ...]
    fundamentals: dict[str, CockpitTickerFundamentals]

    @model_validator(mode="after")
    def _bind_manifests(self) -> CockpitFundamentalsSnapshot:
        if tuple(sorted(self.fundamentals)) != self.ticker_universe:
            raise ValueError("cockpit ticker universe does not match fundamentals")
        for ticker, item in self.fundamentals.items():
            if item.ticker != ticker:
                raise ValueError("cockpit ticker payload does not match universe key")
            _validate_metric_manifests(
                item.revenue_yoy,
                ticker=ticker,
                cutoff=self.cutoff,
                allowed={
                    _series_key("revenue", FinancialCadence.QUARTERLY),
                    _series_key("revenue", FinancialCadence.SEMIANNUAL),
                },
                required={
                    _series_key("revenue", FinancialCadence.QUARTERLY),
                    _series_key("revenue", FinancialCadence.SEMIANNUAL),
                },
            )
            _validate_metric_manifests(
                item.fcf_margin,
                ticker=ticker,
                cutoff=self.cutoff,
                allowed={
                    _series_key(metric, cadence) for metric in _METRICS for cadence in _CADENCES
                },
                required={
                    _series_key("revenue", FinancialCadence.REPORTED_TTM),
                    _series_key("free_cash_flow", FinancialCadence.REPORTED_TTM),
                },
            )
        return self

    def value_projection(self) -> dict[str, tuple[float | None, float | None]]:
        return {
            ticker: (item.revenue_yoy.value_pct, item.fcf_margin.value_pct)
            for ticker, item in self.fundamentals.items()
        }


def _cache_path(repo_root: Path) -> Path:
    return repo_root.joinpath(*_CACHE_REL)


def _series_key(metric: str, cadence: FinancialCadence) -> str:
    return f"{metric}:{cadence.value}"


def _manifest(series: CanonicalFinancialSeries) -> dict[str, object]:
    return series.manifest()


def _lineage_observation_ids(value: object, *, key: str = "") -> set[str]:
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        found: set[str] = set()
        for child_key, child in mapping.items():
            found.update(_lineage_observation_ids(child, key=str(child_key)))
        return found
    if key.endswith("_observation_id") and isinstance(value, str):
        return {value}
    if key.endswith("_observation_ids") and isinstance(value, (list, tuple)):
        sequence = cast("list[object] | tuple[object, ...]", value)
        return {item for item in sequence if isinstance(item, str)}
    if isinstance(value, (list, tuple)):
        sequence = cast("list[object] | tuple[object, ...]", value)
        found = set()
        for child in sequence:
            found.update(_lineage_observation_ids(child))
        return found
    return set()


def _validate_metric_manifests(
    result: CockpitMetricResult,
    *,
    ticker: str,
    cutoff: datetime,
    allowed: set[str],
    required: set[str],
) -> None:
    keys = set(result.source_manifests)
    if not required.issubset(keys) or not keys.issubset(allowed):
        raise ValueError("cockpit metric contains an unexpected canonical series manifest")
    known_observations: set[str] = set()
    known_candidates: set[str] = set()
    for key, raw in result.source_manifests.items():
        if not isinstance(raw, dict):
            raise ValueError("cockpit canonical series manifest must be an object")
        series = CanonicalFinancialSeries.model_validate(raw)
        expected = _series_key(series.metric, series.cadence)
        if (
            key != expected
            or series.ticker != ticker
            or series.cutoff != cutoff
            or series.continuity is not SeriesContinuity.WINDOWED
        ):
            raise ValueError("cockpit canonical series manifest binding mismatch")
        known_observations.update(item.observation_id for item in series.observations)
        known_candidates.update(item.observation_id for item in series.candidates)
    lineage_ids = _lineage_observation_ids(result.lineage)
    if not lineage_ids.issubset(known_observations | known_candidates):
        raise ValueError("cockpit lineage is not bound to its canonical manifests")
    if result.status == "available" and not lineage_ids.issubset(known_observations):
        raise ValueError("available cockpit lineage requires admitted observations")


def _read_series(
    reader: CanonicalFinancialSeriesReader,
    metric: str,
    cadence: FinancialCadence,
) -> CanonicalFinancialSeries:
    return reader.read(metric, cadence=cadence, continuity=SeriesContinuity.WINDOWED)


def _same_coordinate(
    left: CanonicalFinancialObservation, right: CanonicalFinancialObservation
) -> bool:
    return left.coordinate == right.coordinate


def _observations_by_coordinate(
    series: CanonicalFinancialSeries,
) -> dict[tuple[object, ...], CanonicalFinancialObservation]:
    return {item.coordinate: item for item in series.observations}


def _unavailable_metric(
    reason: str,
    manifests: Mapping[str, object],
    *,
    lineage: dict[str, object] | None = None,
) -> CockpitMetricResult:
    return CockpitMetricResult(
        status="unavailable",
        reason_code=reason,
        calculation_kind="unavailable",
        lineage=lineage or {},
        source_manifests=dict(manifests),
    )


def _available_metric(
    value: Decimal,
    manifests: Mapping[str, object],
    *,
    calculation_kind: Literal["reported", "calculated"],
    lineage: dict[str, object],
) -> CockpitMetricResult:
    return CockpitMetricResult(
        status="available",
        value_pct=float(value),
        calculation_kind=calculation_kind,
        lineage=lineage,
        source_manifests=dict(manifests),
    )


def _pick_discrete_revenue(
    quarterly: CanonicalFinancialSeries,
    semiannual: CanonicalFinancialSeries,
) -> CanonicalFinancialSeries | None:
    available = [item for item in (quarterly, semiannual) if item.status == "available"]
    if len(available) != 1:
        return None
    return available[0]


def _revenue_yoy(
    quarterly: CanonicalFinancialSeries,
    semiannual: CanonicalFinancialSeries,
) -> CockpitMetricResult:
    manifests = {
        _series_key("revenue", FinancialCadence.QUARTERLY): _manifest(quarterly),
        _series_key("revenue", FinancialCadence.SEMIANNUAL): _manifest(semiannual),
    }
    series = _pick_discrete_revenue(quarterly, semiannual)
    if series is None:
        reason = (
            "ambiguous_supported_financial_cadence"
            if quarterly.status == semiannual.status == "available"
            else quarterly.reason_code or semiannual.reason_code or "revenue_series_unavailable"
        )
        return _unavailable_metric(reason, manifests)
    rows = sorted(series.observations, key=lambda item: item.period_end, reverse=True)
    if not rows or rows[0].value == 0:
        return _unavailable_metric("revenue_yoy_latest_unavailable", manifests)
    latest = rows[0]
    for prior in rows[1:]:
        age = (latest.period_end.date() - prior.period_end.date()).days
        if 330 <= age <= 430 and prior.value != 0:
            value = (latest.value / prior.value - Decimal(1)) * Decimal(100)
            return _available_metric(
                value,
                manifests,
                calculation_kind="calculated",
                lineage={
                    "formula": "(latest_revenue/prior_revenue-1)*100",
                    "cadence": series.cadence.value,
                    "latest_observation_id": latest.observation_id,
                    "prior_observation_id": prior.observation_id,
                },
            )
    return _unavailable_metric("revenue_yoy_comparable_period_unavailable", manifests)


def _reported_ttm_margin(
    revenue: CanonicalFinancialSeries,
    fcf: CanonicalFinancialSeries,
) -> CockpitMetricResult | None:
    manifests = {
        _series_key("revenue", FinancialCadence.REPORTED_TTM): _manifest(revenue),
        _series_key("free_cash_flow", FinancialCadence.REPORTED_TTM): _manifest(fcf),
    }
    if revenue.status != "available" or fcf.status != "available":
        blocking = next(
            (
                item.reason_code
                for item in (revenue, fcf)
                if item.reason_code not in {None, "exact_financial_concept_unavailable"}
            ),
            None,
        )
        return _unavailable_metric(blocking, manifests) if blocking else None
    revenue_by_coordinate = _observations_by_coordinate(revenue)
    common = [item for item in fcf.observations if item.coordinate in revenue_by_coordinate]
    if not common:
        return _unavailable_metric("reported_ttm_exact_period_mismatch", manifests)
    latest_fcf = max(common, key=lambda item: item.period_end)
    latest_revenue = revenue_by_coordinate[latest_fcf.coordinate]
    if latest_revenue.value <= 0:
        return _unavailable_metric("reported_ttm_revenue_nonpositive", manifests)
    return _available_metric(
        latest_fcf.value / latest_revenue.value * Decimal(100),
        manifests,
        calculation_kind="calculated",
        lineage={
            "formula": "reported_ttm_free_cash_flow/reported_ttm_revenue*100",
            "revenue_observation_id": latest_revenue.observation_id,
            "free_cash_flow_observation_id": latest_fcf.observation_id,
        },
    )


def _period_fcf(
    revenue: CanonicalFinancialObservation,
    direct: CanonicalFinancialSeries,
    operating: CanonicalFinancialSeries,
    capex: CanonicalFinancialSeries,
) -> tuple[Decimal | None, dict[str, object], str | None]:
    direct_map = _observations_by_coordinate(direct) if direct.status == "available" else {}
    direct_item = direct_map.get(revenue.coordinate)
    if direct_item is not None:
        return (
            direct_item.value,
            {
                "kind": "reported",
                "free_cash_flow_observation_id": direct_item.observation_id,
            },
            None,
        )
    rejected_direct = tuple(
        candidate.observation_id for candidate in direct.candidates if candidate.occupies(revenue)
    )
    if rejected_direct:
        return (
            None,
            {"rejected_direct_candidate_observation_ids": rejected_direct},
            "direct_free_cash_flow_candidate_rejected",
        )
    if direct.status == "unavailable" and direct.reason_code not in {
        "exact_financial_concept_unavailable",
    }:
        return None, {}, direct.reason_code or "direct_free_cash_flow_rejected"
    operating_map = (
        _observations_by_coordinate(operating) if operating.status == "available" else {}
    )
    capex_map = _observations_by_coordinate(capex) if capex.status == "available" else {}
    operating_item = operating_map.get(revenue.coordinate)
    capex_item = capex_map.get(revenue.coordinate)
    if operating_item is not None and capex_item is not None:
        return (
            operating_item.value + capex_item.value,
            {
                "kind": "calculated",
                "formula": "operating_cash_flow+capital_expenditure",
                "operating_cash_flow_observation_id": operating_item.observation_id,
                "capital_expenditure_observation_id": capex_item.observation_id,
            },
            None,
        )
    rejected = next(
        (
            item.reason_code
            for item in (operating, capex)
            if item.status == "unavailable"
            and item.reason_code not in {None, "exact_financial_concept_unavailable"}
        ),
        None,
    )
    return None, {}, rejected


def _discrete_margin(
    revenue: CanonicalFinancialSeries,
    direct: CanonicalFinancialSeries,
    operating: CanonicalFinancialSeries,
    capex: CanonicalFinancialSeries,
) -> CockpitMetricResult:
    manifests = {
        _series_key("revenue", revenue.cadence): _manifest(revenue),
        _series_key("free_cash_flow", revenue.cadence): _manifest(direct),
        _series_key("operating_cash_flow", revenue.cadence): _manifest(operating),
        _series_key("capital_expenditure", revenue.cadence): _manifest(capex),
    }
    rows: list[tuple[CanonicalFinancialObservation, Decimal, dict[str, object]]] = []
    blocking_reason: str | None = None
    blocking_lineage: dict[str, object] = {}
    ordered_revenue = sorted(revenue.observations, key=lambda item: item.period_end, reverse=True)
    for revenue_item in ordered_revenue:
        fcf, lineage, rejected = _period_fcf(revenue_item, direct, operating, capex)
        if rejected is not None:
            blocking_reason = rejected
            blocking_lineage = lineage
            break
        if fcf is not None:
            rows.append((revenue_item, fcf, lineage))
    if blocking_reason is not None:
        return _unavailable_metric(blocking_reason, manifests, lineage=blocking_lineage)
    selected: list[tuple[CanonicalFinancialObservation, Decimal, dict[str, object]]] = []
    if revenue.cadence is FinancialCadence.QUARTERLY:
        if len(rows) >= 4 and (rows[0][0].period_end - rows[3][0].period_end).days <= 330:
            selected = rows[:4]
        else:
            return _unavailable_metric("four_quarter_fcf_window_unavailable", manifests)
    else:
        if len(rows) < 3:
            return _unavailable_metric("corroborated_semiannual_fcf_window_unavailable", manifests)
        lo, hi = _SEMI_ANNUAL_GAP_DAYS
        if not all(
            lo <= (rows[index][0].period_end - rows[index + 1][0].period_end).days <= hi
            for index in (0, 1)
        ):
            return _unavailable_metric("semiannual_fcf_cadence_unavailable", manifests)
        newer, older = rows[0][0].period_end, rows[1][0].period_end
        if any(older < item.period_end < newer for item in ordered_revenue):
            return _unavailable_metric("semiannual_intervening_period", manifests)
        selected = rows[:2]
    revenue_sum = sum((item[0].value for item in selected), Decimal(0))
    if revenue_sum <= 0:
        return _unavailable_metric("fcf_margin_revenue_nonpositive", manifests)
    fcf_sum = sum((item[1] for item in selected), Decimal(0))
    return _available_metric(
        fcf_sum / revenue_sum * Decimal(100),
        manifests,
        calculation_kind="calculated",
        lineage={
            "formula": "sum(period_free_cash_flow)/sum(period_revenue)*100",
            "cadence": revenue.cadence.value,
            "periods": [
                {
                    "revenue_observation_id": item[0].observation_id,
                    **item[2],
                }
                for item in selected
            ],
        },
    )


def _fcf_margin(
    series: dict[tuple[str, FinancialCadence], CanonicalFinancialSeries],
) -> CockpitMetricResult:
    ttm_revenue = series["revenue", FinancialCadence.REPORTED_TTM]
    ttm_fcf = series["free_cash_flow", FinancialCadence.REPORTED_TTM]
    ttm_manifests = {
        _series_key("revenue", FinancialCadence.REPORTED_TTM): _manifest(ttm_revenue),
        _series_key("free_cash_flow", FinancialCadence.REPORTED_TTM): _manifest(ttm_fcf),
    }
    ttm = _reported_ttm_margin(
        ttm_revenue,
        ttm_fcf,
    )
    if ttm is not None:
        return ttm
    quarterly_revenue = series["revenue", FinancialCadence.QUARTERLY]
    semiannual_revenue = series["revenue", FinancialCadence.SEMIANNUAL]
    revenue = _pick_discrete_revenue(quarterly_revenue, semiannual_revenue)
    if revenue is None:
        manifests = {
            _series_key("revenue", FinancialCadence.QUARTERLY): _manifest(quarterly_revenue),
            _series_key("revenue", FinancialCadence.SEMIANNUAL): _manifest(semiannual_revenue),
        }
        reason = (
            "ambiguous_supported_financial_cadence"
            if quarterly_revenue.status == semiannual_revenue.status == "available"
            else quarterly_revenue.reason_code
            or semiannual_revenue.reason_code
            or "revenue_series_unavailable"
        )
        return _unavailable_metric(reason, {**ttm_manifests, **manifests})
    cadence = revenue.cadence
    result = _discrete_margin(
        revenue,
        series["free_cash_flow", cadence],
        series["operating_cash_flow", cadence],
        series["capital_expenditure", cadence],
    )
    return result.model_copy(
        update={"source_manifests": {**ttm_manifests, **result.source_manifests}}
    )


def _compute_ticker(
    conn: sqlite3.Connection,
    ticker: str,
    cutoff: datetime,
) -> CockpitTickerFundamentals:
    reader = CanonicalFinancialSeriesReader(conn, ticker, cutoff=cutoff)
    series = {
        (metric, cadence): _read_series(reader, metric, cadence)
        for metric in _METRICS
        for cadence in _CADENCES
    }
    revenue_yoy = _revenue_yoy(
        series["revenue", FinancialCadence.QUARTERLY],
        series["revenue", FinancialCadence.SEMIANNUAL],
    )
    fcf_margin = _fcf_margin(series)
    available_count = sum(item.status == "available" for item in (revenue_yoy, fcf_margin))
    status: Literal["available", "partial", "unavailable"] = (
        "available"
        if available_count == 2
        else "partial"
        if available_count == 1
        else "unavailable"
    )
    return CockpitTickerFundamentals(
        ticker=ticker,
        status=status,
        revenue_yoy=revenue_yoy,
        fcf_margin=fcf_margin,
    )


def compute_snapshot(
    conn: sqlite3.Connection,
    *,
    cutoff: datetime | None = None,
) -> CockpitFundamentalsSnapshot:
    """Compute one complete canonical snapshot or raise before publication."""
    cutoff_at = cutoff or datetime.now(UTC)
    if cutoff_at.tzinfo is None:
        raise ValueError("cockpit fundamentals cutoff must be timezone-aware")
    cutoff_at = cutoff_at.astimezone(UTC)
    owns_snapshot = not conn.in_transaction
    try:
        if owns_snapshot:
            conn.execute("BEGIN")
        tickers = discover_canonical_financial_tickers(
            conn,
            metrics=_METRICS,
            cadences=_CADENCES,
            cutoff=cutoff_at,
        )
        fundamentals = {ticker: _compute_ticker(conn, ticker, cutoff_at) for ticker in tickers}
        return CockpitFundamentalsSnapshot(
            cutoff=cutoff_at,
            ticker_universe=tickers,
            fundamentals=fundamentals,
        )
    finally:
        if owns_snapshot and conn.in_transaction:
            conn.rollback()


def compute_from_db(
    conn: sqlite3.Connection,
    *,
    cutoff: datetime | None = None,
) -> dict[str, tuple[float | None, float | None]]:
    """Compatibility projection for render fallback; canonical data only."""
    try:
        return compute_snapshot(conn, cutoff=cutoff).value_projection()
    except (CanonicalFinancialReadError, sqlite3.Error, ValueError):
        return {}


def materialize_fundamentals(
    conn: sqlite3.Connection,
    repo_root: Path,
    *,
    cutoff: datetime | None = None,
) -> int:
    """Atomically replace the cache only after a complete canonical read."""
    snapshot = compute_snapshot(conn, cutoff=cutoff)
    payload: dict[str, object] = {
        **cache_metadata(_CACHE_SCHEMA, now=snapshot.cutoff),
        "snapshot": snapshot.model_dump(mode="json"),
    }
    write_payload_atomically(_cache_path(repo_root), payload, prefix="cockpit_fundamentals.")
    return len(snapshot.ticker_universe)


def _read_cached_snapshot(repo_root: Path) -> CockpitFundamentalsSnapshot | None:
    payload = read_fresh_payload(_cache_path(repo_root), schema=_CACHE_SCHEMA)
    if not payload:
        return None
    raw = payload.get("snapshot")
    computed_at = payload.get("computed_at")
    if not isinstance(raw, dict) or not isinstance(computed_at, str):
        return None
    try:
        snapshot = CockpitFundamentalsSnapshot.model_validate(raw)
        envelope_cutoff = datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
        if envelope_cutoff.tzinfo is None:
            envelope_cutoff = envelope_cutoff.replace(tzinfo=UTC)
        else:
            envelope_cutoff = envelope_cutoff.astimezone(UTC)
    except ValueError:
        return None
    if envelope_cutoff != snapshot.cutoff.astimezone(UTC):
        return None
    return snapshot


def read_materialized_fundamentals(
    repo_root: Path,
) -> dict[str, tuple[float | None, float | None]]:
    """Read a complete, fresh canonical cache and project existing values."""
    snapshot = _read_cached_snapshot(repo_root)
    return {} if snapshot is None else snapshot.value_projection()
