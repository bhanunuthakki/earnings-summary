"""Exact canonical reported financial series with explicit cadence admission."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, model_validator

from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine
from provenance.fact_plane_v2 import FactDimensionV2
from provenance.fact_read_model import FactReadModel, ProvenanceBundle
from provenance.metric_ontology import CanonicalMetricDefinitionRevision, MetricOntology

FINANCIAL_CONCEPT_NAMESPACE = "urn:earnings-summary:legacy:financial"
_QUARTER_DAYS = (70, 105)
_SEMIANNUAL_DAYS = (175, 200)
_REPORTED_TTM_DAYS = (345, 385)


class CanonicalFinancialReadError(RuntimeError):
    """The canonical source plane could not be enumerated as a complete read."""


class FinancialCadence(StrEnum):
    QUARTERLY = "quarterly"
    SEMIANNUAL = "semiannual"
    REPORTED_TTM = "reported_ttm"


class SeriesContinuity(StrEnum):
    STRICT_CONTIGUOUS = "strict_contiguous"
    WINDOWED = "windowed"


class CanonicalFinancialObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str
    cadence: FinancialCadence
    metric_id: str
    metric_definition_revision_id: str
    canonical_metric_cell_id: str
    canonical_resolution_revision_id: str
    observation_id: str
    observation_payload_sha256: str
    document_version_id: str
    source_locator: dict[str, object]
    reporting_entity_id: str
    scope_security_id: str | None
    period_start: datetime
    period_end: datetime
    fiscal_year: int
    fiscal_period: str
    currency: str
    unit: str
    accounting_basis: str
    consolidation_scope: str
    dimensions: tuple[FactDimensionV2, ...]
    value: Decimal

    @property
    def coordinate(self) -> tuple[object, ...]:
        return (
            self.period_start,
            self.period_end,
            self.fiscal_year,
            self.fiscal_period,
            self.reporting_entity_id,
            self.scope_security_id,
            self.currency,
            self.unit,
            self.accounting_basis,
            self.consolidation_scope,
            tuple(
                json.dumps(item.canonical_member, sort_keys=True, separators=(",", ":"))
                for item in self.dimensions
            ),
        )


class CanonicalFinancialCandidate(BaseModel):
    """One exact-name source candidate retained before cadence admission."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: str
    fact_cell_id: str
    reporting_entity_id: str
    scope_security_id: str | None
    period_start: datetime | None
    period_end: datetime
    fiscal_year: int | None
    fiscal_period: str
    currency: str | None
    unit: str
    accounting_basis: str
    consolidation_scope: str
    duration_days: int | None
    cadence_supported: bool

    def occupies(self, observation: CanonicalFinancialObservation) -> bool:
        """Match the fiscal coordinate even when other semantics are rejected."""
        return (
            self.period_end == observation.period_end
            and self.fiscal_year == observation.fiscal_year
            and self.fiscal_period == observation.fiscal_period
        )


class CanonicalFinancialCoordinate(BaseModel):
    """One supported-label fiscal coordinate and its exact disposition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    period_end: datetime
    fiscal_year: int | None
    fiscal_period: str
    status: Literal["admitted", "rejected", "ambiguous", "unavailable"]
    reason_code: str | None = None
    candidates: tuple[CanonicalFinancialCandidate, ...]
    observation: CanonicalFinancialObservation | None = None

    @model_validator(mode="after")
    def _status_shape(self) -> CanonicalFinancialCoordinate:
        if self.status == "admitted":
            if self.observation is None or self.reason_code is not None:
                raise ValueError("admitted coordinate requires only one observation")
        elif self.observation is not None or self.reason_code is None:
            raise ValueError("non-admitted coordinate requires one reason")
        return self


class CanonicalFinancialCoordinateProjection(BaseModel):
    """Complete coordinate inventory for one exact metric/cadence read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    metric: str
    cadence: FinancialCadence
    cutoff: datetime
    status: Literal["available", "unavailable"]
    reason_code: str | None = None
    coordinates: tuple[CanonicalFinancialCoordinate, ...] = ()

    @model_validator(mode="after")
    def _status_shape(self) -> CanonicalFinancialCoordinateProjection:
        if self.status == "available" and self.reason_code is not None:
            raise ValueError("available projection cannot carry a failure reason")
        if self.status == "unavailable" and (self.reason_code is None or self.coordinates):
            raise ValueError("unavailable projection requires only one reason")
        return self


