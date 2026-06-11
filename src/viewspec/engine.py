"""Execute a ViewSpec against the fact tables (master build P5.1).

Deterministic and LLM-free: every cell comes from the canonical tier-aware
loaders in ``timeseries.loaders`` (the same row picks the reports use) and
every fin/kpi cell carries the provenance of its winning fact row, so the
renderer can chip each number. Segment cells render unchipped for now —
the junction's provenance is period-level and joins documents through
segment_periods; wire it when a surface needs it.

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

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from report.models import CellSource
from timeseries.loaders import (
    SourcedObservation,
    load_financial_series,
    load_financial_series_with_provenance,
    load_kpi_series_with_provenance,
    load_segment_junction_series,
)
from viewspec.spec import MetricRef, ViewSpec

_QUARTERLY_PERIOD_TYPES: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4")
_ANNUAL_PERIOD_TYPES: tuple[str, ...] = ("FY",)

# (calendar year, quarter 1..4) for quarterly views; (year, 0) for annual —
# one orderable key shape either way.
_Bucket = tuple[int, int]


@dataclass(slots=True)
class ViewCell:
    """One rendered cell: the transformed value, the underlying level, and
    the provenance of the level's winning fact row (None for seg cells and
    for buckets the transform could not compute)."""

    value: float | None
    raw: float | None
    source: CellSource | None


@dataclass(slots=True)
class ViewRow:
    """One pivot row — a (ticker, metric) series across the period axis."""

    ticker: str
    metric: MetricRef
    label: str
    unit: str | None
    cells: list[ViewCell]


@dataclass(slots=True)
class ViewResult:
    """What `execute_view` hands the renderer."""

    spec: ViewSpec
    period_labels: list[str]
    rows: list[ViewRow]
    warnings: list[str]


def _bucket_label(bucket: _Bucket, cadence: str) -> str:
    year, q = bucket
    if cadence == "annual" or q == 0:
        return f"FY{year}"
    return f"Q{q}'{str(year)[2:]}"


def _cell_source(prov: dict[str, object]) -> CellSource:
    """SourcedObservation.provenance → the chip model. ``source_doc_id`` is
    documents.id, which is exactly what CellSource.doc_id deep-links."""

    def _s(key: str) -> str | None:
        v = prov.get(key)
        return str(v) if v is not None else None

    doc_raw = prov.get("source_doc_id")
    return CellSource(
        source=str(prov.get("source") or "unknown"),
        fetched_at=_s("fetched_at"),
        source_url=_s("source_url"),
        doc_type=_s("doc_type"),
        accession_number=_s("accession_number"),
        filing_date=_s("filing_date"),
        locator=_s("locator"),
        doc_id=doc_raw if isinstance(doc_raw, int) else None,
    )


def _load_row_data(
    ticker: str,
    metric: MetricRef,
    cadence: str,
    *,
    db_path: Path | None,
    repo_root: Path | None,
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
            sourced = load_financial_series_with_provenance(
                ticker, metric.key, repo_root, db_path=db_path, period_types=period_types
            )
        else:
            sourced = load_kpi_series_with_provenance(
                ticker, metric.key, repo_root, db_path=db_path, period_types=period_types
            )
        for ob in sourced:
            b = _to_bucket(ob.period_end.year, ob.period_end.month, cadence)
            cells[b] = ViewCell(value=None, raw=ob.value, source=_cell_source(ob.provenance))
            if ob.unit:
                unit = ob.unit
        return cells, unit
    # seg: values only (period-level provenance not wired yet — see module
    # docstring).
    dims = [(metric.dim_type or "", metric.dim_name or "")]
    series = load_segment_junction_series(
        ticker, dims, metric.key, repo_root, db_path=db_path, period_types=period_types
    )
    for obs in series:
        b = _to_bucket(obs.period_end.year, obs.period_end.month, cadence)
        cells[b] = ViewCell(value=None, raw=obs.value, source=None)
    return cells, unit


def _to_bucket(year: int, month: int, cadence: str) -> _Bucket:
    if cadence == "annual":
        return (year, 0)
    return (year, (month - 1) // 3 + 1)


def _lookback(bucket: _Bucket, years: int) -> _Bucket:
    return (bucket[0] - years, bucket[1])


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
    # Metric-major ordering: the same metric's tickers sit adjacent, which is
    # the comparison the pivot exists for.
    for metric in spec.metrics:
        for ticker in spec.tickers:
            cells, unit = _load_row_data(
                ticker, metric, spec.cadence, db_path=db_path, repo_root=repo_root
            )
            if not cells:
                warnings.append(f"{ticker}: no data for {metric.token()}")
                continue
            raw_rows.append((ticker, metric, cells, unit))

    # Margin divisor: fin:revenue per ticker, loaded once.
    revenue_by_ticker: dict[str, dict[_Bucket, float]] = {}
    if spec.transform == "margin":
        period_types = _ANNUAL_PERIOD_TYPES if spec.cadence == "annual" else _QUARTERLY_PERIOD_TYPES
        for ticker in spec.tickers:
            rev = load_financial_series(
                ticker, "revenue", repo_root, db_path=db_path, period_types=period_types
            )
            revenue_by_ticker[ticker] = {
                _to_bucket(o.period_end.year, o.period_end.month, spec.cadence): o.value
                for o in rev
            }
            if not revenue_by_ticker[ticker]:
                warnings.append(f"{ticker}: no fin:revenue series — margin cells empty")

    all_buckets: set[_Bucket] = set()
    for _t, _m, cells, _u in raw_rows:
        all_buckets.update(cells)
    display_buckets = sorted(all_buckets)[-spec.periods :]

    rows: list[ViewRow] = []
    for ticker, metric, cells, unit in raw_rows:
        out_cells: list[ViewCell] = []
        for b in display_buckets:
            cell = cells.get(b)
            raw = cell.raw if cell is not None else None
            src = cell.source if cell is not None else None
            value: float | None = None
            if raw is not None:
                if spec.transform == "level":
                    value = raw
                elif spec.transform == "yoy":
                    prior_cell = cells.get(_lookback(b, 1))
                    prior = prior_cell.raw if prior_cell is not None else None
                    if prior is not None and prior != 0:
                        value = (raw / prior - 1) * 100
                elif spec.transform == "cagr":
                    base_cell = cells.get(_lookback(b, spec.cagr_years))
                    base = base_cell.raw if base_cell is not None else None
                    if base is not None and base > 0 and raw > 0:
                        value = ((raw / base) ** (1 / spec.cagr_years) - 1) * 100
                elif spec.transform == "margin":
                    rev = revenue_by_ticker.get(ticker, {}).get(b)
                    if rev is not None and rev != 0:
                        value = raw / rev * 100
            out_cells.append(ViewCell(value=value, raw=raw, source=src))
        rows.append(
            ViewRow(
                ticker=ticker,
                metric=metric,
                label=f"{ticker} · {metric.label}",
                unit=unit,
                cells=out_cells,
            )
        )

    return ViewResult(
        spec=spec,
        period_labels=[_bucket_label(b, spec.cadence) for b in display_buckets],
        rows=rows,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Metric catalog (the builder UI's picker content)
# ---------------------------------------------------------------------------


def metric_catalog(
    db_path: Path,
    tickers: list[str],
    *,
    limit_per_domain: int = 300,
) -> dict[str, list[dict[str, object]]]:
    """What can be plotted for these tickers: distinct financial line items,
    KPI names (with at least one fact row), and segment slices — each as
    ``{"token": ..., "label": ..., "tickers": n}`` ordered by how many of
    the requested tickers carry it. Best-effort: missing DB/tables yield
    empty domain lists.
    """
    out: dict[str, list[dict[str, object]]] = {"fin": [], "kpi": [], "seg": []}
    symbols = [t.strip().upper() for t in tickers if t.strip()]
    if not symbols or not db_path.exists():
        return out
    marks = ",".join("?" * len(symbols))
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error:
        return out
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            out["fin"] = _catalog_query(
                conn,
                f"""
                SELECT line_item AS k, COUNT(DISTINCT ticker) AS n
                FROM financial_facts WHERE ticker IN ({marks})
                GROUP BY line_item ORDER BY n DESC, k ASC LIMIT ?
                """,
                (*symbols, limit_per_domain),
                lambda r: {
                    "token": f"fin:{r['k']}",
                    "label": str(r["k"]),
                    "tickers": int(r["n"]),
                },
            )
            out["kpi"] = _catalog_query(
                conn,
                f"""
                SELECT kd.name AS k, COUNT(DISTINCT kf.ticker) AS n
                FROM kpi_facts kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
                WHERE kf.ticker IN ({marks})
                GROUP BY kd.name ORDER BY n DESC, k ASC LIMIT ?
                """,
                (*symbols, limit_per_domain),
                lambda r: {
                    "token": f"kpi:{r['k']}",
                    "label": str(r["k"]),
                    "tickers": int(r["n"]),
                },
            )
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
                    "token": f"seg:{r['dt']}:{r['dn']}:{r['m']}",
                    "label": f"{r['dn']} {r['m']} ({r['dt']})",
                    "tickers": int(r["n"]),
                },
            )
    finally:
        conn.close()
    return out


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
