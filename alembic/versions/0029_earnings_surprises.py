"""earnings_surprises — EPS / Revenue actual-vs-consensus per release.

One row per (ticker, release_date). Populated by `execution/ingest_earnings_surprises.py`
from `data/surprise/<TICKER>_surprises.json` files, which are themselves produced by
`execution/backfill_earnings_surprises.py` via the FMP → yfinance source chain.

Schema:
  ticker                -- the issuer (e.g. WIX)
  release_date          -- ISO date the company reported (NOT the fiscal period end)
  eps_estimate          -- consensus EPS estimate at the time of release
  eps_actual            -- reported EPS
  revenue_estimate      -- consensus revenue estimate
  revenue_actual        -- reported revenue
  eps_surprise_pct      -- (eps_actual − eps_estimate) / |eps_estimate| × 100, rounded 2dp
  revenue_surprise_pct  -- same formula on revenue (nullable when revenue not in source)
  num_analysts_eps      -- size of the consensus pool for EPS (nullable)
  num_analysts_revenue  -- size of the consensus pool for revenue (nullable)
  source_name           -- 'fmp_calendar' | 'yfinance' | future additions
  source_url            -- where the record came from (nullable; FMP is a local file)
  fetched_at            -- UTC timestamp the dispatcher last produced this record

UNIQUE(ticker, release_date) makes the ingest step idempotent; re-running with
fresher data updates in place rather than duplicating. The same date is used as
the merge key in the dispatcher, so a "first source wins" record can be
overwritten by a later run that adds a more authoritative source.

Revenue columns are nullable by design: yfinance — the fallback source — does
not publish historical revenue actual-vs-estimate. After an FMP plan lapse those
fields will start coming back NULL, which downstream consumers (Phase C
scorecard) must handle.

Revision ID: 0029_earnings_surprises
Revises: 0028_tracked_companies_evaluation_list_type
Create Date: 2026-05-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029_earnings_surprises"
down_revision: str | Sequence[str] | None = "0028_tracked_companies_evaluation_list_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "earnings_surprises",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("release_date", sa.String(length=10), nullable=False),
        sa.Column("eps_estimate", sa.Numeric(precision=24, scale=6)),
        sa.Column("eps_actual", sa.Numeric(precision=24, scale=6)),
        sa.Column("revenue_estimate", sa.Numeric(precision=24, scale=6)),
        sa.Column("revenue_actual", sa.Numeric(precision=24, scale=6)),
        sa.Column("eps_surprise_pct", sa.Numeric(precision=12, scale=4)),
        sa.Column("revenue_surprise_pct", sa.Numeric(precision=12, scale=4)),
        sa.Column("num_analysts_eps", sa.Integer()),
        sa.Column("num_analysts_revenue", sa.Integer()),
        sa.Column("source_name", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.String()),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
    )
    op.create_index(
        "ux_earnings_surprises_ticker_release",
        "earnings_surprises",
        ["ticker", "release_date"],
        unique=True,
    )
    # Portfolio-scorecard lookup pattern: pull all records for one ticker, sort by release_date.
    op.create_index(
        "ix_earnings_surprises_ticker",
        "earnings_surprises",
        ["ticker"],
    )


def downgrade() -> None:
    op.drop_index("ix_earnings_surprises_ticker", table_name="earnings_surprises")
    op.drop_index("ux_earnings_surprises_ticker_release", table_name="earnings_surprises")
    op.drop_table("earnings_surprises")
