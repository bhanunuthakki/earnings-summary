"""llm_budgets.on_exceed — per-purpose cap-exceeded mode (skip | block | warn).

Replaces the binary ``hard_block`` bool with a three-way mode that drives what
happens when a purpose is at/over its monthly cap:

  skip  — forgo the LLM call (no spend), mark the section "forgone due to
          budget", and keep building the rest of the brief.
  block — raise ``LLMBudgetExceeded`` and let it propagate (PR #210 behavior).
  warn  — proceed past the cap; just log + record a one-shot 80%/100% alert
          (today's soft-cap behavior).

Default + backfill = ``'warn'`` (non-enforcing) so this ships DORMANT: every
existing cap keeps behaving exactly as it does today, and ``skip`` / ``block``
are opt-in per purpose (via the dashboard / ``manage_llm_budget`` CLI). See
``scratch/plans/llm_budget_dashboard_plan.md`` §0.2. The legacy ``hard_block``
column is kept for back-compat reads but ``on_exceed`` is now authoritative for
behavior — ``src/llm_budget.check_budget`` derives the gate decision from it.

Backfill: ``hard_block=1 → 'block'`` (preserve any operator-set hard cap);
``hard_block=0 → 'warn'`` (the column default — today's soft cap).

Revision ID: 0066_llm_budget_on_exceed
Revises: 0065_news
Create Date: 2026-06-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0066_llm_budget_on_exceed"
down_revision: str | Sequence[str] | None = "0065_news"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "llm_budgets" not in inspector.get_table_names():
        return  # nothing to alter on a DB without the budgets table yet
    cols = {c["name"] for c in inspector.get_columns("llm_budgets")}
    if "on_exceed" in cols:
        return  # idempotent

    # SQLite ADD COLUMN with NOT NULL needs a DEFAULT; the inline CHECK keeps the
    # three-way enum honest at the DB layer (the 'warn' default satisfies it).
    # Raw SQL so the CHECK rides on the ADD COLUMN.
    op.execute(
        "ALTER TABLE llm_budgets ADD COLUMN on_exceed TEXT NOT NULL DEFAULT 'warn' "
        "CHECK (on_exceed IN ('skip', 'block', 'warn'))"
    )
    # Preserve any operator-set hard cap as 'block'; everything else stays the
    # non-enforcing 'warn' default ("ignore budget at the moment").
    op.execute("UPDATE llm_budgets SET on_exceed = 'block' WHERE hard_block = 1")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "llm_budgets" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("llm_budgets")}
    if "on_exceed" not in cols:
        return
    op.execute("ALTER TABLE llm_budgets DROP COLUMN on_exceed")  # SQLite >= 3.35
