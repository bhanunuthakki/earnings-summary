"""thesis_evaluations.soft_rule_results_json — store soft-rule outcomes alongside hard rules.

Hard `break_rules` continue to drive OK/WARN/BREACH in `rule_evaluations_json`.
Soft `break_rules_soft` (predicate-style: series_decel, series_below, etc.) now
drive YELLOW signals; their evaluated results are serialized into a separate
nullable column so the §2 renderer can surface them inline without coupling
the two schemas.

Nullable Text — historical rows pre-soft-rules read as NULL (renderer treats
that as "no soft rules evaluated", same as an empty list).

Revision ID: 0053_thesis_evaluations_soft_rules
Revises: 0052_llm_budgets
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0053_thesis_evaluations_soft_rules"
down_revision: str | Sequence[str] | None = "0052_llm_budgets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotent column-add: a fresh DB created from metadata may already have
    # the column. Skip when present so re-running upgrade on a populated DB is
    # a no-op rather than an IntegrityError.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("thesis_evaluations")}
    if "soft_rule_results_json" not in cols:
        op.add_column(
            "thesis_evaluations",
            sa.Column("soft_rule_results_json", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    # SQLite supports DROP COLUMN as of 3.35 (Python 3.11 ships with newer).
    # Alembic's batch mode keeps this portable across non-SQLite back ends.
    with op.batch_alter_table("thesis_evaluations") as batch_op:
        batch_op.drop_column("soft_rule_results_json")
