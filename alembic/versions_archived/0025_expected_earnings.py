"""expected_earnings — calendar watcher input queue for the daily worker.

One row per (ticker, expected_date) seen on the FMP earnings calendar (with
yfinance fallback). The daily fetch worker drains this table: for any ticker
with expected_date <= today and no `financial_facts` row for the matching
fiscal period yet, it kicks off the tier-chain fetch (FMP → SEC → IR/LLM).

Schema:
  ticker              -- the issuer (e.g. META)
  expected_date       -- ISO date the earnings event is scheduled
  fiscal_period_end   -- ISO date the period covers (nullable, populated when known)
  fiscal_period_label -- "Q1 2026" style label (nullable)
  detected_source     -- 'fmp' / 'yfinance' / 'manual'
  first_seen_at       -- when the watcher first saw this row
  last_seen_at        -- when the watcher last confirmed this row
  fact_seen_at        -- when financial_facts first appeared for this period

The fact_seen_at column lets the worker detect a "still pending" earnings
release vs. a completed one without an expensive JOIN against financial_facts.

UNIQUE(ticker, expected_date) keeps the table stable across watcher runs;
duplicate detections refresh last_seen_at rather than inserting.

Revision ID: 0025_expected_earnings
Revises: 0024_dcf_runs_extras
Create Date: 2026-05-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_expected_earnings"
down_revision: str | Sequence[str] | None = "0024_dcf_runs_extras"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "expected_earnings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("expected_date", sa.String(length=10), nullable=False),
        sa.Column("fiscal_period_end", sa.String(length=10)),
        sa.Column("fiscal_period_label", sa.String(length=16)),
        sa.Column("detected_source", sa.String(length=16), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("fact_seen_at", sa.DateTime()),
    )
    op.create_index(
        "ux_expected_earnings_ticker_date",
        "expected_earnings",
        ["ticker", "expected_date"],
        unique=True,
    )
    op.create_index(
        "ix_expected_earnings_pending",
        "expected_earnings",
        ["expected_date"],
        sqlite_where=sa.text("fact_seen_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_expected_earnings_pending", table_name="expected_earnings")
    op.drop_index("ux_expected_earnings_ticker_date", table_name="expected_earnings")
    op.drop_table("expected_earnings")
