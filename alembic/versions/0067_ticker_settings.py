"""ticker_settings — per-ticker dashboard-owned settings.

One knob today: ``bypass_budget`` — the persistent "always ignore LLM budget caps
for this ticker's builds" flag, set from the dashboard's per-ticker Refresh panel
and read by ``build_artifacts`` when resolving ``force_budget_bypass`` for
``build_report`` (ORed with the one-shot ``--force-budget-bypass`` CLI flag). See
``scratch/plans/llm_budget_dashboard_plan.md`` §0.4 / PR5.

Revision ID: 0067_ticker_settings
Revises: 0066_llm_budget_on_exceed
Create Date: 2026-06-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0067_ticker_settings"
down_revision: str | Sequence[str] | None = "0066_llm_budget_on_exceed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ticker_settings" in inspector.get_table_names():
        return  # idempotent

    op.create_table(
        "ticker_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column(
            "bypass_budget", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),  # 0/1
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("ticker", name="uq_ticker_settings_ticker"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ticker_settings" not in inspector.get_table_names():
        return
    op.drop_table("ticker_settings")
