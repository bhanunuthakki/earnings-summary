"""Per-ticker SEC CompanyFacts fetch receipts.

One row per ticker in ``sec_companyfacts_fetch_receipts`` (alembic 0050)
records the last COMPLETED companyfacts ingest:

- the payload content identity (``payload_sha256``) — the receipt is keyed on
  content, never on a timestamp-bearing run id, per the pipeline DAG's
  repeat-safety contract;
- the HTTP validators (``etag`` / ``last_modified``) SEC served with those
  bytes, replayed as ``If-None-Match`` / ``If-Modified-Since`` on the next
  conditional GET;
- the retrieval/commit clocks and the completing run id.

The row is written inside the SAME write transaction as the snapshot
registration and the fact walk, so a receipt row exists exactly when that
payload's ingest committed. An interrupted or failed ingest leaves no receipt,
so a retry reprocesses only the incomplete tickers; a completed ticker's
receipt plus its content digest is the resumable skip authority.

The table is optional by design: synthetic fixtures that predate 0050 simply
have no validators, degrade to an unconditional GET, and still get the
digest short-circuit from the content-addressed snapshot document rows.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

_RECEIPTS_TABLE = "sec_companyfacts_fetch_receipts"


@dataclass(frozen=True)
class CompanyFactsValidators:
    """Conditional-GET validators from the last completed fetch of a ticker."""

    etag: str | None
    last_modified: str | None

    @property
    def present(self) -> bool:
        """True when at least one validator can seed a conditional request."""
        return bool(self.etag) or bool(self.last_modified)


@dataclass(frozen=True)
class CompanyFactsFetchReceipt:
    """The completed-ingest receipt for one ticker's last companyfacts payload."""

    ticker: str
    normalized_cik: str
    payload_sha256: str
    etag: str | None
    last_modified: str | None
    payload_fetched_at: datetime
    ingest_completed_at: datetime
    run_id: str

    @property
    def validators(self) -> CompanyFactsValidators:
        return CompanyFactsValidators(etag=self.etag, last_modified=self.last_modified)


def fetch_receipts_table_present(conn: sqlite3.Connection) -> bool:
    """True when the 0050 receipts table exists on this connection's schema."""

    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_RECEIPTS_TABLE,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def load_companyfacts_fetch_receipt(
    conn: sqlite3.Connection,
    *,
    ticker: str,
) -> CompanyFactsFetchReceipt | None:
    """Return the ticker's completed-ingest receipt, or None when absent.

    Absence is not an error state: it is exactly the 'this ticker has no
    completed payload yet' signal that makes a retry reprocess the ticker.
    """

    if not fetch_receipts_table_present(conn):
        return None
    row = conn.execute(
        "SELECT ticker, normalized_cik, payload_sha256, etag, last_modified, "
        "payload_fetched_at, ingest_completed_at, run_id "
        f"FROM {_RECEIPTS_TABLE} WHERE ticker = ?",  # nosec B608 -- fixed internal table name
        (ticker.upper(),),
    ).fetchone()
    if row is None:
        return None
    return CompanyFactsFetchReceipt(
        ticker=str(row[0]),
        normalized_cik=str(row[1]),
        payload_sha256=str(row[2]),
        etag=None if row[3] is None else str(row[3]),
        last_modified=None if row[4] is None else str(row[4]),
        payload_fetched_at=datetime.fromisoformat(str(row[5])),
        ingest_completed_at=datetime.fromisoformat(str(row[6])),
        run_id=str(row[7]),
    )


def record_companyfacts_ingest_receipt(
    conn: sqlite3.Connection,
    receipt: CompanyFactsFetchReceipt,
) -> bool:
    """Upsert one ticker's completion receipt; the caller owns the transaction.

    Joins the ingest's write transaction so the receipt commits exactly when
    (and only when) the snapshot registration and fact walk for the same
    payload digest commit. Returns True when the receipt was written; False
    when this schema predates the 0050 table — the ingest itself stays
    correct (the next run simply reprocesses from bytes instead of skipping),
    so an auxiliary-table absence must never fail a committed fact walk.
    """

    if not fetch_receipts_table_present(conn):
        return False
    conn.execute(
        f"INSERT INTO {_RECEIPTS_TABLE} "  # nosec B608 -- fixed internal table name
        "(ticker, normalized_cik, payload_sha256, etag, last_modified, "
        "payload_fetched_at, ingest_completed_at, run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(ticker) DO UPDATE SET "
        "normalized_cik = excluded.normalized_cik, "
        "payload_sha256 = excluded.payload_sha256, "
        "etag = excluded.etag, "
        "last_modified = excluded.last_modified, "
        "payload_fetched_at = excluded.payload_fetched_at, "
        "ingest_completed_at = excluded.ingest_completed_at, "
        "run_id = excluded.run_id",
        (
            receipt.ticker.upper(),
            receipt.normalized_cik,
            receipt.payload_sha256,
            receipt.etag,
            receipt.last_modified,
            receipt.payload_fetched_at.isoformat(),
            receipt.ingest_completed_at.isoformat(),
            receipt.run_id,
        ),
    )
    return True


def refresh_companyfacts_receipt_validators(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    etag: str | None,
    last_modified: str | None,
    payload_fetched_at: datetime,
) -> bool:
    """Refresh a completed receipt's validators after an unchanged-payload GET.

    A 200 whose bytes match the completed digest can still rotate the server's
    ETag; storing the fresh validators keeps the next run's conditional GET
    returning 304 instead of falling back to a full download. The caller owns
    the transaction. Returns True when a receipt row was updated; False when
    no receipt exists (the ticker then keeps its unconditional fetch shape).
    """

    if not fetch_receipts_table_present(conn):
        return False
    cursor = conn.execute(
        f"UPDATE {_RECEIPTS_TABLE} "  # nosec B608 -- fixed internal table name
        "SET etag = ?, last_modified = ?, payload_fetched_at = ? WHERE ticker = ?",
        (etag, last_modified, payload_fetched_at.isoformat(), ticker.upper()),
    )
    return cursor.rowcount > 0
