"""Execute a ViewSpec against the fact tables (master build P5.1).

Deterministic and LLM-free: every cell comes from the canonical tier-aware
loaders in ``timeseries.loaders`` (the same row picks the reports use) and
every admitted fact cell carries the provenance of its winning fact row or
period, so the renderer can chip each number. Derived cells retain every
current/prior/base/denominator source coordinate used in the calculation.

Cross-ticker alignment is by CALENDAR bucket: a quarterly view buckets
each observation into (calendar year, calendar quarter) derived from its
fiscal period_end, so an offset fiscal calendar (AAPL's December "Q1")
lands in the calendar quarter it actually ended in. That is the honest
axis for cross-ticker comparison; the bucket label ("Q4'25") therefore may
differ from the issuer's own fiscal quarter name. Annual views bucket FY
rows by period_end year.

Transforms (the spec's vocabulary):
  level  — the raw value
  yoy    — % change vs the same bucket one year earlier
  cagr   — trailing ``spec.cagr_years``-year CAGR ending at each bucket
  margin — value / fin:revenue (same ticker, same bucket), in %
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast
from urllib.parse import unquote

from compute.kpi_resolver import (
    kpi_group_key,
    semantic_series_identity_anchor_sql,
    semantic_series_identity_flat_sql,
    semantic_series_identity_sql,
)
from dcf.forecast_series import (
    ForecastOverlay,
    ForecastSemanticCoordinate,
    load_forecast_overlay_for_metric,
)
from pipeline.kpi_semantics import semantic_admission_sql
from provenance.financial_fact_resolution import canonical_fact_relation
from provenance.overrides import FactOverride, get_active_overrides, override_provenance
from report.models import CellSource
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from timeseries.loaders import (
    SourcedObservation,
    load_financial_series_with_provenance,
    load_kpi_series_with_provenance,
    load_segment_junction_series_with_provenance,
)
from viewspec.spec import MetricRef, ViewSpec

log = logging.getLogger(__name__)

# (TICKER, fact_kind, fact_key) -> active replace overrides for that scalar
# fact. Read once per view so override-only injection is an in-memory lookup.
_ScalarOverrideMap = dict[tuple[str, str, str], list[FactOverride]]

# TICKER -> {kpi_group_key -> that ticker's richest stored KPI name for the
# group}. Lets a de-fragmented representative token (metric_catalog groups KPI
# surface variants into one) resolve back to each ticker's own variant. Read
# once per view.
_KpiResolution = dict[str, dict[str, str]]


class _IncompatibleDetailSeriesError(ValueError):
    """A legacy detail row cannot be represented under one honest unit."""


# The DIY picker reads this catalog live per selected ticker; with the
# "capture every reported number" program the long tail can run to many
# hundreds of facts per name. The picker UI carries a type-ahead search
# (explore_panel.py), so a large list is usable — this bound is a safety
# ceiling against a pathological scan, NOT a UX cap. Kept generous so no
# extracted fact is silently truncated out of the picker.
_CATALOG_LIMIT_PER_DOMAIN = 2000

# MetricRef.domain -> fact_overrides.fact_kind, for the scalar read paths that
# surface override-only facts (a company-doc figure FMP never carried).
_DOMAIN_TO_OVERRIDE_KIND: dict[str, str] = {"fin": "financial_fact"}
_SCALAR_OVERRIDE_KINDS = frozenset(_DOMAIN_TO_OVERRIDE_KIND.values())

_QUARTERLY_PERIOD_TYPES: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4")
_ANNUAL_PERIOD_TYPES: tuple[str, ...] = ("FY",)

# (calendar year, quarter 1..4) for quarterly views; (year, 0) for annual —
# one orderable key shape either way.
_Bucket = tuple[int, int]


@dataclass(slots=True)
class ViewCell:
    """One rendered cell: the transformed value, the underlying level, and
    the provenance of the current level's winning row. ``sources`` retains
    every current/prior/base/denominator source used by a derived value."""

    value: float | None
    raw: float | None
    source: CellSource | None
    sources: tuple[CellSource, ...] = ()
    forecast_coordinate: ForecastSemanticCoordinate | None = None


def _cell_sources(cell: ViewCell | None) -> tuple[CellSource, ...]:
    if cell is None:
        return ()
    if cell.sources:
        return cell.sources
    return (cell.source,) if cell.source is not None else ()


def _forecast_coordinate_from_historical_cells(
    cells: dict[_Bucket, ViewCell],
) -> ForecastSemanticCoordinate | None:
    """Return an exact historical semantic coordinate, never infer one.

    Legacy financial/KPI/segment loaders retain source chips but not canonical
    fact coordinates, so they deliberately return no overlay.  A canonical
    projection-backed loader may attach one coordinate to every displayed
    historical cell; mismatches and partial lineage remain suppressed.
    """
    if not cells:
        return None
    coordinates = {cell.forecast_coordinate for cell in cells.values()}
    return next(iter(coordinates)) if len(coordinates) == 1 else None


def _display_value_scale(unit: str | None) -> str | None:
    if not unit:
        return None
    pieces = {piece.lower() for piece in unit.strip().split()}
    for scale in ("billions", "millions", "thousands"):
        if scale in pieces:
            return scale
    if "actual" in pieces or any(len(piece) == 3 and piece.isalpha() for piece in pieces):
        return "ones"
    return None


def canonical_coordinate_from_historical_cells(
    conn: sqlite3.Connection,
    cells: dict[_Bucket, ViewCell],
    *,
    unit: str | None,
) -> ForecastSemanticCoordinate | None:
    """Resolve one exact current canonical coordinate for all displayed cells.

    The bridge is intentionally narrow: every cell must identify an admitted
    legacy financial/KPI fact that is also the selected observation in the
    latest-governed projection. Any missing, ambiguous, or changed coordinate
    suppresses the DCF overlay.
    """
    explicit = _forecast_coordinate_from_historical_cells(cells)
    if explicit is not None:
        return explicit
    value_scale = _display_value_scale(unit)
    if value_scale is None or not cells:
        return None
    coordinates: set[tuple[str, str, str, str, str, str, str]] = set()
    for cell in cells.values():
        source = cell.source
        if (
            source is None
            or source.fact_table not in {"financial_facts", "kpi_facts"}
            or source.fact_id is None
        ):
            return None
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT projection.metric_definition_revision_id,
                       fact_cell.period_kind, definition.unit_family,
                       COALESCE(fact_cell.currency, ''),
                       fact_cell.accounting_basis,
                       fact_cell.consolidation_scope,
                       fact_cell.canonical_dimensions_sha256
                FROM v_fact_observation_match_proofs_current_valid AS proof
                JOIN fact_observations_v2 AS observation
                  ON observation.legacy_match_revision_id=proof.match_revision_id
                JOIN fact_cells_v2 AS fact_cell
                  ON fact_cell.fact_cell_id=observation.fact_cell_id
                JOIN latest_governed_fact_entries AS latest
                  ON latest.selected_observation_id=observation.observation_id
                JOIN canonical_fact_projection_entries AS projection
                  ON projection.generation_id=latest.fact_generation_id
                 AND projection.canonical_metric_cell_id=latest.canonical_metric_cell_id
                 AND projection.selected_observation_id=latest.selected_observation_id
                 AND projection.change_kind='upsert'
                JOIN canonical_metric_definition_revisions AS definition
                  ON definition.metric_definition_revision_id=
                     projection.metric_definition_revision_id
                 AND definition.lifecycle='active'
                WHERE proof.fact_table=? AND proof.fact_row_id=?
                  AND NOT EXISTS (
                    SELECT 1 FROM canonical_metric_definition_revisions AS newer
                    WHERE newer.metric_id=definition.metric_id
                      AND newer.revision>definition.revision
                  )
                """,
                (source.fact_table, source.fact_id),
            ).fetchall()
        except sqlite3.Error:
            return None
        if len(rows) != 1:
            return None
        row = rows[0]
        coordinates.add(
            (
                str(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]),
                str(row[6]),
            )
        )
    if len(coordinates) != 1:
        return None
    definition_id, period_kind, unit_family, currency, basis, scope, dimensions = next(
        iter(coordinates)
    )
    if period_kind != "duration" or not currency:
        return None
    try:
        return ForecastSemanticCoordinate(
            canonical_metric_definition_revision_id=definition_id,
            period_kind="duration",
            unit_family=unit_family,
            value_scale=cast(Literal["ones", "thousands", "millions", "billions"], value_scale),
            currency=currency,
            accounting_basis=basis,
            consolidation_scope=scope,
            dimensions_sha256=dimensions,
        )
    except ValueError:
        return None


def _dedupe_sources(*groups: tuple[CellSource, ...]) -> tuple[CellSource, ...]:
    seen: set[tuple[object, ...]] = set()
    out: list[CellSource] = []
    for source in (item for group in groups for item in group):
        key = (
            source.fact_table,
            source.fact_id,
            source.doc_id,
            source.source_url,
            source.locator,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(source)
    return tuple(out)


@dataclass(slots=True)
class ViewRow:
    """One pivot row — a (ticker, metric) series across the period axis."""

    ticker: str
    metric: MetricRef
    label: str
    unit: str | None
    cells: list[ViewCell]


@dataclass(slots=True)
class ViewForecastRow:
    """One admitted, immutable DCF output aligned to the view's period axis."""

    ticker: str
    metric: MetricRef
    label: str
    unit: str | None
    values: list[float | None]
    dcf_run_id: int
    mapping_revision_id: int


@dataclass(slots=True)
class ViewResult:
    """What `execute_view` hands the renderer."""

    spec: ViewSpec
    period_labels: list[str]
    rows: list[ViewRow]
    warnings: list[str]
    # {metric token: one-line definition} — row-label tooltips (Ask v4).
    # Populated by execute_view from viewspec.glossary; default keeps every
    # hand-constructed ViewResult (tests, embeds) valid.
    definitions: dict[str, str] = field(default_factory=dict[str, str])
    forecast_rows: list[ViewForecastRow] = field(default_factory=list[ViewForecastRow])


def _bucket_label(bucket: _Bucket, cadence: str) -> str:
    year, q = bucket
    if cadence == "annual" or q == 0:
        return f"FY{year}"
    return f"Q{q}'{str(year)[2:]}"


def _display_period_label(spec: ViewSpec, bucket: _Bucket) -> str:
    if spec.cadence == "quarterly" and all(
        metric.domain == "detail" and metric.dim_type == "customer" for metric in spec.metrics
    ):
        year, quarter = bucket
        return f"FQ{quarter} FY{year}"
    return _bucket_label(bucket, spec.cadence)


def _cell_source(prov: dict[str, object]) -> CellSource:
    """SourcedObservation.provenance → the chip model. ``source_doc_id`` is
    documents.id, which is exactly what CellSource.doc_id deep-links."""

    def _s(key: str) -> str | None:
        v = prov.get(key)
        return str(v) if v is not None else None

    doc_raw = prov.get("source_doc_id")
    fact_raw = prov.get("fact_id")
    conf_raw = prov.get("confidence")
    fact_table_raw = prov.get("fact_table")
    return CellSource(
        source=str(prov.get("source") or "unknown"),
        fetched_at=_s("fetched_at"),
        source_url=_s("source_url"),
        doc_type=_s("doc_type"),
        accession_number=_s("accession_number"),
        filing_date=_s("filing_date"),
        locator=_s("locator"),
        doc_id=doc_raw if isinstance(doc_raw, int) else None,
        fact_id=fact_raw if isinstance(fact_raw, int) else None,
        fact_table=(
            fact_table_raw
            if isinstance(fact_table_raw, str) and fact_table_raw
            else "financial_facts"
        ),
        confidence=float(conf_raw) if isinstance(conf_raw, (int, float)) else None,
        extracted_by=_s("extracted_by"),
        computed_from=_s("computed_from"),
    )


def _load_row_data(
    ticker: str,
    metric: MetricRef,
    cadence: str,
    *,
    db_path: Path | None,
    repo_root: Path | None,
    overrides: _ScalarOverrideMap,
    kpi_resolution: _KpiResolution,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[_Bucket, ViewCell], str | None]:
    """One (ticker, metric) series as {bucket: level cell} + the unit hint.

    Within a bucket the later period_end wins (a fiscal-calendar change can
    land two fiscal periods in one calendar bucket; the loaders return
    ascending series so the natural overwrite is the later one).
    """
    period_types = _ANNUAL_PERIOD_TYPES if cadence == "annual" else _QUARTERLY_PERIOD_TYPES
    cells: dict[_Bucket, ViewCell] = {}
    unit: str | None = None
    if metric.domain in ("fin", "kpi"):
        sourced: list[SourcedObservation]
        if metric.domain == "fin":
            load_key = metric.key
            sourced = load_financial_series_with_provenance(
                ticker,
                load_key,
                repo_root,
                db_path=db_path,
                period_types=period_types,
                conn=conn,
            )
        else:
            # The kpi token is a de-fragmented representative — resolve it to
            # THIS ticker's own variant (richest series) before loading, so a
            # grouped token pulls each ticker's data even when their surface
            # spellings differ (acceptance §7a.3). Falls back to the literal key
            # when nothing resolves (then the loader simply returns []).
            load_key = kpi_resolution.get(ticker.upper(), {}).get(
                kpi_group_key(metric.key), metric.key
            )
            sourced = load_kpi_series_with_provenance(
                ticker,
                load_key,
                repo_root,
                db_path=db_path,
                period_types=period_types,
                conn=conn,
            )
        for ob in sourced:
            b = _to_bucket(ob.period_end.year, ob.period_end.month, cadence)
            source = _cell_source(ob.provenance)
            cells[b] = ViewCell(value=None, raw=ob.value, source=source, sources=(source,))
            if ob.unit:
                unit = ob.unit
        # Financial override-only facts (a company-doc figure FMP never carried)
        # remain pickable — metric_catalog unions fact_overrides — so picking one
        # must not come back empty. KPI overrides are intentionally excluded: an
        # active scalar override is not authoritative without independent semantic
        # admission. Localized to the ViewsSpec read path.
        if metric.domain == "fin":
            _inject_override_only(
                cells,
                ticker=ticker,
                fact_kind="financial_fact",
                fact_key=load_key,
                cadence=cadence,
                period_types=period_types,
                overrides=overrides,
            )
        return cells, unit
    if metric.domain == "detail":
        return _load_detail_row_data(
            ticker,
            metric,
            cadence,
            db_path=db_path,
            repo_root=repo_root,
            conn=conn,
        )
    # seg: period-level provenance — the junction joins documents through
    # segment_periods.source_doc_id, so each cell chips its source document.
    dims = [(metric.dim_type or "", metric.dim_name or "")]
    seg_sourced = load_segment_junction_series_with_provenance(
        ticker,
        dims,
        metric.key,
        repo_root,
        db_path=db_path,
        period_types=period_types,
        conn=conn,
    )
    for obs in seg_sourced:
        b = _to_bucket(obs.period_end.year, obs.period_end.month, cadence)
        source = _cell_source(obs.provenance)
        cells[b] = ViewCell(value=None, raw=obs.value, source=source, sources=(source,))
        if obs.unit:
            unit = obs.unit
    return cells, unit


def _detail_source(row: sqlite3.Row, *, fact_table: str) -> CellSource:
    """Build a source chip for an admitted detail-table observation."""

    locator = row["detail_locator"]
    return CellSource(
        source=str(row["source_quality_tier"] or row["source_type"] or "company_reported"),
        fetched_at=str(row["fetched_at"]) if row["fetched_at"] is not None else None,
        source_url=str(row["source_url"]) if row["source_url"] is not None else None,
        doc_type=str(row["doc_type"]) if row["doc_type"] is not None else None,
        accession_number=(
            str(row["accession_number"]) if row["accession_number"] is not None else None
        ),
        filing_date=str(row["filing_date"]) if row["filing_date"] is not None else None,
        locator=str(locator) if locator is not None else None,
        doc_id=int(row["source_doc_id"]),
        fact_id=int(row["fact_id"]),
        fact_table=fact_table,
        extracted_by=str(row["extracted_by"]),
    )


def _load_detail_row_data(
    ticker: str,
    metric: MetricRef,
    cadence: str,
    *,
    db_path: Path | None,
    repo_root: Path | None,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[_Bucket, ViewCell], str | None]:
    """Load explicitly typed, source-backed legacy analytical families.

    These tables are not treated as a generic SQL escape hatch. Each adapter
    names its period/value/provenance contract and fails closed when the source
    document needed for a chip is unavailable.

    A borrowed ``conn`` is reused and left open; its owner closes it.
    """

    if metric.dim_type is None:
        return {}, None
    borrowed = conn is not None
    if conn is None:
        resolved = db_path or (repo_root / "data" / "portfolio.db" if repo_root else None)
        if resolved is None or not resolved.exists():
            return {}, None
        try:
            conn = connect_sqlite(resolved, role=SQLiteConnectionRole.READ_ONLY)
        except sqlite3.Error:
            return {}, None
        conn.row_factory = sqlite3.Row
    cells: dict[_Bucket, ViewCell] = {}
    unit: str | None = None
    try:
        if metric.dim_type == "customer":
            wanted_periods = (
                _ANNUAL_PERIOD_TYPES if cadence == "annual" else _QUARTERLY_PERIOD_TYPES
            )
            marks = ",".join("?" for _ in wanted_periods)
            value_column = "pct_of_revenue" if metric.key == "pct_of_revenue" else "revenue_amount"
            rows = conn.execute(
                f"""
                SELECT fact.id AS fact_id, fact.fiscal_period, fact.fiscal_period_type,
                       fact.{value_column} AS metric_value, fact.revenue_currency,
                       fact.source_doc_id, fact.source_excerpt AS detail_locator,
                       document.source_quality_tier, document.source_type,
                       document.fetched_at, document.source_url, document.doc_type,
                       document.accession_number, document.filing_date,
                       'customer_concentration_extractor' AS extracted_by
                FROM customer_concentrations fact
                JOIN documents document ON document.id=fact.source_doc_id
                WHERE UPPER(fact.ticker)=UPPER(?) AND fact.customer_label=?
                  AND fact.fiscal_period_type IN ({marks})
                  AND fact.{value_column} IS NOT NULL
                ORDER BY fact.fiscal_period, fact.id
                """,  # nosec B608 -- selected column comes from a closed internal enum
                (ticker, metric.dim_name, *wanted_periods),
            ).fetchall()
            if metric.key == "revenue_amount":
                if any(not str(row["revenue_currency"] or "").strip() for row in rows):
                    raise _IncompatibleDetailSeriesError(
                        "missing reported currency prevents an honest combined row"
                    )
                units = {
                    f"{str(row['revenue_currency'] or '').strip()} millions".strip() for row in rows
                }
                if len(units) > 1:
                    raise _IncompatibleDetailSeriesError(
                        "mixed reported currencies cannot be combined without conversion"
                    )
            for row in rows:
                try:
                    year = int(str(row["fiscal_period"])[:4])
                except ValueError:
                    continue
                source = _detail_source(row, fact_table="customer_concentrations")
                value = float(row["metric_value"])
                if metric.key == "pct_of_revenue":
                    value *= 100.0
                    unit = "%"
                else:
                    currency = str(row["revenue_currency"] or "")
                    unit = f"{currency} millions".strip()
                quarter = 0 if cadence == "annual" else int(str(row["fiscal_period_type"])[1:])
                cells[(year, quarter)] = ViewCell(
                    value=None, raw=value, source=source, sources=(source,)
                )
        elif metric.dim_type == "lease" and cadence == "annual" and metric.key == "amount":
            rows = conn.execute(
                """
                SELECT fact.id AS fact_id, fact.ladder_calendar_year, fact.amount,
                       fact.currency, fact.unit, fact.filing_doc_id AS source_doc_id,
                       fact.source_section_key AS detail_locator,
                       document.source_quality_tier, document.source_type,
                       document.fetched_at, document.source_url, document.doc_type,
                       document.accession_number, document.filing_date,
                       'lease_commitments_v1' AS extracted_by
                FROM lease_commitments fact
                JOIN documents document ON document.id=fact.filing_doc_id
                WHERE UPPER(fact.ticker)=UPPER(?) AND fact.lease_type=?
                  AND fact.fiscal_year=(
                    SELECT MAX(latest.fiscal_year) FROM lease_commitments latest
                    WHERE UPPER(latest.ticker)=UPPER(fact.ticker)
                      AND latest.lease_type=fact.lease_type
                  )
                  AND fact.ladder_calendar_year IS NOT NULL
                ORDER BY fact.ladder_calendar_year, fact.id
                """,
                (ticker, metric.dim_name),
            ).fetchall()
            if any(
                not str(row["currency"] or "").strip() or not str(row["unit"] or "").strip()
                for row in rows
            ):
                raise _IncompatibleDetailSeriesError(
                    "missing reported currency or scale prevents an honest combined row"
                )
            units = {
                f"{str(row['currency'] or '').strip()} {str(row['unit'] or '').strip()}".strip()
                for row in rows
            }
            if len(units) > 1:
                raise _IncompatibleDetailSeriesError(
                    "mixed reported currency or scale cannot be combined without conversion"
                )
            for row in rows:
                source = _detail_source(row, fact_table="lease_commitments")
                cells[(int(row["ladder_calendar_year"]), 0)] = ViewCell(
                    value=None,
                    raw=float(row["amount"]),
                    source=source,
                    sources=(source,),
                )
                unit = f"{row['currency']} {row['unit']}"
    except sqlite3.Error:
        return {}, None
    finally:
        if not borrowed:
            conn.close()
    return cells, unit


def _inject_override_only(
    cells: dict[_Bucket, ViewCell],
    *,
    ticker: str,
    fact_kind: str,
    fact_key: str,
    cadence: str,
    period_types: tuple[str, ...],
    overrides: _ScalarOverrideMap,
) -> None:
    """Add cells for periods that exist ONLY as an active ``replace`` override.

    The scalar loaders already overlaid overrides onto periods with a base row
    (so ``cells`` reflects them); this handles the override-only case the
    overlay can't reach — no base row to rewrite. A pure in-memory lookup
    against the per-view ``overrides`` map (built once by
    :func:`_active_scalar_overrides`); a no-op when there are none.
    """
    for ov in overrides.get((ticker.upper(), fact_kind, fact_key), ()):
        if ov.fiscal_period_type not in period_types or ov.value is None:
            continue
        try:
            year, month = int(ov.period_end[:4]), int(ov.period_end[5:7])
        except ValueError:
            continue
        bucket = _to_bucket(year, month, cadence)
        if bucket in cells:  # the base row already carried (and overlaid) it
            continue
        source = _cell_source(override_provenance(ov))
        cells[bucket] = ViewCell(
            value=None,
            raw=float(ov.value),
            source=source,
            sources=(source,),
        )


def _active_scalar_overrides(
    tickers: tuple[str, ...],
    *,
    db_path: Path | None,
    repo_root: Path | None,
) -> _ScalarOverrideMap:
    """Active ``replace`` overrides for fin/kpi facts across ``tickers``, keyed
    ``(TICKER, fact_kind, fact_key)`` — read ONCE per view so override-only
    injection is an in-memory lookup, not a per-cell DB hit. Best-effort: a
    missing DB / ``fact_overrides`` table yields an empty map (the common case).
    """
    resolved = db_path
    if resolved is None and repo_root is not None:
        resolved = repo_root / "data" / "portfolio.db"
    if resolved is None or not resolved.exists():
        return {}
    try:
        conn = connect_sqlite(resolved, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return {}
    conn.row_factory = sqlite3.Row
    out: _ScalarOverrideMap = {}
    try:
        for ticker in dict.fromkeys(t.upper() for t in tickers):
            for ov in get_active_overrides(conn, ticker=ticker):
                if (
                    ov.fact_kind in _SCALAR_OVERRIDE_KINDS
                    and ov.action == "replace"
                    and ov.value is not None
                ):
                    out.setdefault((ticker, ov.fact_kind, ov.fact_key), []).append(ov)
    finally:
        conn.close()
    return out


def _kpi_name_resolution(
    spec: ViewSpec,
    *,
    db_path: Path | None,
    repo_root: Path | None,
) -> _KpiResolution:
    """``{TICKER: {kpi_group_key: that ticker's richest stored name}}`` so a
    de-fragmented representative token resolves to each ticker's own variant.

    Read ONCE per view (ONE query for every requested ticker) and only when
    the spec carries a kpi metric — a no-op otherwise. Within a ticker's
    group the name with the
    MOST observations wins (load the fullest series), ties broken shortest then
    alphabetical. Best-effort: missing DB / kpi tables yield an empty map (the
    engine then loads each kpi token literally).
    """
    if not any(m.domain == "kpi" for m in spec.metrics):
        return {}
    resolved = db_path
    if resolved is None and repo_root is not None:
        resolved = repo_root / "data" / "portfolio.db"
    if resolved is None or not resolved.exists():
        return {}
    try:
        conn = connect_sqlite(resolved, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return {}
    conn.row_factory = sqlite3.Row
    tickers = tuple(dict.fromkeys(t.upper() for t in spec.tickers))
    out: _KpiResolution = {}
    if not tickers:
        return out
    try:
        try:
            fact_relation = canonical_fact_relation(conn, "kpi_facts").sql
            semantic_join, semantic_where = semantic_admission_sql(conn, fail_closed=True)
            # Same flat identity form as _grouped_kpi_catalog: one joined
            # anchor relation instead of a correlated re-derivation per row —
            # and ONE query for every requested ticker, so the anchor's window
            # pass over the resolved relation runs once per view, not once per
            # ticker (the per-ticker form re-derived it per ticker: ~11 s of a
            # 12.8 s view on the 40-issuer synthetic benchmark).
            anchor_sql = semantic_series_identity_anchor_sql(conn, fact_relation=fact_relation)
            if anchor_sql is None:
                identity_join = ""
                semantic_identity = semantic_series_identity_sql(conn, fact_relation=fact_relation)
            else:
                identity_join = (
                    f"LEFT JOIN ({anchor_sql}) series_identity_anchor "
                    "ON series_identity_anchor.definition_id = kf.kpi_definition_id"
                )
                semantic_identity = semantic_series_identity_flat_sql(conn)
            marks = ",".join("?" * len(tickers))
            rows = conn.execute(
                "SELECT kf.ticker AS ticker, kd.name AS name, COUNT(*) AS n "
                f"FROM {fact_relation} kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
                f"{semantic_join} {identity_join} WHERE kf.ticker IN ({marks}) "
                f"AND {semantic_where} AND {semantic_identity} "
                "GROUP BY kf.ticker, kd.name",
                tickers,
            ).fetchall()
        except (RuntimeError, sqlite3.Error):
            return out
        by_ticker: dict[str, dict[str, list[tuple[str, int]]]] = {}
        for r in rows:
            ticker = str(r["ticker"])
            name = str(r["name"])
            by_ticker.setdefault(ticker, {}).setdefault(kpi_group_key(name), []).append(
                (name, int(r["n"]))
            )
        out = {
            ticker: {
                key: min(cands, key=lambda c: (-c[1], len(c[0]), c[0]))[0]
                for key, cands in by_key.items()
            }
            for ticker, by_key in by_ticker.items()
        }
    finally:
        conn.close()
    return out


def _to_bucket(year: int, month: int, cadence: str) -> _Bucket:
    if cadence == "annual":
        return (year, 0)
    return (year, (month - 1) // 3 + 1)


def _lookback(bucket: _Bucket, years: int) -> _Bucket:
    return (bucket[0] - years, bucket[1])


@contextmanager
def _view_connection(
    db_path: Path | None,
    repo_root: Path | None,
) -> Generator[sqlite3.Connection | None, None, None]:
    """One read-only connection for every series a view loads, or None.

    SQLite parses the whole schema on a connection's first statement —
    about 15 ms on the canonical database, far more than any single series
    query. ``execute_view`` loads one series per ticker/metric pair, so
    sharing one connection pays that fixed cost once per view instead of
    once per pair. Best-effort like the loaders underneath: an
    unresolvable or missing DB yields None (the loaders degrade to []),
    never raises, and always closes what it opens.
    """
    resolved = db_path
    if resolved is None and repo_root is not None:
        resolved = repo_root / "data" / "portfolio.db"
    if resolved is None or not resolved.exists():
        yield None
        return
    try:
        conn = connect_sqlite(resolved, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        yield None
        return
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def execute_view(
    spec: ViewSpec,
    *,
    db_path: Path | None = None,
    repo_root: Path | None = None,
) -> ViewResult:
    """Run the spec. Best-effort like the loaders underneath: rows with no
    data become warnings, never exceptions; an unreachable DB yields an
    empty result."""
    warnings: list[str] = []
    raw_rows: list[tuple[str, MetricRef, dict[_Bucket, ViewCell], str | None]] = []
    # Read the per-view lookups ONCE (in-memory per cell, not a per-cell DB
    # hit): the override-only scalar facts, and the kpi de-fragmentation
    # resolution (representative token -> each ticker's own variant).
    overrides = _active_scalar_overrides(spec.tickers, db_path=db_path, repo_root=repo_root)
    kpi_resolution = _kpi_name_resolution(spec, db_path=db_path, repo_root=repo_root)
    # One connection for every series this view loads. SQLite parses the whole
    # schema on a connection's first statement, so a per-series connection would
    # pay that once per ticker/metric pair instead of once per view.
    with _view_connection(db_path, repo_root) as series_conn:
        # Metric-major ordering: the same metric's tickers sit adjacent, which is
        # the comparison the pivot exists for.
        for metric in spec.metrics:
            for ticker in spec.tickers:
                try:
                    cells, unit = _load_row_data(
                        ticker,
                        metric,
                        spec.cadence,
                        db_path=db_path,
                        repo_root=repo_root,
                        overrides=overrides,
                        kpi_resolution=kpi_resolution,
                        conn=series_conn,
                    )
                except _IncompatibleDetailSeriesError as exc:
                    warnings.append(f"{ticker}: {metric.token()} omitted: {exc}")
                    continue
                if not cells:
                    warnings.append(f"{ticker}: no data for {unquote(metric.token())}")
                    continue
                if metric.domain == "detail":
                    warnings.append(
                        f"{ticker}: {metric.token()} is source-backed legacy detail; "
                        "canonical observation and definition admission are pending"
                    )
                    if metric.dim_type == "customer" and spec.cadence == "quarterly":
                        warnings.append(
                            f"{ticker}: quarterly customer concentration uses issuer "
                            "fiscal-quarter labels because the legacy source does not retain "
                            "calendar period_end"
                        )
                raw_rows.append((ticker, metric, cells, unit))

    forecast_overlays: list[tuple[str, MetricRef, ForecastOverlay, str | None]] = []
    display_buckets = set(
        sorted({bucket for _, _, cells, _ in raw_rows for bucket in cells})[-spec.periods :]
    )
    resolved_db = db_path
    if resolved_db is None and repo_root is not None:
        resolved_db = repo_root / "data" / "portfolio.db"
    if spec.cadence == "annual" and spec.transform == "level" and resolved_db is not None:
        try:
            overlay_conn = connect_sqlite(resolved_db, role=SQLiteConnectionRole.READ_ONLY)
        except sqlite3.Error:
            overlay_conn = None
        if overlay_conn is not None:
            try:
                for ticker, metric, cells, unit in raw_rows:
                    displayed_cells = {
                        bucket: cell for bucket, cell in cells.items() if bucket in display_buckets
                    }
                    coordinate = canonical_coordinate_from_historical_cells(
                        overlay_conn, displayed_cells, unit=unit
                    )
                    if coordinate is None:
                        continue
                    loaded = load_forecast_overlay_for_metric(
                        overlay_conn,
                        ticker=ticker,
                        viewspec_metric_token=metric.token(),
                        coordinate=coordinate,
                        actual_unit=unit,
                    )
                    if loaded.overlay is not None:
                        forecast_overlays.append((ticker, metric, loaded.overlay, unit))
            finally:
                overlay_conn.close()

    # Margin divisor: fin:revenue per ticker, loaded once.
    revenue_by_ticker: dict[str, dict[_Bucket, ViewCell]] = {}
    if spec.transform == "margin":
        period_types = _ANNUAL_PERIOD_TYPES if spec.cadence == "annual" else _QUARTERLY_PERIOD_TYPES
        for ticker in spec.tickers:
            rev = load_financial_series_with_provenance(
                ticker, "revenue", repo_root, db_path=db_path, period_types=period_types
            )
            revenue_cells: dict[_Bucket, ViewCell] = {}
            for observation in rev:
                source = _cell_source(observation.provenance)
                revenue_cells[
                    _to_bucket(
                        observation.period_end.year,
                        observation.period_end.month,
                        spec.cadence,
                    )
                ] = ViewCell(
                    value=None,
                    raw=observation.value,
                    source=source,
                    sources=(source,),
                )
            revenue_by_ticker[ticker] = revenue_cells
            if not revenue_by_ticker[ticker]:
                warnings.append(f"{ticker}: no fin:revenue series — margin cells empty")

    all_buckets: set[_Bucket] = set()
    for _t, _m, cells, _u in raw_rows:
        all_buckets.update(cells)
    actual_buckets = sorted(all_buckets)[-spec.periods :]
    forecast_bucket_set = {
        _to_bucket(point.period_end.year, point.period_end.month, "annual")
        for _ticker, _metric, overlay, _unit in forecast_overlays
        for point in overlay.points
    }
    display_buckets = sorted(set(actual_buckets) | forecast_bucket_set)

    rows: list[ViewRow] = []
    for ticker, metric, cells, unit in raw_rows:
        out_cells: list[ViewCell] = []
        for b in display_buckets:
            cell = cells.get(b)
            raw = cell.raw if cell is not None else None
            src = cell.source if cell is not None else None
            sources = _cell_sources(cell)
            value: float | None = None
            if raw is not None:
                if spec.transform == "level":
                    value = raw
                elif spec.transform == "yoy":
                    prior_cell = cells.get(_lookback(b, 1))
                    prior = prior_cell.raw if prior_cell is not None else None
                    if prior is not None and prior != 0:
                        value = (raw / prior - 1) * 100
                        sources = _dedupe_sources(sources, _cell_sources(prior_cell))
                elif spec.transform == "cagr":
                    base_cell = cells.get(_lookback(b, spec.cagr_years))
                    base = base_cell.raw if base_cell is not None else None
                    if base is not None and base > 0 and raw > 0:
                        value = ((raw / base) ** (1 / spec.cagr_years) - 1) * 100
                        sources = _dedupe_sources(sources, _cell_sources(base_cell))
                elif spec.transform == "margin":
                    revenue_cell = revenue_by_ticker.get(ticker, {}).get(b)
                    rev = revenue_cell.raw if revenue_cell is not None else None
                    if rev is not None and rev != 0:
                        value = raw / rev * 100
                        sources = _dedupe_sources(sources, _cell_sources(revenue_cell))
            out_cells.append(ViewCell(value=value, raw=raw, source=src, sources=sources))
        rows.append(
            ViewRow(
                ticker=ticker,
                metric=metric,
                label=f"{ticker} · {metric.label}",
                unit=unit,
                cells=out_cells,
            )
        )

    forecast_rows: list[ViewForecastRow] = []
    for ticker, metric, overlay, unit in forecast_overlays:
        by_bucket = {
            _to_bucket(point.period_end.year, point.period_end.month, "annual"): point.value
            for point in overlay.points
        }
        forecast_rows.append(
            ViewForecastRow(
                ticker=ticker,
                metric=metric,
                label=f"{ticker} · {metric.label} · DCF forecast",
                unit=unit,
                values=[by_bucket.get(bucket) for bucket in display_buckets],
                dcf_run_id=overlay.dcf_run_id,
                mapping_revision_id=overlay.mapping_revision_id,
            )
        )

    from viewspec.glossary import metric_definitions

    return ViewResult(
        spec=spec,
        period_labels=[_display_period_label(spec, b) for b in display_buckets],
        rows=rows,
        warnings=warnings,
        definitions=metric_definitions(spec.metrics, db_path=resolved_db, tickers=spec.tickers),
        forecast_rows=forecast_rows,
    )


# ---------------------------------------------------------------------------
# Metric catalog (the builder UI's picker content)
# ---------------------------------------------------------------------------


def metric_catalog(
    db_path: Path,
    tickers: list[str],
    *,
    limit_per_domain: int = _CATALOG_LIMIT_PER_DOMAIN,
) -> dict[str, list[dict[str, object]]]:
    """What can be plotted for these tickers: distinct financial line items,
    KPI names (with at least one fact row), segment slices, and the small set
    of explicitly admitted detail families — each as
    ``{"token": ..., "label": ..., "tickers": n}`` ordered by how many of
    the requested tickers carry it.

    The KPI domain is DE-FRAGMENTED (acceptance §7a.3): surface variants of one
    metric — across AND within tickers — collapse onto a single comparable
    token via :func:`compute.kpi_resolver.kpi_group_key` (the same leaf
    normalization the ask name-match uses), so a typed/picked metric isn't
    split across spellings. KPI entries also carry ``origin`` (``analyst``
    curated vs ``capture`` auto-minted, from ``kpi_definitions.definition_origin``
    when present — see S1 / migration 0113). Override-only facts (a company-doc
    figure FMP never carried) are unioned in from ``fact_overrides`` so they are
    pickable too. Best-effort: missing DB/tables/columns degrade gracefully
    (empty lists / omitted ``origin``).
    """
    out: dict[str, list[dict[str, object]]] = {"fin": [], "kpi": [], "seg": [], "detail": []}
    symbols = [t.strip().upper() for t in tickers if t.strip()]
    if not symbols or not db_path.exists():
        return out
    marks = ",".join("?" * len(symbols))
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return out
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            try:
                financial_relation = canonical_fact_relation(conn, "financial_facts").sql
            except RuntimeError:
                financial_relation = "(SELECT * FROM financial_facts WHERE 0)"
            out["fin"] = _catalog_query(
                conn,
                f"""
                SELECT line_item AS k, COUNT(DISTINCT ticker) AS n
                FROM {financial_relation} WHERE ticker IN ({marks})
                GROUP BY line_item ORDER BY n DESC, k ASC LIMIT ?
                """,
                (*symbols, limit_per_domain),
                lambda r: {
                    "token": MetricRef(domain="fin", key=str(r["k"])).token(),
                    "label": str(r["k"]),
                    "tickers": int(r["n"]),
                },
            )
            out["kpi"] = _grouped_kpi_catalog(conn, marks, symbols, limit_per_domain)
            out["seg"] = _catalog_query(
                conn,
                f"""
                SELECT sd.dim_type AS dt, sd.dim_name AS dn, sd.metric AS m,
                       COUNT(DISTINCT sp.ticker) AS n
                FROM segment_dimensions sd JOIN segment_periods sp ON sp.id = sd.period_id
                WHERE sp.ticker IN ({marks})
                GROUP BY dt, dn, m ORDER BY n DESC, dn ASC, m ASC LIMIT ?
                """,
                (*symbols, limit_per_domain),
                lambda r: {
                    "token": MetricRef(
                        domain="seg",
                        dim_type=str(r["dt"]),
                        dim_name=str(r["dn"]),
                        key=str(r["m"]),
                    ).token(),
                    "label": f"{r['dn']} {r['m']} ({r['dt']})",
                    "tickers": int(r["n"]),
                },
            )
            out["detail"] = _detail_catalog(conn, marks, symbols, limit_per_domain)
            # Honesty guard (acceptance criterion: never present a truncated
            # list as complete). The cap is generous (well past a metric-rich
            # ticker's real count), so this should never fire — but if a domain
            # ever hits it, the long tail WAS cut, so say so rather than imply
            # completeness. The base lists are what the cap governs; the
            # override union is additive on top.
            for domain in ("fin", "kpi", "seg", "detail"):
                if len(out[domain]) >= limit_per_domain:
                    log.warning(
                        {
                            "event": "metric_catalog_domain_truncated",
                            "domain": domain,
                            "limit": limit_per_domain,
                            "tickers": symbols,
                        }
                    )
            _union_override_only(conn, out, symbols, marks, limit_per_domain)
    finally:
        conn.close()
    # Definition tooltips (Ask v4) — picker <option title> text per entry.
    from viewspec.glossary import attach_catalog_titles

    attach_catalog_titles(out, db_path=db_path, tickers=symbols)
    return out


def _detail_catalog(
    conn: sqlite3.Connection,
    marks: str,
    symbols: list[str],
    limit_per_domain: int,
) -> list[dict[str, object]]:
    """Catalog the small, typed non-statement fact adapters.

    A detail row is chartable only when it retains a source document. Pending
    observations and unrelated application/model tables are deliberately not
    promoted through this function.
    """

    entries: list[dict[str, object]] = []
    customer_rows = _catalog_query(
        conn,
        f"""
        SELECT fact.customer_label AS label,
               COUNT(DISTINCT fact.ticker) AS n,
               MAX(CASE WHEN fact.fiscal_period_type='FY' THEN 1 ELSE 0 END) AS has_annual,
               MAX(CASE WHEN fact.fiscal_period_type IN ('Q1','Q2','Q3','Q4') THEN 1 ELSE 0 END)
                   AS has_quarterly,
               MAX(CASE WHEN fact.fiscal_period_type='FY'
                              AND fact.revenue_amount IS NOT NULL THEN 1 ELSE 0 END)
                   AS has_amount_annual,
               MAX(CASE WHEN fact.fiscal_period_type IN ('Q1','Q2','Q3','Q4')
                              AND fact.revenue_amount IS NOT NULL THEN 1 ELSE 0 END)
                   AS has_amount_quarterly
        FROM customer_concentrations fact
        JOIN documents document ON document.id=fact.source_doc_id
        WHERE fact.ticker IN ({marks})
          AND fact.fiscal_period_type IN ('FY','Q1','Q2','Q3','Q4')
        GROUP BY fact.customer_label ORDER BY n DESC, label ASC LIMIT ?
        """,
        (*symbols, limit_per_domain),
        lambda row: {
            "label": str(row["label"]),
            "tickers": int(row["n"]),
            "has_annual": bool(row["has_annual"]),
            "has_quarterly": bool(row["has_quarterly"]),
            "has_amount_annual": bool(row["has_amount_annual"]),
            "has_amount_quarterly": bool(row["has_amount_quarterly"]),
        },
    )
    for row in customer_rows:
        label = str(row["label"])
        supported_cadences = [
            cadence
            for cadence, available in (
                ("annual", row["has_annual"]),
                ("quarterly", row["has_quarterly"]),
            )
            if available
        ]
        required_cadence = supported_cadences[0] if len(supported_cadences) == 1 else ""
        entries.append(
            {
                "token": MetricRef(
                    domain="detail",
                    key="pct_of_revenue",
                    dim_type="customer",
                    dim_name=label,
                ).token(),
                "label": f"{label} · Share of revenue",
                "tickers": row["tickers"],
                "family": "Customer concentration",
                "shape": "periodic",
                "chartable": True,
                "origin": "source_backed_legacy",
                "required_cadence": required_cadence,
                "supported_cadences": supported_cadences,
                "supported_transforms": ["level"],
                "governance_status": "definition_pending",
            }
        )
        amount_cadences = [
            cadence
            for cadence, available in (
                ("annual", row["has_amount_annual"]),
                ("quarterly", row["has_amount_quarterly"]),
            )
            if available
        ]
        if amount_cadences:
            entries.append(
                {
                    "token": MetricRef(
                        domain="detail",
                        key="revenue_amount",
                        dim_type="customer",
                        dim_name=label,
                    ).token(),
                    "label": f"{label} · Revenue amount",
                    "tickers": row["tickers"],
                    "family": "Customer concentration",
                    "shape": "periodic",
                    "chartable": True,
                    "origin": "source_backed_legacy",
                    "required_cadence": (amount_cadences[0] if len(amount_cadences) == 1 else ""),
                    "supported_cadences": amount_cadences,
                    "supported_transforms": ["level"],
                    "governance_status": "definition_pending",
                }
            )
    lease_rows = _catalog_query(
        conn,
        f"""
        SELECT fact.lease_type AS label, COUNT(DISTINCT fact.ticker) AS n
        FROM lease_commitments fact
        JOIN documents document ON document.id=fact.filing_doc_id
        WHERE fact.ticker IN ({marks}) AND fact.ladder_calendar_year IS NOT NULL
        GROUP BY fact.lease_type ORDER BY n DESC, label ASC LIMIT ?
        """,
        (*symbols, limit_per_domain),
        lambda row: {"label": str(row["label"]), "tickers": int(row["n"])},
    )
    entries.extend(
        {
            "token": MetricRef(
                domain="detail",
                key="amount",
                dim_type="lease",
                dim_name=str(row["label"]),
            ).token(),
            "label": f"{str(row['label']).title()} lease commitments",
            "tickers": row["tickers"],
            "family": "Lease maturity ladder",
            "shape": "ladder",
            "chartable": True,
            "origin": "source_backed_legacy",
            "required_cadence": "annual",
            "supported_transforms": ["level"],
            "governance_status": "definition_pending",
        }
        for row in lease_rows
    )
    return entries[:limit_per_domain]


def _grouped_kpi_catalog(
    conn: sqlite3.Connection,
    marks: str,
    symbols: list[str],
    limit_per_domain: int,
) -> list[dict[str, object]]:
    """The KPI domain, de-fragmented by :func:`kpi_group_key` (acceptance §7a.3).

    Surface variants of one metric — across AND within tickers — collapse to a
    single entry. The representative (the token + label) is the SHORTEST variant
    (the cleanest leaf), most-observations breaking ties; ``tickers`` counts the
    DISTINCT tickers across the whole group; ``origin`` is ``analyst`` if ANY
    member is curated, else ``capture`` (when ``definition_origin`` exists —
    migration 0113). The token stays per-ticker resolvable: at execution
    :func:`_kpi_name_resolution` maps the group key back to each ticker's own
    variant. Best-effort: a missing kpi table yields ``[]``.
    """
    has_origin = _column_exists(conn, "kpi_definitions", "definition_origin")
    origin_select = ", kd.definition_origin AS origin" if has_origin else ""
    try:
        fact_relation = canonical_fact_relation(conn, "kpi_facts").sql
        semantic_join, semantic_where = semantic_admission_sql(conn, fail_closed=True)
        # Flat identity: the per-definition anchor as one joined relation
        # (~19x faster than the correlated predicate on aggregate queries,
        # identical rows). Legacy schemas without the identity columns keep
        # the correlated predicate, which degrades to 1=1 there anyway.
        anchor_sql = semantic_series_identity_anchor_sql(conn, fact_relation=fact_relation)
        if anchor_sql is None:
            identity_join = ""
            semantic_identity = semantic_series_identity_sql(conn, fact_relation=fact_relation)
        else:
            identity_join = (
                f"LEFT JOIN ({anchor_sql}) series_identity_anchor "
                "ON series_identity_anchor.definition_id = kf.kpi_definition_id"
            )
            semantic_identity = semantic_series_identity_flat_sql(conn)
        rows = conn.execute(
            f"""
            SELECT kd.name AS name, kf.ticker AS ticker, COUNT(*) AS obs{origin_select}
            FROM {fact_relation} kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
            {semantic_join}
            {identity_join}
            WHERE kf.ticker IN ({marks}) AND {semantic_where} AND {semantic_identity}
            GROUP BY kd.name, kf.ticker
            """,
            tuple(symbols),
        ).fetchall()
    except (RuntimeError, sqlite3.Error):
        return []
    obs_by_name: dict[str, dict[str, int]] = {}
    tickers_by_key: dict[str, set[str]] = {}
    analyst_by_key: dict[str, bool] = {}
    for r in rows:
        name = str(r["name"])
        key = kpi_group_key(name)
        names = obs_by_name.setdefault(key, {})
        names[name] = names.get(name, 0) + int(r["obs"])
        tickers_by_key.setdefault(key, set()).add(str(r["ticker"]))
        if has_origin and str(r["origin"] or "analyst") == "analyst":
            analyst_by_key[key] = True
    prelim: list[tuple[str, int, bool]] = []
    for key, names in obs_by_name.items():
        prelim.append(
            (_kpi_representative(names), len(tickers_by_key[key]), analyst_by_key.get(key, False))
        )
    # Most broadly-reported first, then alphabetical — same shape as the other
    # domains' ORDER BY.
    prelim.sort(key=lambda t: (-t[1], t[0]))
    entries: list[dict[str, object]] = []
    for rep, n_tickers, analyst in prelim[:limit_per_domain]:
        entry: dict[str, object] = {
            "token": MetricRef(domain="kpi", key=rep).token(),
            "label": rep,
            "tickers": n_tickers,
        }
        if has_origin:
            entry["origin"] = "analyst" if analyst else "capture"
        entries.append(entry)
    return entries


def _kpi_representative(names: dict[str, int]) -> str:
    """The display name for a de-fragmented KPI group: the SHORTEST variant (the
    cleanest leaf), the most-observations one breaking ties, then alphabetical."""
    return min(names, key=lambda n: (len(n), -names[n], n))


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """True iff PRAGMA reports ``column`` on ``table`` (False on any error)."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return False
    return any(str(r["name"]) == column for r in rows)


def _override_token(fact_kind: str, fact_key: str) -> tuple[str, str, str] | None:
    """Map a ``fact_overrides`` (fact_kind, fact_key) to a (domain, token,
    label) for the picker, or None when it has no single pickable metric.

    Every token is :meth:`MetricRef.parse_token`-parseable so a picked
    override-only fact validates like any catalog entry. Record-level segment
    overrides (``fact_key`` with no ``|``) name a whole dimension, not one
    slice — skipped (their cells are already enumerated from the base tables).
    """
    if fact_kind == "financial_fact":
        return ("fin", MetricRef(domain="fin", key=fact_key).token(), fact_key)
    # KPI scalar overrides are not consumer-authoritative without an independent
    # semantic admission record, so they never become pickable view metrics.
    if fact_kind == "segment":
        parts = fact_key.split("|")
        if len(parts) == 3 and all(parts):
            dt, dn, m = parts
            token = MetricRef(domain="seg", dim_type=dt, dim_name=dn, key=m).token()
            return ("seg", token, f"{dn} {m} ({dt})")
    return None


def _union_override_only(
    conn: sqlite3.Connection,
    out: dict[str, list[dict[str, object]]],
    symbols: list[str],
    marks: str,
    limit_per_domain: int,
) -> None:
    """Add picker entries for non-KPI facts that exist only as ``fact_overrides``.

    The base queries enumerate the substrate tables; a fact a company published
    that FMP never carried lives only in ``fact_overrides`` and would otherwise
    be invisible (and so unpickable). Union those in, deduped against the base
    tokens already present (an override that supersedes an existing base row is
    a no-op here — its token is already listed). Best-effort: a missing
    ``fact_overrides`` table is a no-op.
    """
    existing = {str(e["token"]) for entries in out.values() for e in entries}
    try:
        rows = conn.execute(
            f"""
            SELECT fact_kind, fact_key, COUNT(DISTINCT ticker) AS n
            FROM fact_overrides
            WHERE ticker IN ({marks}) AND status = 'active' AND action <> 'drop'
              AND fact_kind <> 'kpi'
            GROUP BY fact_kind, fact_key ORDER BY n DESC, fact_key ASC LIMIT ?
            """,
            (*symbols, limit_per_domain),
        ).fetchall()
    except sqlite3.Error:
        return
    for r in rows:
        mapped = _override_token(str(r["fact_kind"]), str(r["fact_key"]))
        if mapped is None:
            continue
        domain, token, label = mapped
        if token in existing:
            continue
        existing.add(token)
        out[domain].append(
            {"token": token, "label": label, "tickers": int(r["n"]), "override_only": True}
        )


def _catalog_query(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...],
    to_entry: Callable[[sqlite3.Row], dict[str, object]],
) -> list[dict[str, object]]:
    """Run one catalog query, tolerating a missing table ([] instead)."""
    try:
        return [to_entry(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []
