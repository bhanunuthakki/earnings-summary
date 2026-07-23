"""wealth_context_snapshot_history — append-only aggregates-only wealth timeseries.

Personal Investment Partner PRD §7.6 / §11.5 (P0-F, owner-ratified 2026-07-23,
amending the 2026-07-19 "nothing balance-shaped persisted" ruling): a daily
observation row of household-level AGGREGATES pulled live from the portfolio
tracker and the wealthplan plan — total net worth, liquid/investable balances,
allocation by account class, and the label-only cash-need band. No item-level
holdings, no transaction detail, no compensation figures, ever. Rows are
observations, not facts: never ratified, never quoted by the LLM as owner
statements, and never a substitute for a live read when the source APIs are up.

Revision ID: 0187_wealth_context_snapshot_history
Revises: 0186_risk_snapshot_history_input_sha
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0187_wealth_context_snapshot_history"
down_revision: str | Sequence[str] | None = "0186_risk_snapshot_history_input_sha"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "wealth_context_snapshot_history" in insp.get_table_names():
        return  # idempotent
    op.create_table(
        "wealth_context_snapshot_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Text(), nullable=False, server_default=sa.text("'bhanu'")),
        sa.Column("captured_at", sa.Text(), nullable=False),
        # Effective as-of of the combined observation plus per-source stamps
        # (PRD §11.2: "current" is never inferred from run time alone).
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("tracker_as_of", sa.Text(), nullable=True),
        sa.Column("wealthplan_as_of", sa.Text(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=False, server_default=sa.text("'USD'")),
        # Scalar aggregates that trend queries read directly.
        sa.Column("net_worth_total", sa.Float(), nullable=True),
        sa.Column("liquid_total", sa.Float(), nullable=True),
        sa.Column("investable_total", sa.Float(), nullable=True),
        sa.Column("home_equity", sa.Float(), nullable=True),
        # Everything else (allocation by account class, cash-need band +
        # label-only reasons, per-field source provenance) rides in the
        # validated WealthContextSnapshot JSON.
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("input_sha", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("json_valid(snapshot_json)", name="ck_wealth_snapshot_json_valid"),
        sa.UniqueConstraint("input_sha", name="uq_wealth_snapshot_input_sha"),
    )
    op.create_index(
        "ix_wealth_snapshot_history_user_time",
        "wealth_context_snapshot_history",
        ["user_id", "as_of"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "wealth_context_snapshot_history" not in insp.get_table_names():
        return
    op.drop_table("wealth_context_snapshot_history")