class CanonicalFinancialSeries(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    metric: str
    cadence: FinancialCadence
    continuity: SeriesContinuity
    cutoff: datetime
    status: Literal["available", "unavailable"]
    reason_code: str | None = None
    candidates: tuple[CanonicalFinancialCandidate, ...] = ()
    observations: tuple[CanonicalFinancialObservation, ...] = ()

    @model_validator(mode="after")
    def _status_shape(self) -> CanonicalFinancialSeries:
        if self.status == "available" and (not self.observations or self.reason_code is not None):
            raise ValueError("available canonical financial series requires observations only")
        if self.status == "unavailable" and (self.observations or self.reason_code is None):
            raise ValueError("unavailable canonical financial series requires one reason")
        return self

    def manifest(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def _clock(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _duration_days(start: object, end: object) -> int | None:
    if start is None or end is None:
        return None
    try:
        start_at = _clock(start)
        end_at = _clock(end)
    except (TypeError, ValueError):
        return None
    return (end_at.date() - start_at.date()).days + 1


def _label_matches(cadence: FinancialCadence, fiscal_period: object) -> bool:
    label = str(fiscal_period) if fiscal_period is not None else ""
    if cadence is FinancialCadence.QUARTERLY:
        return label in {"Q1", "Q2", "Q3", "Q4"}
    if cadence is FinancialCadence.SEMIANNUAL:
        return label in {"Q2", "Q4"}
    return label == "TTM"


def _duration_matches(cadence: FinancialCadence, days: int | None) -> bool:
    if days is None:
        return False
    if cadence is FinancialCadence.QUARTERLY:
        minimum, maximum = _QUARTER_DAYS
    elif cadence is FinancialCadence.SEMIANNUAL:
        minimum, maximum = _SEMIANNUAL_DAYS
    else:
        minimum, maximum = _REPORTED_TTM_DAYS
    return minimum <= days <= maximum


def _definition_semantics(definition: CanonicalMetricDefinitionRevision) -> object:
    return definition.model_dump(
        include={
            "lifecycle",
            "definition_text",
            "value_kind",
            "period_kind",
            "unit_family",
            "accounting_basis",
            "scope_constraints",
        }
    )


def discover_canonical_financial_tickers(
    conn: sqlite3.Connection,
    *,
    metrics: tuple[str, ...],
    cadences: tuple[FinancialCadence, ...],
    cutoff: datetime,
) -> tuple[str, ...]:
    """Discover the exact source-candidate universe without selecting winners."""
    if cutoff.tzinfo is None:
        raise ValueError("canonical financial cutoff must be timezone-aware")
    if not metrics or not cadences:
        return ()
    try:
        rows = conn.execute(
            "SELECT document.ticker,source.period_start,source.period_end,source.fiscal_period,"
            "source.knowledge_at,source.recorded_at,observation.knowledge_at,"
            "observation.recorded_at,document.recorded_at "
            "FROM fact_cells_v2 source JOIN fact_observations_v2 observation "
            "ON observation.fact_cell_id=source.fact_cell_id "
            "AND observation.observation_kind='reported' "
            "JOIN evidence_document_versions document "
            "ON document.document_version_id=observation.document_version_id "
            "WHERE source.concept_namespace=? "
            "AND source.concept_name IN (SELECT value FROM json_each(?)) "
            "AND source.period_kind='duration'",
            (FINANCIAL_CONCEPT_NAMESPACE, json.dumps(metrics)),
        ).fetchall()
    except sqlite3.Error as exc:
        raise CanonicalFinancialReadError("canonical_financial_schema_unavailable") from exc
    cutoff_at = cutoff.astimezone(UTC)
    tickers: set[str] = set()
    try:
        for row in rows:
            if not all(_clock(row[index]) <= cutoff_at for index in range(4, 9)):
                continue
            if any(_label_matches(cadence, row[3]) for cadence in cadences):
                ticker = str(row[0]).strip().upper()
                if ticker:
                    tickers.add(ticker)
    except (TypeError, ValueError) as exc:
        raise CanonicalFinancialReadError("canonical_financial_clock_invalid") from exc
    return tuple(sorted(tickers))


class CanonicalFinancialSeriesReader:
    """Read admitted actuals at one caller-owned cutoff and SQLite snapshot."""

    def __init__(self, conn: sqlite3.Connection, ticker: str, *, cutoff: datetime) -> None:
        if cutoff.tzinfo is None:
            raise ValueError("canonical financial cutoff must be timezone-aware")
        self._conn = conn
        self._ticker = ticker.upper()
        self._cutoff = cutoff.astimezone(UTC)
        self._resolver = CanonicalFactResolutionEngine(conn)
        self._ontology = MetricOntology(conn)
        self._reader = FactReadModel(conn)
        self._cache: dict[
            tuple[str, FinancialCadence, SeriesContinuity], CanonicalFinancialSeries
        ] = {}
        self._coordinate_cache: dict[
            tuple[str, FinancialCadence], CanonicalFinancialCoordinateProjection
        ] = {}

    def read(
        self,
        metric: str,
        *,
        cadence: FinancialCadence = FinancialCadence.QUARTERLY,
        continuity: SeriesContinuity = SeriesContinuity.STRICT_CONTIGUOUS,
    ) -> CanonicalFinancialSeries:
        key = (metric, cadence, continuity)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        result = self._read_uncached(metric, cadence, continuity)
        self._cache[key] = result
        return result

    def project_coordinates(
        self,
        metric: str,
        *,
        cadence: FinancialCadence = FinancialCadence.QUARTERLY,
    ) -> CanonicalFinancialCoordinateProjection:
        """Retain one explicit admission disposition per fiscal coordinate."""
        key = (metric, cadence)
        cached = self._coordinate_cache.get(key)
        if cached is not None:
            return cached
        result = self._project_coordinates_uncached(metric, cadence)
        self._coordinate_cache[key] = result
        return result

    def _projection_unavailable(
        self,
        metric: str,
        cadence: FinancialCadence,
        reason: str,
    ) -> CanonicalFinancialCoordinateProjection:
        return CanonicalFinancialCoordinateProjection(
            ticker=self._ticker,
            metric=metric,
            cadence=cadence,
            cutoff=self._cutoff,
            status="unavailable",
            reason_code=reason,
        )

    def _project_coordinates_uncached(
        self,
        metric: str,
        cadence: FinancialCadence,
    ) -> CanonicalFinancialCoordinateProjection:
        if not metric:
            return self._projection_unavailable(metric, cadence, "empty_exact_financial_metric")
        try:
            source_rows = self._conn.execute(
                """
                SELECT observation.observation_id,source.fact_cell_id,
                       source.reporting_entity_id,source.scope_security_id,
                       source.period_start,source.period_end,
                       source.fiscal_year,source.fiscal_period,source.currency,source.unit_key,
                       source.accounting_basis,source.consolidation_scope,
                       source.knowledge_at,source.recorded_at,
                       observation.knowledge_at,observation.recorded_at,
                       document.recorded_at,source.scope_security_id
                FROM fact_cells_v2 AS source
                JOIN fact_observations_v2 AS observation
                  ON observation.fact_cell_id=source.fact_cell_id
                 AND observation.observation_kind='reported'
                JOIN evidence_document_versions AS document
                  ON document.document_version_id=observation.document_version_id
                WHERE upper(document.ticker)=?
                  AND source.concept_namespace=?
                  AND source.concept_name=?
                  AND source.period_kind='duration'
                ORDER BY source.period_end,observation.observation_id
                """,
                (self._ticker, FINANCIAL_CONCEPT_NAMESPACE, metric),
            ).fetchall()
        except sqlite3.Error:
            return self._projection_unavailable(
                metric, cadence, "canonical_financial_schema_unavailable"
            )
        relevant: list[tuple[object, ...]] = []
        try:
            for row in source_rows:
                if not all(_clock(row[index]) <= self._cutoff for index in range(12, 17)):
                    continue
                if _label_matches(cadence, row[7]):
                    relevant.append(tuple(row))
        except (TypeError, ValueError):
            return self._projection_unavailable(
                metric, cadence, "canonical_financial_clock_invalid"
            )
        grouped: dict[tuple[datetime, int | None, str], list[tuple[object, ...]]] = {}
        try:
            for row in relevant:
                coordinate = (
                    _clock(row[5]),
                    None if row[6] is None else int(str(row[6])),
                    str(row[7]),
                )
                grouped.setdefault(coordinate, []).append(row)
        except (TypeError, ValueError):
            return self._projection_unavailable(
                metric, cadence, "canonical_financial_coordinate_invalid"
            )
        coordinates = tuple(
            self._project_coordinate(metric, cadence, coordinate, tuple(rows))
            for coordinate, rows in sorted(grouped.items(), key=lambda item: item[0][0])
        )
        return CanonicalFinancialCoordinateProjection(
            ticker=self._ticker,
            metric=metric,
            cadence=cadence,
            cutoff=self._cutoff,
            status="available",
            coordinates=coordinates,
        )

    def _project_coordinate(
        self,
        metric: str,
        cadence: FinancialCadence,
        coordinate: tuple[datetime, int | None, str],
        rows: tuple[tuple[object, ...], ...],
    ) -> CanonicalFinancialCoordinate:
        candidates = tuple(
            CanonicalFinancialCandidate(
                observation_id=str(row[0]),
                fact_cell_id=str(row[1]),
                reporting_entity_id=str(row[2]),
                scope_security_id=None if row[3] is None else str(row[3]),
                period_start=None if row[4] is None else _clock(row[4]),
                period_end=_clock(row[5]),
                fiscal_year=None if row[6] is None else int(str(row[6])),
                fiscal_period=str(row[7]),
                currency=None if row[8] is None else str(row[8]),
                unit=str(row[9]),
                accounting_basis=str(row[10]),
                consolidation_scope=str(row[11]),
                duration_days=_duration_days(row[4], row[5]),
                cadence_supported=_duration_matches(cadence, _duration_days(row[4], row[5])),
            )
            for row in rows
        )

        def failed(
            status: Literal["rejected", "ambiguous", "unavailable"], reason: str
        ) -> CanonicalFinancialCoordinate:
            return CanonicalFinancialCoordinate(
                period_end=coordinate[0],
                fiscal_year=coordinate[1],
                fiscal_period=coordinate[2],
                status=status,
                reason_code=reason,
                candidates=candidates,
            )

        if coordinate[0] > self._cutoff:
            return failed("rejected", "future_financial_period")
        if coordinate[1] is None:
            return failed("rejected", "canonical_financial_coordinate_invalid")
        if any(not candidate.cadence_supported for candidate in candidates):
            return failed("rejected", "unsupported_financial_cadence_or_duration")

        cells: dict[str, str] = {}
        for candidate in candidates:
            binding = self._ontology.binding_as_known(candidate.observation_id, self._cutoff)
            if (
                binding is None
                or binding.binding_status != "bound"
                or binding.canonical_metric_cell_id is None
            ):
                return failed("rejected", "exact_financial_concept_not_admitted")
            try:
                cell_row = self._conn.execute(
                    "SELECT cell.metric_id,cell.knowledge_at,cell.recorded_at,"
                    "metric.knowledge_at,metric.recorded_at "
                    "FROM canonical_metric_cells AS cell "
                    "JOIN canonical_metrics AS metric ON metric.metric_id=cell.metric_id "
                    "WHERE cell.canonical_metric_cell_id=?",
                    (binding.canonical_metric_cell_id,),
                ).fetchone()
            except sqlite3.Error:
                return failed("unavailable", "canonical_financial_schema_unavailable")
            try:
                unavailable = cell_row is None or any(
                    _clock(cell_row[index]) > self._cutoff for index in range(1, 5)
                )
            except (TypeError, ValueError):
                unavailable = True
            if unavailable or cell_row is None:
                return failed("unavailable", "exact_canonical_metric_binding_unavailable")
            cells[binding.canonical_metric_cell_id] = str(cell_row[0])

        selected: list[tuple[str, str, str, str]] = []
        for cell_id, metric_id in cells.items():
            resolution = self._resolver.as_known(cell_id, self._cutoff)
            if (
                resolution is None
                or resolution.status != "resolved"
                or resolution.selected_observation_id is None
            ):
                return failed("rejected", "canonical_financial_resolution_unavailable")
            selected.append(
                (
                    cell_id,
                    metric_id,
                    resolution.canonical_resolution_revision_id,
                    resolution.selected_observation_id,
                )
            )
        selected_ids = tuple(item[3] for item in selected)
        try:
            reads = self._reader.provenance_bundles(selected_ids, cutoff=self._cutoff)
        except (sqlite3.Error, ValueError):
            return failed("unavailable", "canonical_financial_provenance_unavailable")
        bundles = {item.observation_id: item.bundle for item in reads if item.bundle is not None}
        if len(bundles) != len(selected_ids):
            return failed("unavailable", "canonical_financial_provenance_unavailable")

        observations: list[CanonicalFinancialObservation] = []
        for cell_id, metric_id, resolution_id, observation_id in selected:
            bundle = bundles[observation_id]
            selected_binding = self._ontology.binding_as_known(observation_id, self._cutoff)
            if (
                selected_binding is None
                or selected_binding.binding_status != "bound"
                or selected_binding.canonical_metric_cell_id != cell_id
            ):
                return failed("rejected", "selected_financial_binding_unavailable")
            if (
                bundle.cell.concept_namespace != FINANCIAL_CONCEPT_NAMESPACE
                or bundle.cell.concept_name != metric
            ):
                return failed("rejected", "selected_financial_concept_mismatch")
            current_definition = self._ontology.metric_definition_as_known(metric_id, self._cutoff)
            admitted_definition = self._ontology.metric_definition_as_known(
                metric_id, selected_binding.recorded_at
            )
            if (
                current_definition is None
                or admitted_definition is None
                or current_definition.lifecycle != "active"
                or admitted_definition.lifecycle != "active"
                or _definition_semantics(current_definition)
                != _definition_semantics(admitted_definition)
                or current_definition.value_kind != "numeric"
                or current_definition.period_kind != "duration"
                or current_definition.unit_family != "currency"
                or current_definition.accounting_basis != bundle.cell.accounting_basis
                or current_definition.scope_constraints.get("reporting_entity_id")
                != bundle.cell.reporting_entity_id
                or current_definition.scope_constraints.get("consolidation_scope")
                != bundle.cell.consolidation_scope
            ):
                return failed("rejected", "active_metric_definition_unavailable_or_changed")
            item = self._project_observation(
                metric,
                cadence,
                metric_id,
                admitted_definition.metric_definition_revision_id,
                cell_id,
                resolution_id,
                bundle,
            )
            if isinstance(item, str):
                return failed("rejected", item)
            if (
                item.period_end,
                item.fiscal_year,
                item.fiscal_period,
            ) != coordinate:
                return failed("rejected", "selected_financial_coordinate_mismatch")
            observations.append(item)
        if len(observations) != 1:
            return failed("ambiguous", "duplicate_or_ambiguous_quarterly_financial_value")
        return CanonicalFinancialCoordinate(
            period_end=coordinate[0],
            fiscal_year=coordinate[1],
            fiscal_period=coordinate[2],
            status="admitted",
            candidates=candidates,
            observation=observations[0],
        )

    def _unavailable(
        self,
        metric: str,
        cadence: FinancialCadence,
        continuity: SeriesContinuity,
        reason: str,
        *,
        candidates: tuple[CanonicalFinancialCandidate, ...] = (),
    ) -> CanonicalFinancialSeries:
        return CanonicalFinancialSeries(
            ticker=self._ticker,
            metric=metric,
            cadence=cadence,
            continuity=continuity,
            cutoff=self._cutoff,
            status="unavailable",
            reason_code=reason,
            candidates=candidates,
        )

    def _read_uncached(
        self,
        metric: str,
        cadence: FinancialCadence,
        continuity: SeriesContinuity,
    ) -> CanonicalFinancialSeries:
        if not metric:
            return self._unavailable(metric, cadence, continuity, "empty_exact_financial_metric")
        try:
            source_rows = self._conn.execute(
                """
                SELECT observation.observation_id,source.fact_cell_id,
                       source.reporting_entity_id,source.period_start,source.period_end,
                       source.fiscal_year,source.fiscal_period,source.currency,source.unit_key,
                       source.accounting_basis,source.consolidation_scope,
                       source.knowledge_at,source.recorded_at,
                       observation.knowledge_at,observation.recorded_at,
                       document.recorded_at,source.scope_security_id
                FROM fact_cells_v2 AS source
                JOIN fact_observations_v2 AS observation
                  ON observation.fact_cell_id=source.fact_cell_id
                 AND observation.observation_kind='reported'
                JOIN evidence_document_versions AS document
                  ON document.document_version_id=observation.document_version_id
                WHERE upper(document.ticker)=?
                  AND source.concept_namespace=?
                  AND source.concept_name=?
                  AND source.period_kind='duration'
                ORDER BY source.period_end,observation.observation_id
                """,
                (self._ticker, FINANCIAL_CONCEPT_NAMESPACE, metric),
            ).fetchall()
        except sqlite3.Error:
            return self._unavailable(
                metric, cadence, continuity, "canonical_financial_schema_unavailable"
            )
        relevant: list[tuple[object, ...]] = []
        try:
            for row in source_rows:
                if not all(_clock(row[index]) <= self._cutoff for index in range(11, 16)):
                    continue
                if _label_matches(cadence, row[6]):
                    relevant.append(tuple(row))
        except (TypeError, ValueError):
            return self._unavailable(
                metric, cadence, continuity, "canonical_financial_clock_invalid"
            )
        if not relevant:
            return self._unavailable(
                metric, cadence, continuity, "exact_financial_concept_unavailable"
            )
        try:
            candidates = tuple(
                CanonicalFinancialCandidate(
                    observation_id=str(row[0]),
                    fact_cell_id=str(row[1]),
                    reporting_entity_id=str(row[2]),
                    scope_security_id=None if row[16] is None else str(row[16]),
                    period_start=None if row[3] is None else _clock(row[3]),
                    period_end=_clock(row[4]),
                    fiscal_year=None if row[5] is None else int(str(row[5])),
                    fiscal_period=str(row[6]),
                    currency=None if row[7] is None else str(row[7]),
                    unit=str(row[8]),
                    accounting_basis=str(row[9]),
                    consolidation_scope=str(row[10]),
                    duration_days=_duration_days(row[3], row[4]),
                    cadence_supported=_duration_matches(cadence, _duration_days(row[3], row[4])),
                )
                for row in relevant
            )
        except (TypeError, ValueError):
            return self._unavailable(
                metric, cadence, continuity, "canonical_financial_coordinate_invalid"
            )
        valid: list[tuple[object, ...]] = []
        invalid_ends: list[datetime] = []
        for row in relevant:
            days = _duration_days(row[3], row[4])
            if _duration_matches(cadence, days):
                valid.append(row)
            else:
                try:
                    invalid_ends.append(_clock(row[4]))
                except (TypeError, ValueError):
                    invalid_ends.append(datetime.max.replace(tzinfo=UTC))
        if not valid:
            return self._unavailable(
                metric,
                cadence,
                continuity,
                "unsupported_financial_cadence_or_duration",
                candidates=candidates,
            )
        if continuity is SeriesContinuity.STRICT_CONTIGUOUS and invalid_ends:
            return self._unavailable(
                metric,
                cadence,
                continuity,
                "unsupported_financial_cadence_or_duration",
                candidates=candidates,
            )
        latest_valid_end = max(_clock(row[4]) for row in valid)
        if invalid_ends and max(invalid_ends) >= latest_valid_end:
            return self._unavailable(
                metric,
                cadence,
                continuity,
                "newer_rejected_financial_observation",
                candidates=candidates,
            )
        observation_ids = tuple(str(row[0]) for row in valid)

        cells: dict[str, str] = {}
        for observation_id in observation_ids:
            binding = self._ontology.binding_as_known(observation_id, self._cutoff)
            if (
                binding is None
                or binding.binding_status != "bound"
                or binding.canonical_metric_cell_id is None
            ):
                return self._unavailable(
                    metric,
                    cadence,
                    continuity,
                    "exact_financial_concept_not_admitted",
                    candidates=candidates,
                )
            try:
                cell_row = self._conn.execute(
                    """
                    SELECT cell.metric_id,cell.knowledge_at,cell.recorded_at,
                           metric.knowledge_at,metric.recorded_at
                    FROM canonical_metric_cells AS cell
                    JOIN canonical_metrics AS metric ON metric.metric_id=cell.metric_id
                    WHERE cell.canonical_metric_cell_id=?
                    """,
                    (binding.canonical_metric_cell_id,),
                ).fetchone()
            except sqlite3.Error:
                return self._unavailable(
                    metric,
                    cadence,
                    continuity,
                    "canonical_financial_schema_unavailable",
                    candidates=candidates,
                )
            try:
                unavailable = cell_row is None or any(
                    _clock(cell_row[index]) > self._cutoff for index in range(1, 5)
                )
            except (TypeError, ValueError):
                unavailable = True
            if unavailable or cell_row is None:
                return self._unavailable(
                    metric,
                    cadence,
                    continuity,
                    "exact_canonical_metric_binding_unavailable",
                    candidates=candidates,
                )
            cells[binding.canonical_metric_cell_id] = str(cell_row[0])

        selected: list[tuple[str, str, str, str]] = []
        for cell_id, metric_id in cells.items():
            resolution = self._resolver.as_known(cell_id, self._cutoff)
            if (
                resolution is None
                or resolution.status != "resolved"
                or resolution.selected_observation_id is None
            ):
                return self._unavailable(
                    metric,
                    cadence,
                    continuity,
                    "canonical_financial_resolution_unavailable",
                    candidates=candidates,
                )
            selected.append(
                (
                    cell_id,
                    metric_id,
                    resolution.canonical_resolution_revision_id,
                    resolution.selected_observation_id,
                )
            )
        selected_ids = tuple(item[3] for item in selected)
        try:
            reads = self._reader.provenance_bundles(selected_ids, cutoff=self._cutoff)
        except (sqlite3.Error, ValueError):
            return self._unavailable(
                metric,
                cadence,
                continuity,
                "canonical_financial_provenance_unavailable",
                candidates=candidates,
            )
        bundles = {item.observation_id: item.bundle for item in reads if item.bundle is not None}
        if len(bundles) != len(selected_ids):
            return self._unavailable(
                metric,
                cadence,
                continuity,
                "canonical_financial_provenance_unavailable",
                candidates=candidates,
            )

        observations: list[CanonicalFinancialObservation] = []
        admitted_definition_ids: set[str] = set()
        for (cell_id, metric_id, resolution_id, _), observation_id in zip(
            selected, selected_ids, strict=True
        ):
            bundle = bundles[observation_id]
            selected_binding = self._ontology.binding_as_known(observation_id, self._cutoff)
            if (
                selected_binding is None
                or selected_binding.binding_status != "bound"
                or selected_binding.canonical_metric_cell_id != cell_id
            ):
                return self._unavailable(
                    metric,
                    cadence,
                    continuity,
                    "selected_financial_binding_unavailable",
                    candidates=candidates,
                )
            if (
                bundle.cell.concept_namespace != FINANCIAL_CONCEPT_NAMESPACE
                or bundle.cell.concept_name != metric
            ):
                return self._unavailable(
                    metric,
                    cadence,
                    continuity,
                    "selected_financial_concept_mismatch",
                    candidates=candidates,
                )
            current_definition = self._ontology.metric_definition_as_known(metric_id, self._cutoff)
            admitted_definition = self._ontology.metric_definition_as_known(
                metric_id, selected_binding.recorded_at
            )
            if (
                current_definition is None
                or admitted_definition is None
                or current_definition.lifecycle != "active"
                or admitted_definition.lifecycle != "active"
                or _definition_semantics(current_definition)
                != _definition_semantics(admitted_definition)
                or current_definition.value_kind != "numeric"
                or current_definition.period_kind != "duration"
                or current_definition.unit_family != "currency"
                or current_definition.accounting_basis != bundle.cell.accounting_basis
                or current_definition.scope_constraints.get("reporting_entity_id")
                != bundle.cell.reporting_entity_id
                or current_definition.scope_constraints.get("consolidation_scope")
                != bundle.cell.consolidation_scope
            ):
                return self._unavailable(
                    metric,
                    cadence,
                    continuity,
                    "active_metric_definition_unavailable_or_changed",
                    candidates=candidates,
                )
            admitted_definition_ids.add(admitted_definition.metric_definition_revision_id)
            item = self._project_observation(
                metric,
                cadence,
                metric_id,
                admitted_definition.metric_definition_revision_id,
                cell_id,
                resolution_id,
                bundle,
            )
            if isinstance(item, str):
                return self._unavailable(metric, cadence, continuity, item, candidates=candidates)
            if item.period_end > self._cutoff:
                return self._unavailable(
                    metric,
                    cadence,
                    continuity,
                    "future_financial_period",
                    candidates=candidates,
                )
            observations.append(item)
        if len(admitted_definition_ids) != 1:
            return self._unavailable(
                metric,
                cadence,
                continuity,
                "mixed_metric_definition_revision",
                candidates=candidates,
            )
        reason = self._validate_series(observations, cadence, continuity)
        if reason is not None:
            return self._unavailable(metric, cadence, continuity, reason, candidates=candidates)
        return CanonicalFinancialSeries(
            ticker=self._ticker,
            metric=metric,
            cadence=cadence,
            continuity=continuity,
            cutoff=self._cutoff,
            status="available",
            candidates=candidates,
            observations=tuple(sorted(observations, key=lambda item: item.period_end)),
        )

    @staticmethod
    def _project_observation(
        metric: str,
        cadence: FinancialCadence,
        metric_id: str,
        definition_id: str,
        cell_id: str,
        resolution_id: str,
        bundle: ProvenanceBundle,
    ) -> CanonicalFinancialObservation | str:
        value = bundle.observation
        cell = bundle.cell
        fiscal_period = cell.fiscal_period
        if (
            value.observation_kind != "reported"
            or value.value_kind != "numeric"
            or value.decimal_value is None
            or value.period_kind != "duration"
            or value.period_start is None
            or value.currency is None
            or bundle.evidence is None
            or cell.fiscal_year is None
            or fiscal_period is None
            or not _label_matches(cadence, fiscal_period)
            or not _duration_matches(cadence, _duration_days(value.period_start, value.period_end))
        ):
            return "incomplete_canonical_financial_observation"
        source_locator: dict[str, object] = {
            key: locator_value for key, locator_value in bundle.evidence.source_locator.root.items()
        }
        return CanonicalFinancialObservation(
            metric=metric,
            cadence=cadence,
            metric_id=metric_id,
            metric_definition_revision_id=definition_id,
            canonical_metric_cell_id=cell_id,
            canonical_resolution_revision_id=resolution_id,
            observation_id=value.observation_id,
            observation_payload_sha256=value.observation_payload_sha256,
            document_version_id=bundle.evidence.document_version_id,
            source_locator=source_locator,
            reporting_entity_id=cell.reporting_entity_id,
            scope_security_id=cell.scope_security_id,
            period_start=value.period_start,
            period_end=value.period_end,
            fiscal_year=cell.fiscal_year,
            fiscal_period=cast(str, fiscal_period),
            currency=value.currency,
            unit=value.unit_key,
            accounting_basis=cell.accounting_basis,
            consolidation_scope=cell.consolidation_scope,
            dimensions=cell.dimensions,
            value=value.decimal_value,
        )

    @staticmethod
    def _validate_series(
        observations: list[CanonicalFinancialObservation],
        cadence: FinancialCadence,
        continuity: SeriesContinuity,
    ) -> str | None:
        ordered = sorted(observations, key=lambda item: item.period_end)
        if len({(item.period_start, item.period_end) for item in ordered}) != len(ordered):
            return (
                "duplicate_or_ambiguous_quarterly_financial_value"
                if cadence is FinancialCadence.QUARTERLY
                else "duplicate_or_ambiguous_financial_value"
            )
        if (
            len(
                {
                    (
                        item.metric_id,
                        item.metric_definition_revision_id,
                        item.reporting_entity_id,
                        item.scope_security_id,
                        item.currency,
                        item.unit,
                        item.accounting_basis,
                        item.consolidation_scope,
                        tuple(
                            json.dumps(
                                dimension.canonical_member,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            for dimension in item.dimensions
                        ),
                    )
                    for item in ordered
                }
            )
            != 1
        ):
            return "incomparable_financial_series_coordinate"
        if continuity is SeriesContinuity.WINDOWED or cadence is FinancialCadence.REPORTED_TTM:
            return None
        labels = ("Q1", "Q2", "Q3", "Q4") if cadence is FinancialCadence.QUARTERLY else ("Q2", "Q4")
        for older, newer in pairwise(ordered):
            if newer.period_start.date() != older.period_end.date() + timedelta(days=1):
                return (
                    "quarterly_duration_gap_or_overlap"
                    if cadence is FinancialCadence.QUARTERLY
                    else "financial_duration_gap_or_overlap"
                )
            index = labels.index(older.fiscal_period)
            expected_label = labels[(index + 1) % len(labels)]
            expected_year = older.fiscal_year + (1 if index == len(labels) - 1 else 0)
            if newer.fiscal_period != expected_label:
                return (
                    "quarterly_fiscal_cadence_mismatch"
                    if cadence is FinancialCadence.QUARTERLY
                    else "financial_fiscal_cadence_mismatch"
                )
            if newer.fiscal_year != expected_year:
                return (
                    "quarterly_fiscal_year_mismatch"
                    if cadence is FinancialCadence.QUARTERLY
                    else "financial_fiscal_year_mismatch"
                )
        return None
