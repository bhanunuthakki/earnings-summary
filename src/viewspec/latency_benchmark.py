"""Measure ViewSpec read latency for a rerun-per-interaction analytics surface.

A Streamlit-style surface re-executes its whole script on every widget change,
so the per-interaction cost of :func:`viewspec.engine.metric_catalog` and
:func:`viewspec.engine.execute_view` decides whether such a surface is viable
at all. This module measures those two calls against explicit budgets and
emits one atomic JSON receipt.

Two modes, both read-only against the measured database:

``synthetic``
    Migrate a caller-selected NEW database to head, then seed the exact
    relations the ViewSpec read path traverses. The clone is a disposable
    measurement artifact: never a live, fallback, replica, or roster authority.

``snapshot``
    Measure an existing provenance-bearing database (a restored ``.tmp/``
    snapshot or the configured authority) without writing to it.

Scope honesty. The synthetic clone seeds ``documents``, ``financial_facts``,
``kpi_definitions``, ``kpi_facts``, the fact-admission chain the canonical
resolved views join, and the KPI semantic contexts the catalog requires. It
deliberately does NOT fabricate the evidence graph, fact overrides, segment
tables, or the typed detail families: those are absent, not complete, so every
report carries a relation census and an explicit ``empty_relations`` list. A
passing synthetic run therefore bounds only the populations it actually
carried, and the report says so rather than implying full coverage.

Cached-rerun cost is measured as a pickle round-trip of the ``ViewResult``,
because that is what a memoizing cache pays on every rerun after the first.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pickle
import platform
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TypeVar

import pydantic
from alembic.config import Config
from pydantic import BaseModel, ConfigDict, Field

from alembic import command
from viewspec.engine import execute_view, metric_catalog
from viewspec.spec import MAX_METRICS, MAX_TICKERS, MetricRef, ViewSpec

REPORT_VERSION = "viewspec_latency_benchmark.v1"

_T = TypeVar("_T")

# Relations the ViewSpec read path traverses, so a report can state which of
# them were empty when the numbers were taken.
_CENSUS_RELATIONS: tuple[str, ...] = (
    "documents",
    "financial_facts",
    "fact_observation_revisions",
    "observation_resolution_revisions",
    "fact_resolution_outcomes",
    "kpi_definitions",
    "kpi_facts",
    "kpi_fact_semantic_contexts",
    "segment_periods",
    "segment_dimensions",
    "customer_concentrations",
    "lease_commitments",
    "fact_overrides",
)

_QUARTER_ENDS: tuple[tuple[int, int], ...] = ((3, 31), (6, 30), (9, 30), (12, 31))
_QUARTER_TYPES: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RefusedBenchmarkPathError(RuntimeError):
    """A caller-selected path is not an allowed benchmark target."""


class SyntheticScale(_FrozenModel):
    """Shape of the synthetic clone, in production-representative units."""

    tickers: int = Field(ge=1, le=200)
    line_items: int = Field(ge=1, le=1_000)
    kpis: int = Field(ge=0, le=500)
    quarters: int = Field(ge=1, le=80)
    documents_per_ticker: int = Field(ge=1, le=200)

    @property
    def financial_fact_rows(self) -> int:
        return self.tickers * self.line_items * self.quarters

    @property
    def kpi_fact_rows(self) -> int:
        return self.tickers * self.kpis * self.quarters


class LatencyBudgets(_FrozenModel):
    """Per-interaction ceilings for a rerun-per-interaction surface."""

    max_catalog_cold_milliseconds: float = Field(gt=0)
    max_view_small_p95_milliseconds: float = Field(gt=0)
    max_view_full_p95_milliseconds: float = Field(gt=0)
    max_cached_payload_p95_milliseconds: float = Field(gt=0)


class CallMeasurement(_FrozenModel):
    samples: int = Field(ge=1)
    p50_milliseconds: float = Field(ge=0)
    p95_milliseconds: float = Field(ge=0)
    max_milliseconds: float = Field(ge=0)


class ViewShape(_FrozenModel):
    tickers: int = Field(ge=1)
    metrics: int = Field(ge=1)
    cadence: str
    periods: int = Field(ge=1)
    rows_returned: int = Field(ge=0)
    populated_cells: int = Field(ge=0)
    warnings: int = Field(ge=0)


class ViewMeasurement(_FrozenModel):
    shape: ViewShape
    latency: CallMeasurement
    cached_payload_latency: CallMeasurement
    payload_bytes: int = Field(ge=0)


class CatalogMeasurement(_FrozenModel):
    tickers: int = Field(ge=1)
    latency: CallMeasurement
    entries_by_domain: dict[str, int]


class CatalogSweepPoint(_FrozenModel):
    tickers: int = Field(ge=1)
    latency: CallMeasurement
    entries: int = Field(ge=0)


class ViewSweepPoint(_FrozenModel):
    tickers: int = Field(ge=1)
    metrics: int = Field(ge=1)
    pairs: int = Field(ge=1)
    latency: CallMeasurement
    milliseconds_per_pair: float = Field(ge=0)


class BudgetResult(_FrozenModel):
    budget_name: str
    operator: Literal["<="]
    actual: float
    limit: float
    passed: bool


class EnvironmentVersions(_FrozenModel):
    python: str
    sqlite: str
    pydantic: str
    platform: str


class LatencyReport(_FrozenModel):
    report_version: Literal["viewspec_latency_benchmark.v1"]
    mode: Literal["synthetic", "snapshot"]
    measured_at: str
    scale: SyntheticScale | None
    budgets: LatencyBudgets
    database_bytes: int = Field(ge=0)
    relation_census: dict[str, int]
    empty_relations: tuple[str, ...]
    catalog: CatalogMeasurement
    small_view: ViewMeasurement
    full_view: ViewMeasurement
    catalog_sweep: tuple[CatalogSweepPoint, ...] = ()
    view_sweep: tuple[ViewSweepPoint, ...] = ()
    environment: EnvironmentVersions
    budget_results: tuple[BudgetResult, ...]
    overall_pass: bool
    report_sha256: str = Field(min_length=64, max_length=64)


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return ordered[index]


def _measure(samples: list[float]) -> CallMeasurement:
    return CallMeasurement(
        samples=len(samples),
        p50_milliseconds=_percentile(samples, 0.50),
        p95_milliseconds=_percentile(samples, 0.95),
        max_milliseconds=max(samples),
    )


# ---------------------------------------------------------------------------
# Synthetic seeding
# ---------------------------------------------------------------------------


def _ticker_symbols(count: int) -> list[str]:
    """Deterministic synthetic symbols that cannot collide with real issuers."""
    return [f"ZZ{index:03d}" for index in range(1, count + 1)]


def _period_ends(quarters: int) -> list[tuple[str, str, str]]:
    """Return ``(period_end, fiscal_period_type, period_start)`` oldest first."""
    out: list[tuple[str, str, str]] = []
    # Walk back from a fixed anchor so the clone is reproducible.
    anchor = datetime(2026, 6, 30, tzinfo=UTC)
    for step in range(quarters):
        index = (anchor.month // 3 - 1 - step) % 4
        year = anchor.year + (anchor.month // 3 - 1 - step) // 4
        month, day = _QUARTER_ENDS[index]
        end = datetime(year, month, day, tzinfo=UTC)
        start = end - timedelta(days=89)
        out.append(
            (
                end.strftime("%Y-%m-%d %H:%M:%S"),
                _QUARTER_TYPES[index],
                start.strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
    return list(reversed(out))


def _drop_write_admission_triggers(conn: sqlite3.Connection) -> list[str]:
    """Drop INSERT triggers on the fact tables and report what was removed.

    This disposable clone measures the READ plan. Bulk-seeding through the
    production write-admission trigger would require fabricating a complete
    evidence ledger, which would add no read-path fidelity. The production
    tables, migrations, indexes, and canonical views remain exact.
    """
    dropped: list[str] = []
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name IN ('financial_facts','kpi_facts')"
    ).fetchall()
    for name, sql in rows:
        if "INSERT ON" in str(sql).upper() or "AFTER INSERT" in str(sql).upper():
            conn.execute(f"DROP TRIGGER {name}")  # nosec B608 -- name from sqlite_master
            dropped.append(str(name))
    return dropped


def _max_id(conn: sqlite3.Connection, table: str) -> int:
    """Highest existing primary key, so seeded ids never collide with migrations."""
    row = conn.execute(
        f"SELECT COALESCE(MAX(id),0) FROM {table}"  # nosec B608 -- fixed table allowlist
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _seed_documents(conn: sqlite3.Connection, scale: SyntheticScale) -> dict[str, list[int]]:
    doc_ids: dict[str, list[int]] = {}
    next_id = _max_id(conn, "documents") + 1
    rows: list[tuple[object, ...]] = []
    for ticker in _ticker_symbols(scale.tickers):
        ids: list[int] = []
        for sequence in range(scale.documents_per_ticker):
            document_id = next_id
            next_id += 1
            ids.append(document_id)
            rows.append(
                (
                    document_id,
                    ticker,
                    "sec",
                    "10-Q",
                    f"synthetic/{ticker}/{sequence}.htm",
                    hashlib.sha256(f"{ticker}:{sequence}".encode()).hexdigest(),
                    "2026-07-01 00:00:00",
                    "ok",
                    1024,
                    "sec_filing",
                    f"0000000000-26-{document_id:06d}",
                    "2026-07-01",
                )
            )
        doc_ids[ticker] = ids
    conn.executemany(
        "INSERT INTO documents(id,ticker,source_type,doc_type,file_path,sha256,fetched_at,"
        "fetch_status,raw_bytes_size,source_quality_tier,accession_number,filing_date) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return doc_ids


def _admission_rows(
    fact_table: str,
    fact_row_id: int,
    *,
    ticker: str,
    concept_key: str,
    period_start: str,
    period_end: str,
    fiscal_period_type: str,
    value: float,
    unit: str,
    document_id: int,
) -> tuple[
    tuple[object, ...],
    tuple[object, ...],
    tuple[object, ...],
    tuple[object, ...],
    tuple[object, ...],
]:
    """Build the admission-chain rows that make a fact canonically visible.

    Order matters: the candidate set must exist before its resolution revision
    (``trg_observation_resolution_selected_candidate``), and the candidate's
    own FK to that revision is deferred for exactly this reason.
    """
    observation_id = f"{fact_table}:{fact_row_id}:r1"
    logical_key = f"{fact_table}:{fact_row_id}"
    resolution_id = f"resolution:{fact_table}:{fact_row_id}:r1"
    period_kind = "quarter" if fiscal_period_type in _QUARTER_TYPES else "annual"
    observation = (
        observation_id,
        f"fact-capture:{observation_id}",
        f"issuer:{ticker}",
        ticker,
        concept_key,
        period_start,
        period_end,
        period_kind,
        "[]",
        str(value),
        None,
        "USD",
        unit,
        0,
        "reported",
        f"evidence-node:{fact_table}:{fact_row_id}",
        "2026-07-01 00:00:00",
        "2026-07-01 00:00:00",
        "synthetic-benchmark",
        "latency-benchmark-v1",
        1.0,
        None,
        None,
    )
    link = (
        fact_table,
        fact_row_id,
        1,
        observation_id,
        logical_key,
        document_id,
        "sec_filing",
        "2026-07-01 00:00:00",
    )
    resolution = (
        resolution_id,
        f"resolve:{logical_key}:r1",
        logical_key,
        1,
        observation_id,
        "synthetic-benchmark",
        "latency-benchmark-v1",
        "single admitted candidate",
        "2026-07-01 00:00:00",
        "2026-07-01 00:00:00",
        0,
        None,
        "2026-07-01 00:00:00",
    )
    outcome = (
        resolution_id,
        "resolved",
        hashlib.sha256(observation_id.encode()).hexdigest(),
        "{}",
        "2026-07-01 00:00:00",
    )
    candidate = (resolution_id, observation_id)
    return observation, link, candidate, resolution, outcome


_OBSERVATION_SQL = (
    "INSERT INTO reported_observations(observation_id,idempotency_key,issuer_id,ticker,"
    "concept_key,period_start,period_end,fiscal_period_type,dimensions_json,numeric_value,"
    "text_value,currency,unit,scale,observation_status,evidence_node_id,available_at,"
    "recorded_at,method,method_version,confidence,legacy_table,legacy_row_id) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_LINK_SQL = (
    "INSERT INTO fact_observation_revisions(fact_table,fact_row_id,fact_revision,"
    "observation_id,logical_key,source_document_id,source_tier,captured_at) "
    "VALUES (?,?,?,?,?,?,?,?)"
)
_RESOLUTION_SQL = (
    "INSERT INTO observation_resolution_revisions(resolution_id,idempotency_key,logical_key,"
    "revision,selected_observation_id,resolver_kind,policy_version,reason,knowledge_cutoff,"
    "effective_at,material_dissent,supersedes_resolution_id,recorded_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_OUTCOME_SQL = (
    "INSERT INTO fact_resolution_outcomes(resolution_id,resolution_status,"
    "candidate_set_sha256,checks_json,recorded_at) VALUES (?,?,?,?,?)"
)
_CANDIDATE_SQL = (
    "INSERT INTO observation_resolution_candidates(resolution_id,observation_id) VALUES (?,?)"
)
_FINANCIAL_FACT_SQL = (
    "INSERT INTO financial_facts(id,ticker,period_end,fiscal_period_type,line_item,value,"
    "currency,unit,source_doc_id,confidence,extracted_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
)


class _AdmissionBatch:
    """Accumulated admission-chain rows, flushed in dependency order."""

    def __init__(self) -> None:
        self.observations: list[tuple[object, ...]] = []
        self.links: list[tuple[object, ...]] = []
        self.candidates: list[tuple[object, ...]] = []
        self.resolutions: list[tuple[object, ...]] = []
        self.outcomes: list[tuple[object, ...]] = []

    def add(self, rows: tuple[tuple[object, ...], ...]) -> None:
        observation, link, candidate, resolution, outcome = rows
        self.observations.append(observation)
        self.links.append(link)
        self.candidates.append(candidate)
        self.resolutions.append(resolution)
        self.outcomes.append(outcome)

    def flush(self, conn: sqlite3.Connection) -> None:
        conn.executemany(_OBSERVATION_SQL, self.observations)
        conn.executemany(_LINK_SQL, self.links)
        conn.executemany(_CANDIDATE_SQL, self.candidates)
        conn.executemany(_RESOLUTION_SQL, self.resolutions)
        conn.executemany(_OUTCOME_SQL, self.outcomes)
        self.observations.clear()
        self.links.clear()
        self.candidates.clear()
        self.resolutions.clear()
        self.outcomes.clear()


def line_item_names(count: int) -> list[str]:
    """Deterministic statement line items, revenue first so margin views work."""
    names = ["revenue"]
    names.extend(f"line_item_{index:03d}" for index in range(1, count))
    return names[:count]


def kpi_names(count: int) -> list[str]:
    return [f"synthetic kpi {index:03d}" for index in range(1, count + 1)]


def seed_synthetic_database(db_path: Path, scale: SyntheticScale) -> None:
    """Migrate a NEW database to head and seed the ViewSpec read-path relations."""
    if db_path.exists():
        raise RefusedBenchmarkPathError(f"synthetic database must not already exist: {db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    command.upgrade(config, "head")

    periods = _period_ends(scale.quarters)
    items = line_item_names(scale.line_items)
    kpis = kpi_names(scale.kpis)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _drop_write_admission_triggers(conn)
        documents = _seed_documents(conn, scale)

        fact_rows: list[tuple[object, ...]] = []
        batch = _AdmissionBatch()
        fact_id = _max_id(conn, "financial_facts")
        for ticker in _ticker_symbols(scale.tickers):
            ticker_documents = documents[ticker]
            for item_index, line_item in enumerate(items):
                for period_index, (end, period_type, start) in enumerate(periods):
                    fact_id += 1
                    document_id = ticker_documents[period_index % len(ticker_documents)]
                    value = 1_000.0 + item_index * 10 + period_index
                    fact_rows.append(
                        (
                            fact_id,
                            ticker,
                            end,
                            period_type,
                            line_item,
                            value,
                            "USD",
                            "millions",
                            document_id,
                            1.0,
                            "synthetic-benchmark",
                        )
                    )
                    batch.add(
                        _admission_rows(
                            "financial_facts",
                            fact_id,
                            ticker=ticker,
                            concept_key=line_item,
                            period_start=start,
                            period_end=end,
                            fiscal_period_type=period_type,
                            value=value,
                            unit="millions",
                            document_id=document_id,
                        )
                    )
                    if len(fact_rows) >= 5_000:
                        conn.executemany(_FINANCIAL_FACT_SQL, fact_rows)
                        fact_rows.clear()
                        batch.flush(conn)
        if fact_rows:
            conn.executemany(_FINANCIAL_FACT_SQL, fact_rows)
            batch.flush(conn)
        conn.commit()

        if kpis:
            _seed_kpis(conn, scale, kpis=kpis, periods=periods, documents=documents)
        conn.commit()
        conn.execute("ANALYZE")
        conn.commit()
    finally:
        conn.close()


def _seed_kpis(
    conn: sqlite3.Connection,
    scale: SyntheticScale,
    *,
    kpis: list[str],
    periods: list[tuple[str, str, str]],
    documents: dict[str, list[int]],
) -> None:
    symbols = _ticker_symbols(scale.tickers)
    # Migrations may already carry curated definitions, so let SQLite assign ids
    # and read back the ones this clone owns rather than assuming a range.
    conn.executemany(
        "INSERT INTO kpi_definitions(ticker,name,unit,primary_source) VALUES (?,?,?,?)",
        [(ticker, name, "actual", "ir_release") for ticker in symbols for name in kpis],
    )
    marks = ",".join("?" * len(symbols))
    definition_ids: dict[tuple[str, str], int] = {
        (str(row[0]), str(row[1])): int(row[2])
        for row in conn.execute(
            f"SELECT ticker,name,id FROM kpi_definitions WHERE ticker IN ({marks})",  # nosec B608
            symbols,
        )
    }

    fact_rows: list[tuple[object, ...]] = []
    context_rows: list[tuple[object, ...]] = []
    batch = _AdmissionBatch()
    fact_id = _max_id(conn, "kpi_facts")
    for ticker in symbols:
        ticker_documents = documents[ticker]
        for name_index, name in enumerate(kpis):
            for period_index, (end, period_type, start) in enumerate(periods):
                fact_id += 1
                document_id = ticker_documents[period_index % len(ticker_documents)]
                value = 50.0 + name_index + period_index
                fact_rows.append(
                    (
                        fact_id,
                        ticker,
                        end,
                        period_type,
                        definition_ids[(ticker, name)],
                        value,
                        "actual",
                        document_id,
                        1.0,
                        "synthetic-benchmark",
                    )
                )
                context_rows.append(
                    (
                        fact_id,
                        1,
                        name,
                        end[:10],
                        "current",
                        "current_actual",
                        "non_gaap",
                        "consolidated",
                        "{}",
                        "none",
                        "admitted",
                        "synthetic-benchmark",
                        "2026-07-01T00:00:00Z",
                    )
                )
                batch.add(
                    _admission_rows(
                        "kpi_facts",
                        fact_id,
                        ticker=ticker,
                        concept_key=name,
                        period_start=start,
                        period_end=end,
                        fiscal_period_type=period_type,
                        value=value,
                        unit="actual",
                        document_id=document_id,
                    )
                )
    conn.executemany(
        "INSERT INTO kpi_facts(id,ticker,period_end,fiscal_period_type,kpi_definition_id,value,"
        "unit,source_doc_id,confidence,extracted_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
        fact_rows,
    )
    conn.executemany(
        "INSERT INTO kpi_fact_semantic_contexts(kpi_fact_id,revision,metric_name_as_reported,"
        "reported_period_end,period_role,publication_lane,accounting_basis,consolidation_scope,"
        "dimensions_json,unit_scale,status,reviewed_by,knowledge_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        context_rows,
    )
    batch.flush(conn)


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def relation_census(db_path: Path) -> dict[str, int]:
    """Row counts for every relation the ViewSpec read path can traverse."""
    census: dict[str, int] = {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for relation in _CENSUS_RELATIONS:
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {relation}"  # nosec B608 -- fixed relation allowlist
                ).fetchone()
            except sqlite3.Error:
                census[relation] = -1
                continue
            census[relation] = int(row[0]) if row is not None else 0
    finally:
        conn.close()
    return census


def _time_call(call: Callable[[], _T], samples: int) -> tuple[list[float], _T]:
    """Time ``call`` ``samples`` times, returning durations and the last result.

    No warm-up: the first sample is deliberately the coldest one this process
    can observe, which is what an interactive surface pays on first paint. The
    OS page cache is still warm from seeding, so a synthetic run reports a
    lower bound on first-open cost, never an upper bound.
    """
    durations: list[float] = []
    started = time.perf_counter()
    result = call()
    durations.append((time.perf_counter() - started) * 1_000)
    for _ in range(max(0, samples - 1)):
        started = time.perf_counter()
        result = call()
        durations.append((time.perf_counter() - started) * 1_000)
    return durations, result


def measure_catalog(
    db_path: Path, tickers: list[str], *, samples: int
) -> tuple[CatalogMeasurement, dict[str, list[dict[str, object]]]]:
    durations, catalog = _time_call(lambda: metric_catalog(db_path, tickers), samples)
    return (
        CatalogMeasurement(
            tickers=len(tickers),
            latency=_measure(durations),
            entries_by_domain={domain: len(entries) for domain, entries in catalog.items()},
        ),
        catalog,
    )


def measure_view(db_path: Path, spec: ViewSpec, *, samples: int) -> ViewMeasurement:
    durations, result = _time_call(lambda: execute_view(spec, db_path=db_path), samples)
    payload = pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL)
    # What a memoizing cache pays on every rerun after the first: rehydrating
    # the stored result, not re-reading the database.
    cached_durations, _ = _time_call(lambda: pickle.loads(payload), samples)  # nosec B301
    populated = sum(1 for row in result.rows for cell in row.cells if cell.value is not None)
    return ViewMeasurement(
        shape=ViewShape(
            tickers=len(spec.tickers),
            metrics=len(spec.metrics),
            cadence=spec.cadence,
            periods=spec.periods,
            rows_returned=len(result.rows),
            populated_cells=populated,
            warnings=len(result.warnings),
        ),
        latency=_measure(durations),
        cached_payload_latency=_measure(cached_durations),
        payload_bytes=len(payload),
    )


_CATALOG_SWEEP_TICKERS: tuple[int, ...] = (1, 2, 4, 8, 16)
_VIEW_SWEEP_SHAPES: tuple[tuple[int, int], ...] = (
    (1, 1),
    (2, 3),
    (4, 3),
    (4, 5),
    (8, 5),
    (16, 10),
)


def sweep_catalog(
    db_path: Path, tickers: list[str], *, samples: int
) -> tuple[CatalogSweepPoint, ...]:
    """Catalog latency by selected-ticker count.

    The picker only ever asks for the tickers the reader selected, so the
    single all-roster number is not the interactive cost.
    """
    points: list[CatalogSweepPoint] = []
    for count in _CATALOG_SWEEP_TICKERS:
        if count > len(tickers):
            continue
        subset = tickers[:count]
        durations, catalog = _time_call(
            lambda subset=subset: metric_catalog(db_path, subset), samples
        )
        points.append(
            CatalogSweepPoint(
                tickers=count,
                latency=_measure(durations),
                entries=sum(len(entries) for entries in catalog.values()),
            )
        )
    return tuple(points)


def sweep_views(
    db_path: Path,
    tickers: list[str],
    catalog: dict[str, list[dict[str, object]]],
    *,
    samples: int,
    periods: int,
) -> tuple[ViewSweepPoint, ...]:
    """View latency by requested cell shape, to expose what actually drives it."""
    points: list[ViewSweepPoint] = []
    for ticker_count, metric_count in _VIEW_SWEEP_SHAPES:
        if ticker_count > len(tickers):
            continue
        try:
            metrics = _fin_metrics(catalog, metric_count)
        except RefusedBenchmarkPathError:
            continue
        if len(metrics) < metric_count:
            continue
        spec = ViewSpec(
            tickers=tuple(tickers[:ticker_count]),
            metrics=metrics,
            transform="level",
            cadence="quarterly",
            periods=periods,
        )
        durations, _ = _time_call(lambda spec=spec: execute_view(spec, db_path=db_path), samples)
        measurement = _measure(durations)
        pairs = ticker_count * metric_count
        points.append(
            ViewSweepPoint(
                tickers=ticker_count,
                metrics=metric_count,
                pairs=pairs,
                latency=measurement,
                milliseconds_per_pair=measurement.p50_milliseconds / pairs,
            )
        )
    return tuple(points)


def _budget_results(
    budgets: LatencyBudgets,
    catalog: CatalogMeasurement,
    small: ViewMeasurement,
    full: ViewMeasurement,
) -> tuple[BudgetResult, ...]:
    checks = (
        ("catalog_cold", catalog.latency.max_milliseconds, budgets.max_catalog_cold_milliseconds),
        (
            "view_small_p95",
            small.latency.p95_milliseconds,
            budgets.max_view_small_p95_milliseconds,
        ),
        ("view_full_p95", full.latency.p95_milliseconds, budgets.max_view_full_p95_milliseconds),
        (
            "cached_payload_p95",
            max(
                small.cached_payload_latency.p95_milliseconds,
                full.cached_payload_latency.p95_milliseconds,
            ),
            budgets.max_cached_payload_p95_milliseconds,
        ),
    )
    return tuple(
        BudgetResult(
            budget_name=name, operator="<=", actual=actual, limit=limit, passed=actual <= limit
        )
        for name, actual, limit in checks
    )


def _environment() -> EnvironmentVersions:
    return EnvironmentVersions(
        python=sys.version.split()[0],
        sqlite=sqlite3.sqlite_version,
        pydantic=pydantic.VERSION,
        platform=platform.platform(),
    )


def _fin_metrics(catalog: dict[str, list[dict[str, object]]], count: int) -> tuple[MetricRef, ...]:
    tokens = [str(entry["token"]) for entry in catalog.get("fin", [])[:count]]
    if not tokens:
        raise RefusedBenchmarkPathError(
            "measured database exposes no canonical fin metrics; nothing to time"
        )
    return tuple(MetricRef.parse_token(token) for token in tokens)


def run_benchmark(
    *,
    db_path: Path,
    tickers: list[str],
    budgets: LatencyBudgets,
    samples: int,
    mode: Literal["synthetic", "snapshot"],
    scale: SyntheticScale | None,
    periods: int = 12,
    sweep: bool = False,
) -> LatencyReport:
    """Measure catalog and view latency against ``budgets`` and build the receipt."""
    census = relation_census(db_path)
    catalog_measurement, catalog = measure_catalog(db_path, tickers, samples=samples)

    small_tickers = tuple(tickers[: min(4, len(tickers))])
    full_tickers = tuple(tickers[: min(MAX_TICKERS, len(tickers))])
    small_spec = ViewSpec(
        tickers=small_tickers,
        metrics=_fin_metrics(catalog, 3),
        transform="level",
        cadence="quarterly",
        periods=periods,
    )
    full_spec = ViewSpec(
        tickers=full_tickers,
        metrics=_fin_metrics(catalog, MAX_METRICS),
        transform="level",
        cadence="quarterly",
        periods=periods,
    )
    small = measure_view(db_path, small_spec, samples=samples)
    full = measure_view(db_path, full_spec, samples=samples)
    catalog_sweep = sweep_catalog(db_path, tickers, samples=samples) if sweep else ()
    view_sweep = (
        sweep_views(db_path, tickers, catalog, samples=samples, periods=periods) if sweep else ()
    )

    results = _budget_results(budgets, catalog_measurement, small, full)
    body: dict[str, object] = {
        "report_version": REPORT_VERSION,
        "mode": mode,
        "measured_at": datetime.now(UTC).isoformat(),
        "scale": scale.model_dump(mode="json") if scale is not None else None,
        "budgets": budgets.model_dump(mode="json"),
        "database_bytes": db_path.stat().st_size,
        "relation_census": census,
        "empty_relations": tuple(
            name for name, count in sorted(census.items()) if count in {0, -1}
        ),
        "catalog": catalog_measurement.model_dump(mode="json"),
        "small_view": small.model_dump(mode="json"),
        "full_view": full.model_dump(mode="json"),
        "catalog_sweep": [point.model_dump(mode="json") for point in catalog_sweep],
        "view_sweep": [point.model_dump(mode="json") for point in view_sweep],
        "environment": _environment().model_dump(mode="json"),
        "budget_results": [result.model_dump(mode="json") for result in results],
        "overall_pass": all(result.passed for result in results),
    }
    body["report_sha256"] = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    return LatencyReport.model_validate(body)


def write_report_atomic(report: LatencyReport, output_path: Path) -> None:
    """Atomically replace one canonical JSON receipt in its target directory."""
    output = output_path.resolve()
    if output.suffix == ".db":
        raise RefusedBenchmarkPathError("benchmark receipt cannot be written to a database path")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(report.model_dump(mode="json")) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output)
        temporary_path = None
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def synthetic_tickers(scale: SyntheticScale) -> list[str]:
    """The symbols :func:`seed_synthetic_database` creates for ``scale``."""
    return _ticker_symbols(scale.tickers)
