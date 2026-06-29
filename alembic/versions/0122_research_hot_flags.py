"""research_hot_flags — the manual "actively-considering-near-term" signal.

The owner flags a name as hot (a near-term buy candidate they're actively
weighing) to lift its research-budget tier. Time-boxed (``expires_at``) so a
stale flag decays; OR'd into the deterministic tier resolver alongside the
portfolio-weight floor. Append-style: a new flag supersedes by recency.

naive-UTC TEXT stamps; no FK.

Revision ID: 0122_research_hot_flags
Revises: 0121_research_proposals
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0122_research_hot_flags"
down_revision: str | Sequence[str] | None = "0121_research_proposals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if "research_hot_flags" in set(sa.inspect(bind).get_table_names()):
        return  # idempotent
    op.create_table(
        "research_hot_flags",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("set_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
    )
    op.create_index("ix_research_hot_flags_ticker", "research_hot_flags", ["ticker"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "research_hot_flags" not in set(inspector.get_table_names()):
        return
    existing = {idx["name"] for idx in inspector.get_indexes("research_hot_flags")}
    if "ix_research_hot_flags_ticker" in existing:
        op.drop_index("ix_research_hot_flags_ticker", table_name="research_hot_flags")
    op.drop_table("research_hot_flags")
