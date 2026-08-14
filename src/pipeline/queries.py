"""Common reads against documents / kpi_definitions / tracked_companies.

Read-only helpers that consumers (orchestrators, computers, synthesizers)
use so they don't all hand-roll SQL. Returns Pydantic models, not raw rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from identity import DEFAULT_USER_ID
from models.companies import Company, FilingRegime, InstrumentType, ListType
from models.documents import DocType, SourceType
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


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


# Default scope for `tracked_companies_for_user`: only the lists the user actively
# analyzes. Index members and ETFs (added by the FMP universe backfill) are
# excluded so that bulk consumers like `extract_facts.py --all` don't fan out
# parsing/analysis over thousands of names. Callers needing the wider universe
# pass an explicit `list_types=` set.
ANALYZED_LIST_TYPES: frozenset[ListType] = frozenset(
    {ListType.PORTFOLIO, ListType.WATCHLIST, ListType.EVALUATION}
)

# Subset of ANALYZED_LIST_TYPES that produces full briefs (portfolio-flavor or
# eval-flavor). Watchlist names are a holding pen — no auto-brief. Use this for
# brief-producing pipelines: company description, build_artifacts.py defaults.
BRIEFED_LIST_TYPES: frozenset[ListType] = frozenset({ListType.PORTFOLIO, ListType.EVALUATION})
BRIEFED_LIST_TYPE_VALUES: tuple[str, ...] = tuple(
    sorted(list_type.value for list_type in BRIEFED_LIST_TYPES)
)
ANALYZED_LIST_TYPE_VALUES: tuple[str, ...] = tuple(
    sorted(list_type.value for list_type in ANALYZED_LIST_TYPES)
)


def tracked_companies_for_user(
    conn: sqlite3.Connection,
    user_id: str = DEFAULT_USER_ID,
    only_classified: bool = True,
    list_types: frozenset[ListType] = ANALYZED_LIST_TYPES,
    include_archived: bool = False,
) -> list[Company]:
    """Return Company rows for user, scoped to `list_types`.

    `only_classified` filters out NULL instrument_type. Default `list_types` is
    portfolio + watchlist so bulk callers don't iterate the index-member universe;
    pass `frozenset(ListType)` (or any superset) to opt in to the broader scope.

    `include_archived` defaults False so archived names disappear from analysis,
    DCF, thesis, refresh-queue scopes; pass True only for admin/diagnostic flows.
    """
    if not list_types:
        raise ValueError("list_types must be non-empty")
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in list_types)
    sql = (
        "SELECT id, user_id, ticker, name, list_type, added_at, sec_validated, "
        "       ir_url, instrument_type, filing_regime, fiscal_year_end, "
        "       fmp_data_saved, fmp_data_upto "
        "FROM tracked_companies WHERE user_id = ? "
        f"AND list_type IN ({placeholders})"
    )
    if only_classified:
        sql += " AND instrument_type IS NOT NULL"
    if not include_archived:
        sql += " AND archived_at IS NULL"
    sql += " ORDER BY list_type, ticker"
    cur.execute(sql, (user_id, *(lt.value for lt in list_types)))
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
    """Open the pipeline's writer-capable portfolio connection.

    Pipeline entrypoints use this helper for both reads and mutations, so its
    historical contract remains writer-capable while inheriting the central
    concurrency, integrity, and schema-compatibility policy.
    """
    return connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER)
