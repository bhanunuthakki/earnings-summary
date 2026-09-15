"""Governed, read-only DCF forecast series.

DCF forecasts are model output, never a substitute for reported facts.  This
module deliberately accepts only an already-resolved canonical metric
coordinate and an admitted, versioned mapping.  It has no workbook or ViewSpec
dependency and performs no writes outside ``persist.upsert``'s transaction.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal, cast

from dcf.latest import latest_dcf_row

AdmissionStatus = Literal["admitted", "quarantined", "retired"]
Confidence = Literal["high", "medium", "low"]
OverlayReason = Literal[
    "schema_unavailable",
    "no_current_dcf_run",
    "current_run_rejected",
    "no_compatible_mapping",
    "no_persisted_points",
]


def _required_text(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


@dataclass(frozen=True, slots=True)
class ForecastSemanticCoordinate:
    """Exact canonical identity required before a forecast may be overlaid."""

    canonical_metric_definition_revision_id: str
    period_kind: Literal["duration"]
    unit_family: str
    value_scale: Literal["ones", "thousands", "millions", "billions"]
    currency: str
    accounting_basis: str
    consolidation_scope: str
    dimensions_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "canonical_metric_definition_revision_id",
            _required_text(
                self.canonical_metric_definition_revision_id,
                "canonical_metric_definition_revision_id",
            ),
        )
        object.__setattr__(self, "unit_family", _required_text(self.unit_family, "unit_family"))
        object.__setattr__(self, "currency", _required_text(self.currency, "currency"))
        object.__setattr__(
            self,
            "accounting_basis",
            _required_text(self.accounting_basis, "accounting_basis"),
        )
        object.__setattr__(
            self,
            "consolidation_scope",
            _required_text(self.consolidation_scope, "consolidation_scope"),
        )
        if len(self.dimensions_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.dimensions_sha256
        ):
            raise ValueError("dimensions_sha256 must be a lowercase SHA-256 digest")
        if self.value_scale not in {"ones", "thousands", "millions", "billions"}:
            raise ValueError("value_scale must be a supported numeric scale")


@dataclass(frozen=True, slots=True)
class ForecastMetricMapping:
    """A typed mapping record for DCF-output admission workflows.

    The write workflow is intentionally separate from the read adapter.  A
    caller must supply a stable model series key and a canonical definition
    revision; labels and substring matching are not represented here.
    """

    ticker: str
    engine_family: str
    engine_version: str
    series_key: str
    viewspec_metric_token: str
    coordinate: ForecastSemanticCoordinate
    admission_status: AdmissionStatus
    confidence: Confidence
    revision: int
    reviewer_identity: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", _required_text(self.ticker, "ticker").upper())
        object.__setattr__(
            self, "engine_family", _required_text(self.engine_family, "engine_family")
        )
        object.__setattr__(
            self, "engine_version", _required_text(self.engine_version, "engine_version")
        )
        object.__setattr__(self, "series_key", _required_text(self.series_key, "series_key"))
        object.__setattr__(
            self,
            "viewspec_metric_token",
            _required_text(self.viewspec_metric_token, "viewspec_metric_token"),
        )
        if self.revision < 1:
            raise ValueError("revision must be positive")
        if self.admission_status == "admitted" and self.confidence != "high":
            raise ValueError("admitted forecast mappings require high confidence")
        if self.admission_status == "admitted" and (
            self.reviewer_identity is None or not self.reviewer_identity.strip()
        ):
            raise ValueError("admitted forecast mappings require a reviewer identity")


@dataclass(frozen=True, slots=True)
class ForecastSeriesPoint:
    """One modelled annual duration point prepared for atomic DCF persistence."""

    mapping_revision_id: int
    series_key: str
    period_start: date
    period_end: date
    value: float

    def __post_init__(self) -> None:
        if self.mapping_revision_id < 1:
            raise ValueError("mapping_revision_id must be positive")
        object.__setattr__(self, "series_key", _required_text(self.series_key, "series_key"))
        try:
            expected_start = date(
                self.period_end.year - 1, self.period_end.month, self.period_end.day
            ) + timedelta(days=1)
        except ValueError as exc:
            raise ValueError("forecast period_end cannot define an annual fiscal period") from exc
        if self.period_start != expected_start:
            raise ValueError("forecast points must cover one exact annual fiscal period")
        if not math.isfinite(self.value):
            raise ValueError("forecast value must be finite")


@dataclass(frozen=True, slots=True)
class ForecastOverlayPoint:
    period_start: date
    period_end: date
    value: float


@dataclass(frozen=True, slots=True)
class ForecastOverlay:
    """Read-only model output with the mapping/run lineage required by a UI."""

    ticker: str
    dcf_run_id: int
    mapping_revision_id: int
    series_key: str
    points: tuple[ForecastOverlayPoint, ...]


@dataclass(frozen=True, slots=True)
class ForecastOverlayLoad:
    overlay: ForecastOverlay | None
    reason: OverlayReason | None


def _has_forecast_plane(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('dcf_forecast_metric_mapping_revisions', 'dcf_forecast_series_points')"
    ).fetchall()
    return {str(row[0]) for row in rows} == {
        "dcf_forecast_metric_mapping_revisions",
        "dcf_forecast_series_points",
    }


def _mapping_commitments_are_valid(
    dimensions_json: object,
    dimensions_sha256: object,
    evidence_json: object,
    evidence_sha256: object,
    reviewer_identity: object,
) -> bool:
    """Verify immutable admission evidence before any mapping is trusted."""
    if not all(
        isinstance(value, str)
        for value in (
            dimensions_json,
            dimensions_sha256,
            evidence_json,
            evidence_sha256,
            reviewer_identity,
        )
    ):
        return False
    dimensions_text = str(dimensions_json)
    evidence_text = str(evidence_json)
    reviewer = str(reviewer_identity)
    if not reviewer.strip():
        return False
    try:
        dimensions = json.loads(dimensions_text)
        evidence = json.loads(evidence_text)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(dimensions, list) or not isinstance(evidence, dict):
        return False
    basis = cast("dict[str, object]", evidence).get("basis")
    if not isinstance(basis, str) or not basis.strip():
        return False
    expected_dimensions = hashlib.sha256(dimensions_text.encode("utf-8")).hexdigest()
    expected_evidence = hashlib.sha256(evidence_text.encode("utf-8")).hexdigest()
    return hmac.compare_digest(str(dimensions_sha256), expected_dimensions) and hmac.compare_digest(
        str(evidence_sha256), expected_evidence
    )


def current_admitted_mapping_id(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    engine_family: str,
    engine_version: str,
    series_key: str,
    unit_family: str,
    currency: str,
    value_scale: Literal["ones", "thousands", "millions", "billions"],
) -> int | None:
    """Return one current admitted mapping, or ``None`` on any ambiguity.

    Forecast producers use this instead of selecting a convenient mapping.  A
    mapping is only current within its immutable series-revision lineage.
    """
    try:
        rows = conn.execute(
            """
            SELECT mapping.id, mapping.dimensions_json, mapping.dimensions_sha256,
                   mapping.evidence_json, mapping.evidence_sha256, mapping.reviewer_identity
            FROM dcf_forecast_metric_mapping_revisions AS mapping
            JOIN canonical_metric_definition_revisions AS definition
              ON definition.metric_definition_revision_id =
                 mapping.canonical_metric_definition_revision_id
            WHERE mapping.ticker = ?
              AND mapping.engine_family = ?
              AND mapping.engine_version = ?
              AND mapping.series_key = ?
              AND mapping.unit_family = ?
              AND mapping.currency = ?
              AND mapping.value_scale = ?
              AND mapping.admission_status = 'admitted'
              AND mapping.confidence = 'high'
              AND mapping.reviewer_identity IS NOT NULL
              AND length(trim(mapping.reviewer_identity)) > 0
              AND definition.lifecycle = 'active'
              AND definition.period_kind = mapping.period_kind
              AND definition.unit_family = mapping.unit_family
              AND definition.accounting_basis = mapping.accounting_basis
              AND NOT EXISTS (
                  SELECT 1 FROM canonical_metric_definition_revisions AS newer_definition
                  WHERE newer_definition.metric_id = definition.metric_id
                    AND newer_definition.revision > definition.revision
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM dcf_forecast_metric_mapping_revisions AS newer
                  WHERE newer.ticker = mapping.ticker
                    AND newer.engine_family = mapping.engine_family
                    AND newer.engine_version = mapping.engine_version
                    AND newer.series_key = mapping.series_key
                    AND newer.revision > mapping.revision
              )
            """,
            (
                ticker.upper(),
                engine_family,
                engine_version,
                series_key,
                unit_family,
                currency,
                value_scale,
            ),
        ).fetchall()
    except sqlite3.Error:
        return None
    if len(rows) != 1 or not _mapping_commitments_are_valid(*rows[0][1:6]):
        return None
    mapping_id = rows[0][0]
    return mapping_id if isinstance(mapping_id, int) else None


def _display_unit_matches(coordinate: ForecastSemanticCoordinate, actual_unit: str | None) -> bool:
    """Require the rendered reported-series unit to match without conversion."""
    if actual_unit is None:
        return False
    pieces = actual_unit.strip().split()
    expected_scale = "actual" if coordinate.value_scale == "ones" else coordinate.value_scale
    if len(pieces) == 1:
        only = pieces[0]
        if only.lower() == expected_scale:
            return True
        return coordinate.value_scale == "ones" and only.upper() == coordinate.currency
    if len(pieces) != 2:
        return False
    return pieces[0].upper() == coordinate.currency and pieces[1].lower() == expected_scale


def _overlay_from_rows(
    rows: list[sqlite3.Row] | list[tuple[object, ...]], *, ticker: str, dcf_run_id: int
) -> ForecastOverlayLoad:
    if not rows:
        return ForecastOverlayLoad(None, "no_compatible_mapping")
    try:
        mapping_ids: set[int] = set()
        series_keys: set[str] = set()
        for row in rows:
            if len(row) < 10 or not _mapping_commitments_are_valid(*row[5:10]):
                return ForecastOverlayLoad(None, "no_compatible_mapping")
            mapping_id = row[3]
            series_key = row[4]
            if not isinstance(mapping_id, int) or not isinstance(series_key, str):
                return ForecastOverlayLoad(None, "no_compatible_mapping")
            mapping_ids.add(mapping_id)
            series_keys.add(series_key)
        if len(mapping_ids) != 1 or len(series_keys) != 1:
            return ForecastOverlayLoad(None, "no_compatible_mapping")
        points_list: list[ForecastOverlayPoint] = []
        for row in rows:
            value = row[2]
            if not isinstance(value, int | float):
                return ForecastOverlayLoad(None, "no_persisted_points")
            points_list.append(
                ForecastOverlayPoint(
                    period_start=date.fromisoformat(str(row[0])),
                    period_end=date.fromisoformat(str(row[1])),
                    value=float(value),
                )
            )
        points = tuple(points_list)
    except (TypeError, ValueError):
        return ForecastOverlayLoad(None, "no_persisted_points")
    if any(not math.isfinite(point.value) for point in points):
        return ForecastOverlayLoad(None, "no_persisted_points")
    return ForecastOverlayLoad(
        ForecastOverlay(
            ticker=ticker,
            dcf_run_id=dcf_run_id,
            mapping_revision_id=next(iter(mapping_ids)),
            series_key=next(iter(series_keys)),
            points=points,
        ),
        None,
    )


def load_forecast_overlay(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    coordinate: ForecastSemanticCoordinate,
) -> ForecastOverlayLoad:
    """Return only an exact, currently-admitted forecast overlay.

    Missing tables, runs, mappings, or points deliberately look identical to a
    missing overlay to consumers.  Callers can retain ``reason`` for receipts,
    but must never fall back to labels, legacy keys, or heuristic matching.
    """
    try:
        if not _has_forecast_plane(conn):
            return ForecastOverlayLoad(None, "schema_unavailable")
        current = latest_dcf_row(conn, ticker)
        if current is None:
            return ForecastOverlayLoad(None, "no_current_dcf_run")
        if current.sanity_flag == "outlier":
            return ForecastOverlayLoad(None, "current_run_rejected")
        rows = conn.execute(
            """
            SELECT point.period_start, point.period_end, point.value,
                   point.mapping_revision_id, point.series_key,
                   mapping.dimensions_json, mapping.dimensions_sha256,
                   mapping.evidence_json, mapping.evidence_sha256,
                   mapping.reviewer_identity
            FROM dcf_forecast_series_points AS point
            JOIN dcf_forecast_metric_mapping_revisions AS mapping
              ON mapping.id = point.mapping_revision_id
            JOIN canonical_metric_definition_revisions AS definition
              ON definition.metric_definition_revision_id =
                 mapping.canonical_metric_definition_revision_id
            WHERE point.dcf_run_id = ?
              AND mapping.ticker = ?
              AND mapping.engine_version = (
                  SELECT run.engine_version FROM dcf_runs AS run WHERE run.id = point.dcf_run_id
              )
              AND mapping.admission_status = 'admitted'
              AND mapping.confidence = 'high'
              AND mapping.reviewer_identity IS NOT NULL
              AND length(trim(mapping.reviewer_identity)) > 0
              AND mapping.canonical_metric_definition_revision_id = ?
              AND mapping.period_kind = ?
              AND mapping.unit_family = ?
              AND mapping.value_scale = ?
              AND mapping.currency = ?
              AND mapping.accounting_basis = ?
              AND mapping.consolidation_scope = ?
              AND mapping.dimensions_sha256 = ?
              AND definition.lifecycle = 'active'
              AND definition.period_kind = mapping.period_kind
              AND definition.unit_family = mapping.unit_family
              AND definition.accounting_basis = mapping.accounting_basis
              AND NOT EXISTS (
                  SELECT 1 FROM canonical_metric_definition_revisions AS newer_definition
                  WHERE newer_definition.metric_id = definition.metric_id
                    AND newer_definition.revision > definition.revision
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM dcf_forecast_metric_mapping_revisions AS newer
                  WHERE newer.engine_family = mapping.engine_family
                    AND newer.engine_version = mapping.engine_version
                    AND newer.ticker = mapping.ticker
                    AND newer.series_key = mapping.series_key
                    AND newer.revision > mapping.revision
              )
            ORDER BY point.period_end, point.id
            """,
            (
                current.id,
                current.ticker,
                coordinate.canonical_metric_definition_revision_id,
                coordinate.period_kind,
                coordinate.unit_family,
                coordinate.value_scale,
                coordinate.currency,
                coordinate.accounting_basis,
                coordinate.consolidation_scope,
                coordinate.dimensions_sha256,
            ),
        ).fetchall()
    except sqlite3.Error:
        return ForecastOverlayLoad(None, "schema_unavailable")
    return _overlay_from_rows(rows, ticker=current.ticker, dcf_run_id=current.id)


def load_forecast_overlay_for_metric(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    viewspec_metric_token: str,
    coordinate: ForecastSemanticCoordinate,
    actual_unit: str | None,
) -> ForecastOverlayLoad:
    """Resolve an overlay through an admitted semantic ViewSpec crosswalk.

    The token is an adapter locator recorded on a reviewed mapping revision;
    it is never compared with a DCF series label or guessed by substring.
    """
    if not _display_unit_matches(coordinate, actual_unit):
        return ForecastOverlayLoad(None, "no_compatible_mapping")
    try:
        if not _has_forecast_plane(conn):
            return ForecastOverlayLoad(None, "schema_unavailable")
        current = latest_dcf_row(conn, ticker)
        if current is None:
            return ForecastOverlayLoad(None, "no_current_dcf_run")
        if current.sanity_flag == "outlier":
            return ForecastOverlayLoad(None, "current_run_rejected")
        rows = conn.execute(
            """
            SELECT point.period_start, point.period_end, point.value,
                   point.mapping_revision_id, point.series_key,
                   mapping.dimensions_json, mapping.dimensions_sha256,
                   mapping.evidence_json, mapping.evidence_sha256,
                   mapping.reviewer_identity
            FROM dcf_forecast_series_points AS point
            JOIN dcf_forecast_metric_mapping_revisions AS mapping
              ON mapping.id = point.mapping_revision_id
            JOIN dcf_runs AS run ON run.id = point.dcf_run_id
            JOIN canonical_metric_definition_revisions AS definition
              ON definition.metric_definition_revision_id =
                 mapping.canonical_metric_definition_revision_id
            WHERE point.dcf_run_id = ?
              AND mapping.ticker = ?
              AND mapping.engine_version = run.engine_version
              AND mapping.viewspec_metric_token = ?
              AND mapping.canonical_metric_definition_revision_id = ?
              AND mapping.period_kind = ?
              AND mapping.unit_family = ?
              AND mapping.value_scale = ?
              AND mapping.currency = ?
              AND mapping.accounting_basis = ?
              AND mapping.consolidation_scope = ?
              AND mapping.dimensions_sha256 = ?
              AND mapping.admission_status = 'admitted'
              AND mapping.confidence = 'high'
              AND mapping.reviewer_identity IS NOT NULL
              AND length(trim(mapping.reviewer_identity)) > 0
              AND definition.lifecycle = 'active'
              AND definition.period_kind = mapping.period_kind
              AND definition.unit_family = mapping.unit_family
              AND definition.accounting_basis = mapping.accounting_basis
              AND NOT EXISTS (
                  SELECT 1 FROM canonical_metric_definition_revisions AS newer_definition
                  WHERE newer_definition.metric_id = definition.metric_id
                    AND newer_definition.revision > definition.revision
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM dcf_forecast_metric_mapping_revisions AS newer
                  WHERE newer.ticker = mapping.ticker
                    AND newer.engine_family = mapping.engine_family
                    AND newer.engine_version = mapping.engine_version
                    AND newer.series_key = mapping.series_key
                    AND newer.revision > mapping.revision
              )
            ORDER BY point.period_end, point.id
            """,
            (
                current.id,
                current.ticker,
                viewspec_metric_token,
                coordinate.canonical_metric_definition_revision_id,
                coordinate.period_kind,
                coordinate.unit_family,
                coordinate.value_scale,
                coordinate.currency,
                coordinate.accounting_basis,
                coordinate.consolidation_scope,
                coordinate.dimensions_sha256,
            ),
        ).fetchall()
    except sqlite3.Error:
        return ForecastOverlayLoad(None, "schema_unavailable")
    return _overlay_from_rows(rows, ticker=current.ticker, dcf_run_id=current.id)
