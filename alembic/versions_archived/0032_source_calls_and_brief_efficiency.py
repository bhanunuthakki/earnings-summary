"""source_calls_and_brief_efficiency — generic source-call provenance log + brief gating columns.

Adds three things:

1. `source_calls` table — log of every call we made to an external data source
   (FMP, yfinance, SEC XBRL, etc.). Powers future intelligent routing: which
   sources are reliable per kind, what tiers expose what endpoints over time,
   typical latencies. Today it's a write-many / read-rarely log; the read-side
   will be a follow-up when we build the router.

2. `tracked_companies.last_built_at` — timestamp of the last successful brief
   build. Used by daily_fetch_and_brief to gate `evaluation` tickers to a
   weekly cadence (portfolio remains daily).

3. `tracked_companies.last_brief_hash` — content hash of the inputs that fed
   the last brief (financial_facts max period_end + kpi_facts max period_end +
   transcripts set + live price bucket). Same hash + recent build = skip rebuild.

All three are additive. No table drops, no constraint changes.

Revision ID: 0032_source_calls_and_brief_efficiency
Revises: 0031_drop_dead_tables
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0032_source_calls_and_brief_efficiency"
down_revision: str | Sequence[str] | None = "0031_drop_dead_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_calls",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_name", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=True),
        sa.Column(
            "called_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("http_code", sa.Integer(), nullable=True),
        sa.Column("record_count", sa.Integer(), nullable=True),
        sa.Column("notes", sa.String(length=256), nullable=True),
    )
    op.create_index(
        "ix_source_calls_lookup",
        "source_calls",
        ["source_name", "kind", "ticker", "called_at"],
    )

    # Brief-gating columns. Both default to NULL — existing rows will be treated
    # as "never built" by daily_fetch_and_brief, which is correct.
    bind = op.get_bind()
    existing_cols = {c["name"] for c in sa.inspect(bind).get_columns("tracked_companies")}
    if "last_built_at" not in existing_cols:
        op.add_column(
            "tracked_companies",
            sa.Column("last_built_at", sa.DateTime(), nullable=True),
        )
    if "last_brief_hash" not in existing_cols:
        op.add_column(
            "tracked_companies",
            sa.Column("last_brief_hash", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("tracked_companies") as batch:
        batch.drop_column("last_brief_hash")
        batch.drop_column("last_built_at")
    op.drop_index("ix_source_calls_lookup", table_name="source_calls")
    op.drop_table("source_calls")
