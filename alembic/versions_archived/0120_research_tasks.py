"""research_tasks — The Ledger Phase-1 detection→proposal spine.

A wondering detected in an owner-authored musing (``wondering_detect``) lands one
``research_tasks`` row in status ``proposed`` with an INERT chip — it NEVER
auto-runs. The owner taps one button (``POST /research/<id>/run``), which resolves
the budget tier, runs the two-pass research (network-isolated fetch → network-less
narrate), and produces a reviewable ``research_proposals`` row.

  status: proposed → running → drafted → {approved, rejected, superseded}

NO real FK ``REFERENCES`` (code-level RI, mirroring ``insight_notes`` 0118):
``note_id`` is a plain INT into ``analyst_notes``. naive-UTC TEXT stamps.

Revision ID: 0120_research_tasks
Revises: 0119_ledger_synthesis_budget
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0120_research_tasks"
down_revision: str | Sequence[str] | None = "0119_ledger_synthesis_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if "research_tasks" in set(sa.inspect(bind).get_table_names()):
        return  # idempotent
    op.create_table(
        "research_tasks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("note_id", sa.Integer(), nullable=True),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'proposed'")),
        sa.Column("budget_tier", sa.Text(), nullable=True),
        sa.Column("budget_usd", sa.Float(), nullable=True),
        sa.Column("adversarial_verdict", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_index("ix_research_tasks_status", "research_tasks", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "research_tasks" not in set(inspector.get_table_names()):
        return
    existing = {idx["name"] for idx in inspector.get_indexes("research_tasks")}
    if "ix_research_tasks_status" in existing:
        op.drop_index("ix_research_tasks_status", table_name="research_tasks")
    op.drop_table("research_tasks")
