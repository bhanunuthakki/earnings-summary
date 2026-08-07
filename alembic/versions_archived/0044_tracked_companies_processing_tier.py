"""tracked_companies_processing_tier — per-ticker pipeline depth tier.

Adds `processing_tier VARCHAR(8) DEFAULT 'P3'` to tracked_companies. The tier
gates how much of the analytical pipeline runs for a given ticker, so a
1000-name watchlist doesn't trigger the same expensive LLM lens chain as the
20-name portfolio.

Tier semantics (the values are advisory; consumers decide what to skip):
  P1 = full pipeline: facts + KPI extraction + LLM lenses + bear case +
       saydo + thesis evaluator + insights. Default for portfolio.
  P2 = facts + KPI extraction + bear case (no expensive lenses or insights).
       Default for watchlist + evaluation.
  P3 = facts + KPI extraction only — the bare-minimum brief shape.
       Default for everything else (index_member / etf / none).

Backfill rules at upgrade time:
  list_type = 'portfolio'                          → P1
  list_type IN ('watchlist', 'evaluation')         → P2
  list_type IN ('etf', 'index_member', 'none')     → P3 (matches default)

The column is intentionally NOT a CHECK-constrained enum so onboarding can
write an unknown tier value without a migration; consumers normalize on
read. The default 'P3' is the conservative floor: a new row inserted by
some long-tail code path that doesn't yet set the tier won't accidentally
pull a portfolio-grade pipeline.

Revision ID: 0044_tracked_companies_processing_tier
Revises: 0043_self_update_triggers
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0044_tracked_companies_processing_tier"
down_revision: str | Sequence[str] | None = "0043_self_update_triggers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("tracked_companies")}
    if "processing_tier" not in cols:
        op.add_column(
            "tracked_companies",
            sa.Column(
                "processing_tier",
                sa.String(length=8),
                nullable=False,
                server_default=sa.text("'P3'"),
            ),
        )

    # Backfill from list_type. Run unconditionally — idempotent because
    # WHERE clauses only touch rows that need updating, and re-runs are
    # cheap on a small table.
    bind.execute(
        sa.text("UPDATE tracked_companies SET processing_tier = 'P1' WHERE list_type = 'portfolio'"),
    )
    bind.execute(
        sa.text(
            "UPDATE tracked_companies SET processing_tier = 'P2' "
            "WHERE list_type IN ('watchlist', 'evaluation')",
        ),
    )
    bind.execute(
        sa.text(
            "UPDATE tracked_companies SET processing_tier = 'P3' "
            "WHERE list_type IN ('etf', 'index_member', 'none') "
            "OR list_type IS NULL",
        ),
    )

    # Partial index for the common drain pattern: scan-portfolio-tier-active.
    inspector = sa.inspect(bind)
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("tracked_companies")}
    if "ix_tracked_companies_processing_tier" not in existing_indexes:
        op.create_index(
            "ix_tracked_companies_processing_tier",
            "tracked_companies",
            ["processing_tier"],
            sqlite_where=sa.text("archived_at IS NULL"),
        )


def downgrade() -> None:
    op.drop_index(
        "ix_tracked_companies_processing_tier", table_name="tracked_companies",
    )
    with op.batch_alter_table("tracked_companies") as batch:
        batch.drop_column("processing_tier")
