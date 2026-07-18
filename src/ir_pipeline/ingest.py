"""Persist parsed IR-spreadsheet KPI series into kpi_facts.

The spreadsheet is the issuer's own audited historical-data file, so its values
ingest at IR_DOC tier (FMP_NORMALIZED) — above the LLM_EXTRACTED brief/press
values. The restatement detector inside `persist_manifest` links each new row to
the brief incumbent via `supersedes_id` (the spreadsheet doc is fetched later),
so the report loaders prefer the spreadsheet figure while the brief value
survives for time-travel.

The config carries two layers of ``SheetKpi`` (capture-every-number program):
curated ``analyst`` rows keep their canonical ``kpi_name`` verbatim, while
auto-mapped ``capture`` rows carry a RAW spreadsheet label that is routed through
``compute.kpi_resolver.canonical_metric_name`` HERE (the write path, where a live
DB connection exists) — so a captured row joins an existing series or mints a
clean ``origin='capture'`` definition. Canonicalizing on write is also what lets
an audited spreadsheet value cleanly supersede S3's broad LLM value for the SAME
metric: both resolve to one definition, then `_supersede_llm_incumbents` drops the
lower-tier LLM row.

Routes through `persist_manifest` (the single kpi_facts write interface) with an
`ir_spreadsheet` extractor tag rather than inserting directly.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import sqlite3
from decimal import Decimal
from pathlib import Path

from compute.kpi_resolver import canonical_metric_name
from ir_pipeline.config import IrConfig, SheetKpi
from models.documents import SourceType, tier_for_source_type
from models.facts import FiscalPeriodType, LegacyEscapeHatch, Unit
from models.kpis import DefinitionOrigin
from pipeline.kpi_persistence import (
    KpiExtractionManifest,
    KpiValue,
    persist_manifest,
)
from pipeline.restatement_detector import _table_has_column
from pipeline.run_accounting import start_run

_DOC_TYPE = "ir_historical_spreadsheet"

# A parsed spreadsheet cell has no JSON/PDF position to anchor a
# FactLocator to (documented exception, docs/design/
# provenance_clickthrough.md §5.3) -- the cell's (row, column) identity
# lives in the source .xlsx, which this reader doesn't carry through.
_NO_LOCATOR = LegacyEscapeHatch(
    reason=(
        "IR historical spreadsheet cells have no JSON/PDF position captured "
        "by this reader -- the source .xlsx row/column identity is not "
        "threaded through to persist time (documented exception)"
    )
)


def _fiscal_period_type(period_end: _dt.datetime) -> FiscalPeriodType:
    """Calendar-quarter mapping from a period-end month (NU et al. are calendar-FY)."""
    return FiscalPeriodType(f"Q{(period_end.month - 1) // 3 + 1}")


def _ensure_spreadsheet_document(conn: sqlite3.Connection, ticker: str, path: Path) -> int:
    """Insert (idempotently, sha256-keyed) the documents row for the spreadsheet."""
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    existing = conn.execute("SELECT id FROM documents WHERE sha256 = ?", (sha,)).fetchone()
    if existing is not None:
        return int(existing["id"])
    common = (
        ticker.upper(),
        SourceType.IR_DOC.value,
        _DOC_TYPE,
        None,  # period_end: a multi-quarter file spans periods
        str(path).replace("\\", "/"),
        sha,
        _dt.datetime.now(_dt.UTC),
        "ok",
        len(raw),
    )
    if _table_has_column(conn, "documents", "source_quality_tier"):
        tier = tier_for_source_type(SourceType.IR_DOC).value
        cur = conn.execute(
            "INSERT INTO documents (ticker, source_type, doc_type, period_end, "
            "file_path, sha256, fetched_at, fetch_status, raw_bytes_size, "
            "source_quality_tier) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*common, tier),
        )
    else:
        cur = conn.execute(
            "INSERT INTO documents (ticker, source_type, doc_type, period_end, "
            "file_path, sha256, fetched_at, fetch_status, raw_bytes_size) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            common,
        )
    conn.commit()
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def _resolve_definition(
    conn: sqlite3.Connection, ticker: str, spec: SheetKpi
) -> tuple[str, Unit, DefinitionOrigin]:
    """Resolve one ``SheetKpi`` to its (canonical name, unit, origin) for write.

    A curated ``analyst`` row keeps its ``kpi_name`` verbatim (it already IS the
    canonical name the report charts read) at ANALYST origin. A ``capture`` row's
    raw label is canonicalized via ``canonical_metric_name`` — reusing an existing
    series when one safely matches, else minting a clean CAPTURE name. Requires
    ``conn.row_factory = sqlite3.Row`` (canonical_metric_name reads rows by key).
    """
    unit = _unit(spec.unit)
    if spec.origin == "capture":
        name = canonical_metric_name(conn, ticker, spec.kpi_name, unit)
        return name, unit, DefinitionOrigin.CAPTURE
    return spec.kpi_name, unit, DefinitionOrigin.ANALYST


def _canonicalize_parsed(
    conn: sqlite3.Connection,
    ticker: str,
    config: IrConfig,
    parsed: dict[str, dict[_dt.datetime, float]],
) -> tuple[
    dict[str, dict[_dt.datetime, float]],
    dict[str, Unit],
    dict[str, DefinitionOrigin],
]:
    """Re-key ``parsed`` (keyed by ``SheetKpi.kpi_name``) by CANONICAL name.

    Returns ``(series_by_name, unit_by_name, origins)`` all keyed by the resolved
    ``kpi_definitions.name``. Two source rows that canonicalize together merge
    (later periods overwrite); an analyst origin always wins over capture for a
    shared name so a curated series is never demoted to the long tail.
    """
    resolved = {
        spec.kpi_name: _resolve_definition(conn, ticker, spec) for spec in config.spreadsheet_kpis
    }
    series_by_name: dict[str, dict[_dt.datetime, float]] = {}
    unit_by_name: dict[str, Unit] = {}
    origins: dict[str, DefinitionOrigin] = {}
    for raw_name, series in parsed.items():
        res = resolved.get(raw_name)
        if res is None:
            continue
        name, unit, origin = res
        series_by_name.setdefault(name, {}).update(series)
        unit_by_name[name] = unit
        if origin is DefinitionOrigin.ANALYST or name not in origins:
            origins[name] = origin
    return series_by_name, unit_by_name, origins


def ingest_spreadsheet_kpis(
    conn: sqlite3.Connection,
    ticker: str,
    config: IrConfig,
    parsed: dict[str, dict[_dt.datetime, float]],
    source_path: Path,
) -> tuple[int, int]:
    """Persist `parsed` ({kpi: {period_end: value}}). Returns (rows_inserted, doc_id).

    Capture-row labels are canonicalized (and tagged ``origin='capture'``) before
    persisting; analyst rows are untouched. ``conn.row_factory`` must be
    ``sqlite3.Row``.
    """
    doc_id = _ensure_spreadsheet_document(conn, ticker, source_path)
    series_by_name, unit_by_name, origins = _canonicalize_parsed(conn, ticker, config, parsed)

    # Regroup to one manifest per period_end (each manifest = a period's KPIs).
    by_period: dict[_dt.datetime, dict[str, float]] = {}
    for kpi, series in series_by_name.items():
        for period, val in series.items():
            by_period.setdefault(period, {})[kpi] = val

    run_id = start_run(conn, directive="ingest_ir_spreadsheet", ticker_scope=[ticker])
    inserted = 0
    for period_end, kpis in sorted(by_period.items()):
        values = [
            KpiValue(
                name=name,
                value=Decimal(str(val)),
                unit=unit_by_name.get(name, Unit.ACTUAL),
                confidence=1.0,
                locator=_NO_LOCATOR,
            )
            for name, val in kpis.items()
        ]
        manifest = KpiExtractionManifest(
            ticker=ticker.upper(),
            period_end=period_end,
            fiscal_period_type=_fiscal_period_type(period_end),
            source_doc_id=doc_id,
            primary_source=SourceType.IR_DOC,
            model_name=None,
            extracted_by="ir_spreadsheet",
            origins=origins,
            values=values,
        )
        result = persist_manifest(conn, run_id=run_id, manifest=manifest)
        inserted += result.inserted

    _supersede_llm_incumbents(conn, ticker, doc_id, series_by_name)
    return inserted, doc_id


def _supersede_llm_incumbents(
    conn: sqlite3.Connection,
    ticker: str,
    doc_id: int,
    parsed: dict[str, dict[_dt.datetime, float]],
) -> None:
    """Delete the LLM brief/press rows the spreadsheet now restates.

    The spreadsheet (IR_DOC tier) is authoritative for the KPIs it carries, so
    its values supersede the LLM_EXTRACTED incumbents for the exact (definition,
    period) pairs it covers — leaving a single row per logical key for every
    consumer (charts, break-rule display, the thesis evaluator). Mirrors
    purge_duplicate_kpi_facts (keep the highest-tier/latest source); FMP/SEC rows
    (extracted_by not LIKE 'llm%') are deliberately left untouched.
    """
    for kpi_name, series in parsed.items():
        row = conn.execute(
            "SELECT id FROM kpi_definitions WHERE ticker = ? AND name = ?",
            (ticker.upper(), kpi_name),
        ).fetchone()
        if row is None:
            continue
        def_id = int(row["id"])
        for period_end in series:
            conn.execute(
                "DELETE FROM kpi_facts WHERE ticker = ? AND kpi_definition_id = ? "
                "AND period_end = ? AND source_doc_id != ? AND extracted_by LIKE 'llm%'",
                (ticker.upper(), def_id, period_end, doc_id),
            )
    conn.commit()


def _unit(raw: str) -> Unit:
    try:
        return Unit(raw)
    except ValueError:
        return Unit.ACTUAL
