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
metric: both resolve to one definition, then the append-only fact chain points
the audited row at the lower-tier incumbent so resolution stays deterministic.

Routes through `persist_manifest` (the single kpi_facts write interface) with an
`ir_spreadsheet` extractor tag rather than inserting directly.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import sqlite3
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import TypeVar

from compute.kpi_resolver import canonical_metric_name
from ir_pipeline.config import IrConfig, SheetKpi
from ir_pipeline.spreadsheet import SpreadsheetCellRead, SpreadsheetParseResult
from ir_pipeline.spreadsheet_capture import capture_spreadsheet_parse
from models.documents import SourceType, tier_for_source_type
from models.facts import Currency, FiscalPeriodType, LegacyEscapeHatch, Unit
from models.kpis import DefinitionOrigin
from models.runs import StageStatus
from pipeline.invocation_fingerprint import file_fingerprint, payload_sha256
from pipeline.kpi_persistence import (
    KpiExtractionManifest,
    KpiValue,
    persist_manifest,
)
from pipeline.kpi_semantics import unclassified_kpi_context
from pipeline.run_accounting import end_run, start_run
from provenance.evidence_backfill import ensure_legacy_document_evidence

_DOC_TYPE = "ir_historical_spreadsheet"

# Historical value-only callers do not carry worksheet coordinates. The typed
# source handoff supplies an exact locator without changing their compatibility path.
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


def _ensure_spreadsheet_document(
    conn: sqlite3.Connection, ticker: str, path: Path, *, raw: bytes | None = None
) -> int:
    """Reuse exact spreadsheet bytes only within their registered issuer."""
    raw = path.read_bytes() if raw is None else raw
    sha = hashlib.sha256(raw).hexdigest()
    existing = conn.execute("SELECT id, ticker FROM documents WHERE sha256 = ?", (sha,)).fetchone()
    if existing is not None:
        if str(existing["ticker"]).upper() != ticker.upper():
            raise ValueError("spreadsheet document hash is already registered to another issuer")
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
    if _documents_has_column(conn, "source_quality_tier"):
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


_ParsedValue = TypeVar("_ParsedValue", float, SpreadsheetCellRead)


def _canonicalize_parsed(
    conn: sqlite3.Connection,
    ticker: str,
    config: IrConfig,
    parsed: dict[str, dict[_dt.datetime, _ParsedValue]],
) -> tuple[
    dict[str, dict[_dt.datetime, _ParsedValue]],
    dict[str, Unit],
    dict[str, DefinitionOrigin],
    dict[str, Currency | None],
]:
    """Resolve all specs once, then merge selected series and validate currencies.

    Later configured names and periods overwrite; analyst origin wins a shared
    canonical name. Even absent or overwritten specs require valid currencies.
    """
    resolved: list[tuple[SheetKpi, str, Unit, DefinitionOrigin]] = []
    for spec in config.spreadsheet_kpis:
        unit = _unit(spec.unit)
        capture = spec.origin == "capture"
        name = (
            canonical_metric_name(conn, ticker, spec.kpi_name, unit) if capture else spec.kpi_name
        )
        origin = DefinitionOrigin.CAPTURE if capture else DefinitionOrigin.ANALYST
        resolved.append((spec, name, unit, origin))
    by_config_name = {spec.kpi_name: (name, unit, origin) for spec, name, unit, origin in resolved}
    series_by_name: dict[str, dict[_dt.datetime, _ParsedValue]] = {}
    unit_by_name: dict[str, Unit] = {}
    origins: dict[str, DefinitionOrigin] = {}
    for raw_name, series in parsed.items():
        res = by_config_name.get(raw_name)
        if res is None:
            continue
        name, unit, origin = res
        series_by_name.setdefault(name, {}).update(series)
        unit_by_name[name] = unit
        if origin is DefinitionOrigin.ANALYST or name not in origins:
            origins[name] = origin
    currencies: dict[str, Currency | None] = {}
    monetary = {Unit.ACTUAL, Unit.THOUSANDS, Unit.MILLIONS, Unit.BILLIONS}
    for spec, name, unit, _ in resolved:
        if unit not in monetary:
            currencies.setdefault(name, None)
            continue
        declared_currency = spec.currency or config.reporting_currency
        if declared_currency is None:
            raise ValueError(
                f"monetary IR spreadsheet KPI {spec.kpi_name!r} has no explicit currency"
            )
        try:
            currency = Currency(declared_currency.upper())
        except ValueError as exc:
            raise ValueError(
                f"IR spreadsheet KPI {spec.kpi_name!r} has unsupported currency {declared_currency!r}"
            ) from exc
        existing = currencies.get(name)
        if existing is not None and existing is not currency:
            raise ValueError(f"canonical IR spreadsheet KPI {name!r} has conflicting currencies")
        currencies[name] = currency
    return series_by_name, unit_by_name, origins, currencies


