"""Read/write layer for ``filing_sections`` + ``filing_section_coverage``.

Design rules this module enforces so its callers don't have to:

* **Absence is never silent.** Every write path has a matching coverage write,
  and every read path reports which sources were present, which were absent
  *with a reason*, and which were never attempted. ``PeriodAvailability``
  exists so a consumer physically cannot mistake "only FMP had this period"
  for "both sources agreed".
* **A missing table is a setup error, not an empty result.** Readers raise
  ``HardStopError`` by default and only degrade to empty when the caller opts
  in with ``missing_ok=True`` (the synthesis-lens case, which must drop out
  quietly on a DB that predates the migration). Defaulting the other way is
  how a migration that never ran turns into "this ticker has no filings".
* **Pointers are code-validated, never DB foreign keys**, per the repo's
  FK-poisoning invariant. ``doc_id`` values are checked against ``documents``
  in one batched query before insert.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from filings.models import (
    COVERED_STATUSES,
    CoverageRecord,
    CoverageStatus,
    FilingForm,
    FilingSection,
    FiscalPeriod,
    HardStopError,
    PeriodAvailability,
    SectionSource,
)

log = logging.getLogger(__name__)

SECTIONS_TABLE = "filing_sections"
COVERAGE_TABLE = "filing_section_coverage"


def _now() -> datetime:
    """Naive-UTC stamp, per the repo's datetime convention (aware stamps crash
    comparisons against the rest of the schema)."""
    return datetime.now(UTC).replace(tzinfo=None)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _require_tables(conn: sqlite3.Connection, *, missing_ok: bool) -> bool:
    """True when both tables exist. Raises unless ``missing_ok``."""
    present = table_exists(conn, SECTIONS_TABLE) and table_exists(conn, COVERAGE_TABLE)
    if present:
        return True
    if missing_ok:
        log.info({"event": "filing_sections_tables_absent", "degraded": True})
        return False
    raise HardStopError(
        f"{SECTIONS_TABLE}/{COVERAGE_TABLE} missing — run `alembic upgrade head` "
        "(migration 0198) before ingesting or querying filing sections"
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _validate_doc_ids(conn: sqlite3.Connection, doc_ids: set[int]) -> None:
    """Code-validated pointer check for ``documents.id`` (no DB-level FK).

    Raises ``HardStopError`` on a dangling pointer rather than nulling it out:
    the ingest path resolves every ``doc_id`` from ``documents`` itself, so a
    dangling one means the caller invented it, and silently dropping the link
    would strip provenance from rows that look fully provenanced.
    """
    if not doc_ids:
        return
    if not table_exists(conn, "documents"):
        raise HardStopError("documents table missing — cannot validate doc_id pointers")
    placeholders = ",".join("?" * len(doc_ids))
    found = {
        int(r[0])
        for r in conn.execute(
            f"SELECT id FROM documents WHERE id IN ({placeholders})", tuple(doc_ids)
        ).fetchall()
    }
    dangling = doc_ids - found
    if dangling:
        raise HardStopError(f"doc_id(s) not present in documents: {sorted(dangling)}")


def _prune_superseded(conn: sqlite3.Connection, sections: Sequence[FilingSection]) -> int:
    """Delete rows for these documents that the new partition no longer contains.

    Without this, improving an extractor leaves orphans: when the 20-F splitter
    learned to break Item 3 into its sub-items, the old whole-``Item 3`` row
    survived alongside the new ``Item 3.D``, so the document was stored as two
    overlapping partitions of the same prose. Any diff or change metric run
    over that would double-count. One document holds exactly one partition —
    the newest — so anything not in the incoming batch for a touched
    ``(source, source_ref)`` is superseded and goes.
    """
    by_ref: dict[tuple[str, str], set[tuple[str, int]]] = {}
    for s in sections:
        by_ref.setdefault((s.source.value, s.source_ref), set()).add((s.section_key_raw, s.ordinal))
    deleted = 0
    for (source, source_ref), keep in by_ref.items():
        rows = conn.execute(
            f"SELECT id, section_key_raw, ordinal FROM {SECTIONS_TABLE} "
            "WHERE source = ? AND source_ref = ?",
            (source, source_ref),
        ).fetchall()
        stale = [int(r[0]) for r in rows if (str(r[1]), int(r[2])) not in keep]
        if not stale:
            continue
        conn.executemany(f"DELETE FROM {SECTIONS_TABLE} WHERE id = ?", [(i,) for i in stale])
        deleted += len(stale)
    if deleted:
        log.info({"event": "filing_sections_superseded_rows_pruned", "count": deleted})
    return deleted


def write_sections(conn: sqlite3.Connection, sections: Sequence[FilingSection]) -> int:
    """Upsert sections on (source, source_ref, section_key_raw, ordinal).

    Re-running with the same extractor is a no-op in content terms; re-running
    with an improved extractor replaces that document's partition WHOLLY —
    changed rows are updated in place and rows the new partition dropped are
    deleted (see ``_prune_superseded``), so the store never holds two
    overlapping partitions of one document.

    Returns the number of rows written. Raises ``HardStopError`` on a missing
    table or a dangling ``doc_id``; both are setup/logic errors that must not
    be absorbed mid-backfill.
    """
    if not sections:
        return 0
    _require_tables(conn, missing_ok=False)
    _validate_doc_ids(conn, {s.doc_id for s in sections if s.doc_id is not None})
    _prune_superseded(conn, sections)

    now = _now()
    rows = [
        (
            s.ticker,
            s.source.value,
            s.source_ref,
            s.doc_id,
            s.accession_number,
            s.form.value,
            s.fiscal_year,
            s.fiscal_period.value,
            s.period_end,
            s.filing_date,
            s.section_key_raw,
            s.section_stem,
            s.canonical_id,
            s.title,
            s.ordinal,
            s.text,
            s.text_sha256,
            s.char_len,
            1 if s.key_truncated else 0,
            s.extractor_version,
            now,
        )
        for s in sections
    ]
    conn.executemany(
        f"""
        INSERT INTO {SECTIONS_TABLE}
            (ticker, source, source_ref, doc_id, accession_number, form,
             fiscal_year, fiscal_period, period_end, filing_date,
             section_key_raw, section_stem, canonical_id, title, ordinal,
             text, text_sha256, char_len, key_truncated, extractor_version,
             created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source, source_ref, section_key_raw, ordinal) DO UPDATE SET
            ticker=excluded.ticker,
            doc_id=excluded.doc_id,
            accession_number=excluded.accession_number,
            form=excluded.form,
            fiscal_year=excluded.fiscal_year,
            fiscal_period=excluded.fiscal_period,
            period_end=excluded.period_end,
            filing_date=excluded.filing_date,
            section_stem=excluded.section_stem,
            canonical_id=excluded.canonical_id,
            title=excluded.title,
            text=excluded.text,
            text_sha256=excluded.text_sha256,
            char_len=excluded.char_len,
            key_truncated=excluded.key_truncated,
            extractor_version=excluded.extractor_version,
            created_at=excluded.created_at
        """,
        rows,
    )
    return len(rows)


def record_coverage(conn: sqlite3.Connection, record: CoverageRecord) -> None:
    """Upsert one (ticker, source, form, period, extractor) verdict.

    Uses an explicit NULL-safe lookup rather than ``ON CONFLICT``: SQLite does
    not treat two NULL ``fiscal_year`` values as conflicting in a UNIQUE index,
    so ``ON CONFLICT`` would silently accumulate duplicate rows for exactly the
    periods whose year we failed to resolve — the ones most in need of a single
    clear verdict. Same reasoning, and same fix, as
    ``pipeline.segment_quarterly_coverage.record_coverage``.
    """
    _require_tables(conn, missing_ok=False)
    now = _now()
    existing = conn.execute(
        f"""
        SELECT id FROM {COVERAGE_TABLE}
        WHERE ticker = ? AND source = ? AND form = ? AND fiscal_year IS ?
          AND fiscal_period = ? AND extractor_version = ?
        LIMIT 1
        """,
        (
            record.ticker.upper(),
            record.source.value,
            record.form.value,
            record.fiscal_year,
            record.fiscal_period.value,
            record.extractor_version,
        ),
    ).fetchone()

    if existing is None:
        conn.execute(
            f"""
            INSERT INTO {COVERAGE_TABLE}
                (ticker, source, form, fiscal_year, fiscal_period, source_ref,
                 status, reason_code, detail, sections_written,
                 extractor_version, checked_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record.ticker.upper(),
                record.source.value,
                record.form.value,
                record.fiscal_year,
                record.fiscal_period.value,
                record.source_ref,
                record.status.value,
                record.reason_code,
                record.detail,
                record.sections_written,
                record.extractor_version,
                now,
            ),
        )
        return

    conn.execute(
        f"""
        UPDATE {COVERAGE_TABLE}
        SET source_ref = ?, status = ?, reason_code = ?, detail = ?,
            sections_written = ?, checked_at = ?
        WHERE id = ?
        """,
        (
            record.source_ref,
            record.status.value,
            record.reason_code,
            record.detail,
            record.sections_written,
            now,
            int(existing[0]),
        ),
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _rows_to_sections(rows: Iterable[sqlite3.Row]) -> list[FilingSection]:
    """Rehydrate DB rows into validated models.

    A row that fails validation is dropped with a loud log rather than
    crashing the read: a single corrupt row (hand-edited, or written by an
    older extractor before a constraint tightened) must not make an entire
    ticker unreadable. The drop is counted in the log so it stays visible.
    """
    out: list[FilingSection] = []
    dropped = 0
    for row in rows:
        try:
            out.append(
                FilingSection(
                    ticker=row["ticker"],
                    source=SectionSource(row["source"]),
                    source_ref=row["source_ref"],
                    doc_id=row["doc_id"],
                    accession_number=row["accession_number"],
                    form=FilingForm(row["form"]),
                    fiscal_year=row["fiscal_year"],
                    fiscal_period=FiscalPeriod(row["fiscal_period"]),
                    period_end=row["period_end"],
                    filing_date=row["filing_date"],
                    section_key_raw=row["section_key_raw"],
                    section_stem=row["section_stem"],
                    canonical_id=row["canonical_id"],
                    title=row["title"],
                    ordinal=row["ordinal"],
                    text=row["text"],
                    text_sha256=row["text_sha256"],
                    char_len=row["char_len"],
                    key_truncated=bool(row["key_truncated"]),
                    extractor_version=row["extractor_version"],
                )
            )
        except (ValueError, KeyError, TypeError) as exc:
            dropped += 1
            # sqlite3.Row implements keys() but not __contains__, so membership
            # has to go through the key list here.
            row_id = row["id"] if "id" in row.keys() else None  # noqa: SIM118
            log.warning(
                {
                    "event": "filing_section_row_invalid",
                    "id": row_id,
                    "error": str(exc),
                }
            )
    if dropped:
        log.warning({"event": "filing_sections_rows_dropped", "count": dropped})
    return out


def get_sections(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    source: SectionSource | None = None,
    form: FilingForm | None = None,
    fiscal_year: int | None = None,
    fiscal_period: FiscalPeriod | None = None,
    canonical_id: str | None = None,
    section_stem: str | None = None,
    limit: int | None = None,
    missing_ok: bool = False,
) -> list[FilingSection]:
    """Fetch sections for a ticker under any combination of filters."""
    if not _require_tables(conn, missing_ok=missing_ok):
        return []
    clauses = ["ticker = ?"]
    params: list[object] = [ticker.strip().upper()]
    for column, value in (
        ("source", source.value if source else None),
        ("form", form.value if form else None),
        ("fiscal_period", fiscal_period.value if fiscal_period else None),
        ("canonical_id", canonical_id),
        ("section_stem", section_stem),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    if fiscal_year is not None:
        clauses.append("fiscal_year = ?")
        params.append(fiscal_year)

    sql = (
        f"SELECT * FROM {SECTIONS_TABLE} WHERE {' AND '.join(clauses)} "
        "ORDER BY fiscal_year DESC, fiscal_period DESC, source, ordinal ASC"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.row_factory = prior_factory
    return _rows_to_sections(rows)


def get_coverage(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    form: FilingForm | None = None,
    fiscal_year: int | None = None,
    missing_ok: bool = False,
) -> list[CoverageRecord]:
    """Fetch coverage verdicts for a ticker."""
    if not _require_tables(conn, missing_ok=missing_ok):
        return []
    clauses = ["ticker = ?"]
    params: list[object] = [ticker.strip().upper()]
    if form is not None:
        clauses.append("form = ?")
        params.append(form.value)
    if fiscal_year is not None:
        clauses.append("fiscal_year = ?")
        params.append(fiscal_year)

    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT * FROM {COVERAGE_TABLE} WHERE {' AND '.join(clauses)} "
            "ORDER BY fiscal_year DESC, fiscal_period DESC, source",
            tuple(params),
        ).fetchall()
    finally:
        conn.row_factory = prior_factory

    out: list[CoverageRecord] = []
    for row in rows:
        try:
            out.append(
                CoverageRecord(
                    ticker=row["ticker"],
                    source=SectionSource(row["source"]),
                    form=FilingForm(row["form"]),
                    fiscal_year=row["fiscal_year"],
                    fiscal_period=FiscalPeriod(row["fiscal_period"]),
                    source_ref=row["source_ref"],
                    status=CoverageStatus(row["status"]),
                    reason_code=row["reason_code"],
                    detail=row["detail"],
                    sections_written=row["sections_written"],
                    extractor_version=row["extractor_version"],
                )
            )
        except (ValueError, KeyError, TypeError) as exc:
            log.warning({"event": "filing_coverage_row_invalid", "error": str(exc)})
    return out


def period_availability(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    form: FilingForm | None = None,
    missing_ok: bool = False,
) -> list[PeriodAvailability]:
    """Per-period view of which sources actually produced sections, and why not.

    This is the guard rail for every downstream consumer: it joins what was
    *written* against what was *attempted*, so a period whose coverage row says
    ``ok`` but which holds zero sections reads as absent (not as covered), and
    a period covered by one source is explicitly flagged
    ``is_single_source`` rather than being indistinguishable from a
    two-source period. Recorded mismatches ride along in ``mismatches`` so a
    caller can refuse to diff a period whose sources disagree about what
    filing it even is.
    """
    if not _require_tables(conn, missing_ok=missing_ok):
        return []
    ticker = ticker.strip().upper()

    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        section_clauses = ["ticker = ?"]
        params: list[object] = [ticker]
        if form is not None:
            section_clauses.append("form = ?")
            params.append(form.value)
        section_rows = conn.execute(
            f"""
            SELECT form, fiscal_year, fiscal_period, source, COUNT(*) AS n
            FROM {SECTIONS_TABLE}
            WHERE {" AND ".join(section_clauses)}
            GROUP BY form, fiscal_year, fiscal_period, source
            """,
            tuple(params),
        ).fetchall()
        coverage_rows = conn.execute(
            f"""
            SELECT form, fiscal_year, fiscal_period, source, status, reason_code
            FROM {COVERAGE_TABLE}
            WHERE {" AND ".join(section_clauses)}
            """,
            tuple(params),
        ).fetchall()
    finally:
        conn.row_factory = prior_factory

    buckets: dict[tuple[str, int | None, str], PeriodAvailability] = {}

    def _bucket(form_value: str, year: int | None, period: str) -> PeriodAvailability | None:
        key = (form_value, year, period)
        if key not in buckets:
            try:
                buckets[key] = PeriodAvailability(
                    ticker=ticker,
                    form=FilingForm(form_value),
                    fiscal_year=year,
                    fiscal_period=FiscalPeriod(period),
                )
            except ValueError as exc:
                log.warning({"event": "filing_availability_row_invalid", "error": str(exc)})
                return None
        return buckets[key]

    for row in section_rows:
        bucket = _bucket(row["form"], row["fiscal_year"], row["fiscal_period"])
        if bucket is None:
            continue
        try:
            src = SectionSource(row["source"])
        except ValueError:
            continue
        bucket.sources_present.add(src)
        bucket.section_counts[src.value] = int(row["n"])

    for row in coverage_rows:
        bucket = _bucket(row["form"], row["fiscal_year"], row["fiscal_period"])
        if bucket is None:
            continue
        try:
            src = SectionSource(row["source"])
            status = CoverageStatus(row["status"])
        except ValueError:
            continue
        reason = row["reason_code"] or ""
        if "mismatch" in reason or "mismatch" in status.value:
            bucket.mismatches.append(f"{src.value}:{reason or status.value}")
        # A claimed-covered period with zero stored sections is an absence, not
        # a success — trust the rows, not the claim.
        if src not in bucket.sources_present:
            bucket.absent_sources[src.value] = reason or status.value
        elif status not in COVERED_STATUSES:
            bucket.mismatches.append(f"{src.value}:rows_present_but_status_{status.value}")

    return sorted(
        buckets.values(),
        key=lambda a: (a.fiscal_year or 0, a.fiscal_period.value, a.form.value),
        reverse=True,
    )


def section_timeline(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    canonical_id: str | None = None,
    section_stem: str | None = None,
    source: SectionSource | None = None,
    form: FilingForm | None = None,
    missing_ok: bool = False,
) -> list[FilingSection]:
    """One disclosure's versions across periods, oldest first — the diff input.

    Exactly one of ``canonical_id`` (cross-form concept, e.g. ``risk_factors``,
    which follows a company across a 10-K→20-F change) or ``section_stem``
    (normalized FMP note key) must be given. Ordering is oldest-first because
    every consumer of this walks consecutive pairs forward in time; returning
    newest-first here would make an off-by-one silently invert every diff.
    """
    if (canonical_id is None) == (section_stem is None):
        raise HardStopError("section_timeline requires exactly one of canonical_id / section_stem")
    rows = get_sections(
        conn,
        ticker,
        canonical_id=canonical_id,
        section_stem=section_stem,
        source=source,
        form=form,
        missing_ok=missing_ok,
    )
    _period_rank = {"FY": 5, "Q4": 4, "Q3": 3, "Q2": 2, "Q1": 1}
    return sorted(
        rows,
        key=lambda s: (
            s.fiscal_year or 0,
            _period_rank.get(s.fiscal_period.value, 0),
            s.source.value,
            s.ordinal,
        ),
    )
