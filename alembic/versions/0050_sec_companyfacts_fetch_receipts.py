"""Per-ticker SEC CompanyFacts completion receipts and conditional-GET validators.

One row per ticker records the last COMPLETED companyfacts ingest: the payload
content identity (sha256), the HTTP validators SEC served with it (ETag /
Last-Modified), and the run that completed it. Rows are written in the SAME
write transaction as the snapshot registration and fact walk, so a receipt
row exists exactly when that payload's ingest committed — an interrupted or
failed ingest leaves no receipt, and the next run reprocesses that ticker only.
"""

from __future__ import annotations

from alembic import op

revision = "0050_sec_companyfacts_fetch_receipts"
down_revision = "0049_current_fact_selection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE sec_companyfacts_fetch_receipts (
        ticker TEXT PRIMARY KEY,
        normalized_cik TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
        etag TEXT,
        last_modified TEXT,
        payload_fetched_at TEXT NOT NULL,
        ingest_completed_at TEXT NOT NULL,
        run_id TEXT NOT NULL
    )"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE sec_companyfacts_fetch_receipts")