def ingest_spreadsheet_kpis(
    conn: sqlite3.Connection,
    ticker: str,
    config: IrConfig,
    parsed: dict[str, dict[_dt.datetime, float]] | SpreadsheetParseResult,
    source_path: Path,
    *,
    repo_root: Path | None = None,
) -> tuple[int, int]:
    """Persist `parsed` ({kpi: {period_end: value}}). Returns (rows_inserted, doc_id).

    Capture-row labels are canonicalized (and tagged ``origin='capture'``) before
    persisting; analyst rows are untouched. ``conn.row_factory`` must be
    ``sqlite3.Row``.
    """
    source_bytes: bytes | None = None
    if isinstance(parsed, SpreadsheetParseResult):
        source_bytes = source_path.read_bytes()
        if (
            hashlib.sha256(source_bytes).hexdigest() != parsed.workbook_sha256
            or len(source_bytes) != parsed.workbook_bytes_size
        ):
            raise ValueError("spreadsheet bytes changed after parsing")
        if payload_sha256(asdict(config)) != parsed.config_sha256:
            raise ValueError("spreadsheet config changed after parsing")
    doc_id = _ensure_spreadsheet_document(conn, ticker, source_path, raw=source_bytes)
    if _has_trigger(conn, "trg_kpi_facts_observation_insert"):
        if repo_root is None:
            raise ValueError("post-cutover IR spreadsheet ingest requires repo_root")
        ensure_legacy_document_evidence(conn, repo_root=repo_root, document_id=doc_id)
        if isinstance(parsed, SpreadsheetParseResult):
            # Keep the acquired document and its evidence anchor durable before
            # the separate, atomic parser capture begins.
            conn.commit()
            document_version_id = f"legacy-doc-{doc_id}"
            version = conn.execute(
                "SELECT issuer_id,blob_sha256 FROM evidence_document_versions "
                "WHERE document_version_id=? AND legacy_document_id=?",
                (document_version_id, doc_id),
            ).fetchone()
            if version is None or str(version[1]) != parsed.workbook_sha256:
                raise ValueError("spreadsheet document evidence version is unavailable")
            capture_spreadsheet_parse(
                conn,
                ticker=ticker,
                issuer_id=str(version[0]),
                document_version_id=document_version_id,
                config=config,
                parsed=parsed,
            )
    if isinstance(parsed, SpreadsheetParseResult):
        series_by_name, unit_by_name, origins, currency_by_name = _canonicalize_parsed(
            conn, ticker, config, parsed.selected_series()
        )
        numeric_parsed = parsed.numeric_projection()
    else:
        series_by_name, unit_by_name, origins, currency_by_name = _canonicalize_parsed(
            conn, ticker, config, parsed
        )
        numeric_parsed = parsed

    # Regroup to one manifest per period_end (each manifest = a period's KPIs).
    by_period: dict[_dt.datetime, dict[str, float | SpreadsheetCellRead]] = {}
    for kpi, series in series_by_name.items():
        for period, val in series.items():
            by_period.setdefault(period, {})[kpi] = val

    run_id = start_run(
        conn,
        directive="ingest_ir_spreadsheet",
        ticker_scope=[ticker],
        invocation_inputs={
            "document_id": doc_id,
            "source": file_fingerprint(source_path)
            if source_bytes is None
            else {
                "path": source_path.resolve().as_posix(),
                "exists": True,
                "bytes": len(source_bytes),
                "sha256": hashlib.sha256(source_bytes).hexdigest(),
            },
            "config_sha256": payload_sha256(asdict(config)),
            "parsed_sha256": payload_sha256(
                {
                    name: {period.isoformat(): value for period, value in sorted(series.items())}
                    for name, series in sorted(numeric_parsed.items())
                }
            ),
        },
        deduplicate_completed=True,
    )
    inserted = 0
    try:
        for period_end, kpis in sorted(by_period.items()):
            values = [
                KpiValue(
                    name=name,
                    value=Decimal(
                        str(val.scaled_value if isinstance(val, SpreadsheetCellRead) else val)
                    ),
                    unit=unit_by_name.get(name, Unit.ACTUAL),
                    currency=currency_by_name.get(name),
                    confidence=1.0,
                    locator=(
                        val.locator
                        if isinstance(val, SpreadsheetCellRead) and val.locator is not None
                        else _NO_LOCATOR
                    ),
                    semantic_context=(
                        unclassified_kpi_context(
                            metric_name_as_reported=val.raw_label or val.spec.kpi_name,
                            reported_period_end=period_end.date(),
                        )
                        if isinstance(val, SpreadsheetCellRead)
                        else None
                    ),
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

    except Exception as exc:
        # Prior periods are committed; discard only this period's pending writes
        # before end_run commits the failure accounting.
        conn.rollback()
        end_run(conn, run_id, StageStatus.FAILED, error_summary=f"{type(exc).__name__}: {exc}")
        raise
    end_run(conn, run_id, StageStatus.OK)
    return inserted, doc_id


def _has_trigger(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _documents_has_column(conn: sqlite3.Connection, column: str) -> bool:
    return any(str(row[1]) == column for row in conn.execute("PRAGMA table_info(documents)"))


def _unit(raw: str) -> Unit:
    try:
        return Unit(raw)
    except ValueError:
        return Unit.ACTUAL
