"""Per-ticker IR auto-fetch status + coverage helpers.

Backs two surfaces:

  * the command-center "IR Docs" panel — which roster names have auto-fetched IR
    documents vs. which need a manual pull, with the last crawl outcome + reason;
  * the failing-crawler rescan (``discover_ir_documents_all --only-failing``) —
    the roster names with zero registered IR documents.

Two data sources, deliberately split:

  * ``documents`` (``source_type='ir_doc'``) is authoritative for COVERAGE — how
    many IR docs a ticker has, the latest period covered, and when the newest was
    registered. Read live so a manual upload shows up immediately.
  * ``ir_fetch_status`` is the crawl-HEALTH log — when the batch last tried a
    ticker, the outcome, and (for a name that found nothing) why. Written by the
    batch after each attempt.

Every reader is tolerant of a missing table / missing DB (returns empty), and the
writer swallows any sqlite error — IR-fetch bookkeeping must never abort the batch
or break a dashboard render.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

# The "briefed" active universe — portfolio + evaluation (mirrors
# db.BRIEFED_LIST_TYPES; duplicated as a literal to keep this module import-light
# and test-isolated, like the batch orchestrator's roster SQL).
BRIEFED_LIST_TYPES: tuple[str, ...] = ("portfolio", "evaluation")


def _now_iso() -> str:
    """Naive-UTC ISO timestamp (repo convention — never store an aware stamp)."""
    return datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


@dataclass(slots=True, frozen=True)
class IrFetchStatus:
    """One ticker's most recent crawl outcome (the ``ir_fetch_status`` row)."""

    ticker: str
    last_attempt_at: str
    last_status: str  # ok | failed | skipped
    discovered: int | None
    downloaded: int | None
    reason: str | None
    updated_at: str


@dataclass(slots=True, frozen=True)
class IrCoverageRow:
    """A roster ticker joined with its live IR-doc coverage + crawl health."""

    ticker: str
    list_type: str
    name: str
    doc_count: int
    latest_period: str | None  # MAX(period_end) over its ir_doc rows
    last_doc_at: str | None  # MAX(fetched_at) over its ir_doc rows
    status: IrFetchStatus | None  # crawl health, if ever attempted

    @property
    def has_docs(self) -> bool:
        return self.doc_count > 0


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def record_attempt(
    db_path: Path,
    ticker: str,
    *,
    status: str,
    discovered: int | None,
    downloaded: int | None,
    reason: str | None = None,
    now_iso: str | None = None,
) -> bool:
    """Upsert one ticker's latest crawl outcome. Returns ok.

    Best-effort: a missing DB / missing table / any sqlite error is swallowed so
    the batch never aborts on bookkeeping. ``now_iso`` is injectable for tests.
    """
    if not db_path.exists():
        return False
    ts = now_iso or _now_iso()
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            if not _table_exists(conn, "ir_fetch_status"):
                return False
            conn.execute(
                """
                INSERT INTO ir_fetch_status
                    (ticker, last_attempt_at, last_status, discovered, downloaded,
                     reason, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    last_attempt_at = excluded.last_attempt_at,
                    last_status     = excluded.last_status,
                    discovered      = excluded.discovered,
                    downloaded      = excluded.downloaded,
                    reason          = excluded.reason,
                    updated_at      = excluded.updated_at
                """,
                (ticker.upper(), ts, status, discovered, downloaded, reason, ts),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return True


def load_statuses(db_path: Path) -> dict[str, IrFetchStatus]:
    """Every ``ir_fetch_status`` row, keyed by upper-cased ticker."""
    out: dict[str, IrFetchStatus] = {}
    if not db_path.exists():
        return out
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            if not _table_exists(conn, "ir_fetch_status"):
                return out
            rows = conn.execute("SELECT * FROM ir_fetch_status").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return out
    for r in rows:
        ticker = str(r["ticker"]).upper()
        out[ticker] = IrFetchStatus(
            ticker=ticker,
            last_attempt_at=str(r["last_attempt_at"]),
            last_status=str(r["last_status"]),
            discovered=cast("int | None", r["discovered"]),
            downloaded=cast("int | None", r["downloaded"]),
            reason=cast("str | None", r["reason"]),
            updated_at=str(r["updated_at"]),
        )
    return out


def ir_doc_coverage(db_path: Path) -> dict[str, tuple[int, str | None, str | None]]:
    """Per-ticker ``(doc_count, latest_period_end, last_fetched_at)`` over the
    auto-fetched IR documents (``documents.source_type='ir_doc'``).

    Read live so a manual upload is reflected immediately. Empty if the table is
    absent (a fresh DB) — callers treat an absent ticker as zero coverage.
    """
    out: dict[str, tuple[int, str | None, str | None]] = {}
    if not db_path.exists():
        return out
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            if not _table_exists(conn, "documents"):
                return out
            rows = conn.execute(
                """
                SELECT ticker,
                       COUNT(*)        AS doc_count,
                       MAX(period_end) AS latest_period,
                       MAX(fetched_at) AS last_doc_at
                FROM documents
                WHERE source_type = 'ir_doc'
                GROUP BY ticker
                """
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return out
    for r in rows:
        latest = r["latest_period"]
        last_at = r["last_doc_at"]
        out[str(r["ticker"]).upper()] = (
            int(cast("int", r["doc_count"]) or 0),
            str(latest) if latest else None,
            str(last_at) if last_at else None,
        )
    return out


def briefed_roster(db_path: Path) -> list[tuple[str, str, str]]:
    """``(ticker, list_type, name)`` for active portfolio+evaluation names, sorted.

    The shared roster for the IR-coverage panel + the rescan target set. Empty if
    the DB / table is absent.
    """
    out: list[tuple[str, str, str]] = []
    if not db_path.exists():
        return out
    placeholders = ", ".join("?" for _ in BRIEFED_LIST_TYPES)
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            if not _table_exists(conn, "tracked_companies"):
                return out
            rows = conn.execute(
                f"SELECT ticker, list_type, name FROM tracked_companies "
                f"WHERE list_type IN ({placeholders}) AND archived_at IS NULL "
                f"ORDER BY ticker",
                BRIEFED_LIST_TYPES,
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return out
    for r in rows:
        name = r["name"]
        out.append((str(r["ticker"]).upper(), str(r["list_type"]), str(name) if name else ""))
    return out


def coverage_rows(db_path: Path, roster: list[tuple[str, str, str]]) -> list[IrCoverageRow]:
    """Join each roster ``(ticker, list_type, name)`` with live IR-doc coverage +
    the last crawl-health status. Names with no docs sort first (the work-list)."""
    cov = ir_doc_coverage(db_path)
    statuses = load_statuses(db_path)
    out: list[IrCoverageRow] = []
    for ticker, list_type, name in roster:
        t = ticker.upper()
        doc_count, latest_period, last_doc_at = cov.get(t, (0, None, None))
        out.append(
            IrCoverageRow(
                ticker=t,
                list_type=list_type,
                name=name,
                doc_count=doc_count,
                latest_period=latest_period,
                last_doc_at=last_doc_at,
                status=statuses.get(t),
            )
        )
    # Gaps first (most actionable), then by ticker.
    out.sort(key=lambda r: (r.has_docs, r.ticker))
    return out


def gap_tickers(db_path: Path, roster: list[str]) -> list[str]:
    """Roster names with ZERO auto-fetched IR docs — the rescan target set."""
    cov = ir_doc_coverage(db_path)
    return [t.upper() for t in roster if cov.get(t.upper(), (0, None, None))[0] == 0]
