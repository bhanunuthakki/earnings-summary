"""Persist parsed IR-spreadsheet KPI series into kpi_facts.

The spreadsheet is the issuer's own audited historical-data file, so its values
ingest at IR_DOC tier (FMP_NORMALIZED) — above the LLM_EXTRACTED brief/press
values. The restatement detector inside `persist_manifest` links each new row to
the brief incumbent via `supersedes_id` (the spreadsheet doc is fetched later),
so the report loaders prefer the spreadsheet figure while the brief value
survives for time-travel.

Routes through `persist_manifest` (the single kpi_facts write interface) with an
`ir_spreadsheet` extractor tag rather than inserting directly.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import sqlite3
from decimal import Decimal
from pathlib import Path

from ir_pipeline.config import IrConfig
from models.documents import SourceType, tier_for_source_type
from models.facts import FiscalPeriodType, Unit
from pipeline.kpi_persistence import (
    KpiExtractionManifest,
    KpiValue,
    persist_manifest,
)
from pipeline.restatement_detector import _table_has_column
from pipeline.run_accounting import start_run

_DOC_TYPE = "ir_historical_spreadsheet"


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


def ingest_spreadsheet_kpis(
    conn: sqlite3.Connection,
    ticker: str,
    config: IrConfig,
    parsed: dict[str, dict[_dt.datetime, float]],
    source_path: Path,
) -> tuple[int, int]:
    """Persist `parsed` ({kpi: {period_end: value}}). Returns (rows_inserted, doc_id)."""
    doc_id = _ensure_spreadsheet_document(conn, ticker, source_path)
    unit_by_kpi = {s.kpi_name: s.unit for s in config.spreadsheet_kpis}

    # Regroup to one manifest per period_end (each manifest = a period's KPIs).
    by_period: dict[_dt.datetime, dict[str, float]] = {}
    for kpi, series in parsed.items():
        for period, val in series.items():
            by_period.setdefault(period, {})[kpi] = val

    run_id = start_run(conn, directive="ingest_ir_spreadsheet", ticker_scope=[ticker])
    inserted = 0
    for period_end, kpis in sorted(by_period.items()):
        values = [
            KpiValue(
                name=name,
                value=Decimal(str(val)),
                unit=_unit(unit_by_kpi.get(name, "actual")),
                confidence=1.0,
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
            values=values,
        )
        result = persist_manifest(conn, run_id=run_id, manifest=manifest)
        inserted += result.inserted

    _supersede_llm_incumbents(conn, ticker, doc_id, parsed)
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
