"""expected_earnings revival — the canonical next-earnings calendar table.

Recreates the table 0031 dropped (verbatim 0025 schema). 0031's "never
SELECTed" audit grepped this repo only; the portfolio-tracker sibling repo's
read-only bridge (services/earnings_summary.py::_next_earnings) has SELECTed
`ticker, MIN(expected_date)` from this table since 2026-05 and has been
silently degrading to "no earnings date" ever since the drop.

The revived table is the single earnings-calendar pipeline every reader
consumes:

  writer   execution/refresh_expected_earnings.py (daily 05:45, same cron
           slot as the FMP cache fetch) — resolves each active ticker via
           sources.earnings_calendar (FMP cache -> yfinance fallback). The
           FMP /stable/earnings endpoint started 402ing on the free tier
           2026-06-10, so the yfinance rung is now load-bearing.
  readers  src/dashboard/digest.py (Upcoming this week — real dates, +91d
           estimate only when a ticker has no calendar row),
           src/pipeline/research_cockpit.py (Next ER column),
           portfolio-tracker's earnings_summary bridge (unchanged).

Revision ID: 0082_expected_earnings_revival
Revises: 0081_discovery_candidates
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0082_expected_earnings_revival"
down_revision: str | Sequence[str] | None = "0081_discovery_candidates"
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
