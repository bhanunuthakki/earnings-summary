"""§8 Provenance & data quality — coverage matrix + source_doc audit + open issues."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from report.models import (
    CoverageRow,
    ProvenanceSection,
    SectionStatus,
    SourceDocRow,
)
from report.sections._common import has_table, missing, open_repo_db


def build(ticker: str, repo_root: Path) -> ProvenanceSection:
    conn = open_repo_db(repo_root)
    if conn is None:
        return ProvenanceSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="PERSIST(init_db)",
                fix_command="alembic upgrade head",
                detail="No portfolio.db at data/portfolio.db",
            ),
        )

    coverage = _coverage(conn, ticker)
    source_docs = _source_docs(conn, ticker)
    open_issues = _open_validation_issues(conn, ticker)
    conn.close()

    if not coverage and not source_docs:
        return ProvenanceSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="INGEST",
                fix_command=f"python execution/onboard_ticker.py --ticker {ticker.upper()}",
                detail="No artifacts or documents recorded for this ticker.",
            ),
        )

    return ProvenanceSection(
        status=SectionStatus.OK,
        coverage=coverage,
        source_docs=source_docs,
        open_validation_issues=open_issues,
    )


def _coverage(conn: sqlite3.Connection, ticker: str) -> list[CoverageRow]:
    if not has_table(conn, "quarterly_artifacts"):
        return []
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT year, quarter, has_audio_file, has_transcript_file, has_release_file,
               has_slides_file, step_saydo_analyzed, step_llm_summarized
        FROM quarterly_artifacts
        WHERE ticker = ?
        ORDER BY year DESC, quarter DESC
        """,
        (ticker.upper(),),
    )
    return [
        CoverageRow(
            quarter=str(r["quarter"]),
            year=int(r["year"]),
            has_audio_file=bool(r["has_audio_file"]),
            has_transcript_file=bool(r["has_transcript_file"]),
            has_release_file=bool(r["has_release_file"]),
            has_slides_file=bool(r["has_slides_file"]),
            step_saydo_analyzed=bool(r["step_saydo_analyzed"]),
            step_llm_summarized=bool(r["step_llm_summarized"]),
        )
        for r in cursor.fetchall()
    ]


def _source_docs(conn: sqlite3.Connection, ticker: str) -> list[SourceDocRow]:
    if not has_table(conn, "documents"):
        return []
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT doc_type, period_end, file_path, sha256, fetched_at
        FROM documents
        WHERE ticker = ?
        ORDER BY period_end DESC, doc_type
        """,
        (ticker.upper(),),
    )
    return [
        SourceDocRow(
            doc_type=str(r["doc_type"]),
            period_end=str(r["period_end"])[:10] if r["period_end"] else None,
            file_path=str(r["file_path"]),
            sha256=str(r["sha256"]) if r["sha256"] else None,
            fetched_at=str(r["fetched_at"]) if r["fetched_at"] else None,
        )
        for r in cursor.fetchall()
    ]


def _open_validation_issues(conn: sqlite3.Connection, ticker: str) -> int:
    if not has_table(conn, "validation_issues"):
        return 0
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT COUNT(*) AS n
        FROM validation_issues
        WHERE ticker = ? AND resolved_at IS NULL
        """,
        (ticker.upper(),),
    )
    return int(cursor.fetchone()["n"])
