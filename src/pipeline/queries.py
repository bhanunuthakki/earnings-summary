"""Common reads against documents / kpi_definitions / tracked_companies.

Read-only helpers that consumers (orchestrators, computers, synthesizers)
use so they don't all hand-roll SQL. Returns Pydantic models, not raw rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from models.companies import Company, FilingRegime, InstrumentType, ListType
from models.documents import DocType, SourceType


def latest_document_for(
    conn: sqlite3.Connection,
    ticker: str,
    doc_type: DocType,
) -> dict[str, object] | None:
    """Return the most-recent documents row matching (ticker, doc_type), or None.

    Returns a dict (not yet Document model — DocType enum doesn't include all
    50+ FMP doc_types loaded into the documents table; building strict Document
    instances would mean every read is at the mercy of enum coverage).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT id, ticker, source_type, doc_type, period_start, period_end, "
        "       file_path, sha256, fetched_at, fetch_status, http_code, "
        "       raw_bytes_size, source_url, parent_document_id "
        "FROM documents WHERE ticker = ? AND doc_type = ? "
        "ORDER BY fetched_at DESC LIMIT 1",
        (ticker.upper(), doc_type.value),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def documents_for(
    conn: sqlite3.Connection,
    ticker: str,
    doc_type: DocType | None = None,
    source_type: SourceType | None = None,
) -> list[dict[str, object]]:
    """List documents matching (ticker[, doc_type][, source_type]) ordered by period_end DESC."""
    sql = (
        "SELECT id, ticker, source_type, doc_type, period_start, period_end, "
        "       file_path, sha256, fetched_at, fetch_status, http_code, "
        "       raw_bytes_size, source_url, parent_document_id "
        "FROM documents WHERE ticker = ? "
    )
    params: list[str] = [ticker.upper()]
    if doc_type is not None:
        sql += "AND doc_type = ? "
        params.append(doc_type.value)
    if source_type is not None:
        sql += "AND source_type = ? "
        params.append(source_type.value)
    sql += "ORDER BY period_end DESC NULLS LAST, fetched_at DESC"
    cur = conn.cursor()
    cur.execute(sql, tuple(params))
    return [dict(r) for r in cur.fetchall()]


def tracked_companies_for_user(
    conn: sqlite3.Connection,
    user_id: int = 1,
    only_classified: bool = True,
) -> list[Company]:
    """Return Company rows for user. only_classified filters out NULL instrument_type."""
    cur = conn.cursor()
    sql = (
        "SELECT id, user_id, ticker, name, list_type, added_at, sec_validated, "
        "       ir_url, instrument_type, filing_regime, fiscal_year_end, "
        "       fmp_data_saved, fmp_data_upto "
        "FROM tracked_companies WHERE user_id = ?"
    )
    if only_classified:
        sql += " AND instrument_type IS NOT NULL"
    sql += " ORDER BY list_type, ticker"
    cur.execute(sql, (user_id,))
    out: list[Company] = []
    for row in cur.fetchall():
        out.append(
            Company(
                id=row["id"],
                user_id=row["user_id"],
                ticker=row["ticker"],
                name=row["name"],
                list_type=ListType(row["list_type"]),
                sec_validated=bool(row["sec_validated"]),
                ir_url=row["ir_url"],
                instrument_type=(
                    InstrumentType(row["instrument_type"]) if row["instrument_type"] else None
                ),
                filing_regime=(
                    FilingRegime(row["filing_regime"]) if row["filing_regime"] else None
                ),
                fiscal_year_end=row["fiscal_year_end"],
                fmp_data_saved=bool(row["fmp_data_saved"]),
                fmp_data_upto=row["fmp_data_upto"],
            )
        )
    return out


def kpi_definitions_for(conn: sqlite3.Connection, ticker: str) -> list[dict[str, object]]:
    """Return kpi_definitions rows for ticker (raw dicts; consumers may project to KpiDefinition)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, ticker, name, unit, primary_source, fallback_source, "
        "       ir_url, threshold_tier, threshold_low, threshold_high, notes "
        "FROM kpi_definitions WHERE ticker = ?",
        (ticker.upper(),),
    )
    return [dict(r) for r in cur.fetchall()]


def project_root_relative(path: str) -> Path:
    """Resolve a documents.file_path (project-root-relative) to an absolute Path."""
    project_root = Path(__file__).resolve().parents[2]
    return project_root / path


def documents_count_by_doc_type(
    conn: sqlite3.Connection,
    source_type: SourceType | None = None,
) -> list[tuple[str, int]]:
    """Histogram of (doc_type, count) for diagnostics. Empty list if no rows."""
    cur = conn.cursor()
    if source_type is None:
        cur.execute(
            "SELECT doc_type, COUNT(*) AS n FROM documents GROUP BY doc_type ORDER BY n DESC"
        )
    else:
        cur.execute(
            "SELECT doc_type, COUNT(*) AS n FROM documents "
            "WHERE source_type = ? GROUP BY doc_type ORDER BY n DESC",
            (source_type.value,),
        )
    return [(r["doc_type"], r["n"]) for r in cur.fetchall()]


def open_db(db_path: str | Path) -> sqlite3.Connection:
    """Open a portfolio.db connection with row_factory set to sqlite3.Row."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn
