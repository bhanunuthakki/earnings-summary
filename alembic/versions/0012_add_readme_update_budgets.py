"""Add fail-closed per-purpose budgets for README generation and judging.

Revision ID: 0012_add_readme_update_budgets
Revises: 0011_add_operations_journal
Create Date: 2026-08-13
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision = "0012_add_readme_update_budgets"
down_revision = "0011_add_operations_journal"
branch_labels = None
depends_on = None

_NOTE = "README updater governance migration 0012"
_PURPOSES = ("readme_update", "readme_update_judge")


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "llm_budgets" not in tables:
        return
    now = datetime.now(UTC).isoformat()
    for purpose in _PURPOSES:
        bind.execute(
            sa.text(
                "INSERT INTO llm_budgets "
                "(purpose,monthly_cap_usd,warn_threshold_pct,hard_block,created_at,"
                "updated_at,notes,on_exceed) "
                "SELECT :purpose,5,0.80,1,:now,:now,:notes,'block' "
                "WHERE NOT EXISTS (SELECT 1 FROM llm_budgets WHERE purpose=:purpose)"
            ),
            {"purpose": purpose, "now": now, "notes": _NOTE},
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "llm_budgets" not in set(sa.inspect(bind).get_table_names()):
        return
    bind.execute(
        sa.text("DELETE FROM llm_budgets WHERE purpose IN :purposes AND notes=:notes").bindparams(
            sa.bindparam("purposes", expanding=True)
        ),
        {"purposes": _PURPOSES, "notes": _NOTE},
    )
