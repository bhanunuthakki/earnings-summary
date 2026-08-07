"""post_earnings_readout purpose budget.

No new artifact table is needed: ``llm_artifacts`` already enforces one
current row per (ticker, purpose, fiscal_period) and retains superseded rows.
This migration only seeds the skip-mode spend boundary shared by the
portfolio-only scheduled lane and explicit evaluation-name requests.

Revision ID: 0273_post_earnings_readout_budget
Revises: 0272_archive_generation_catalog
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0273_post_earnings_readout_budget"
down_revision: str | Sequence[str] | None = "0272_archive_generation_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURPOSE = "post_earnings_readout"
_MONTHLY_CAP_USD = 5.00


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "llm_budgets" not in set(insp.get_table_names()):
        return
    columns = {column["name"] for column in insp.get_columns("llm_budgets")}
    now = datetime.now(UTC).isoformat()
    if "on_exceed" in columns:
        sql = """
            INSERT INTO llm_budgets
                (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                 on_exceed, created_at, updated_at, notes)
            VALUES (:purpose, :cap, 0.80, 0, 'skip', :now, :now, :notes)
            ON CONFLICT(purpose) DO NOTHING
        """
    else:
        sql = """
            INSERT INTO llm_budgets
                (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                 created_at, updated_at, notes)
            VALUES (:purpose, :cap, 0.80, 0, :now, :now, :notes)
            ON CONFLICT(purpose) DO NOTHING
        """
    bind.execute(
        sa.text(sql),
        {
            "purpose": _PURPOSE,
            "cap": _MONTHLY_CAP_USD,
            "now": now,
            "notes": "seeded by migration 0273 - portfolio-only automatic post-earnings "
            "readouts plus explicit evaluation-name requests; skip mode preserves the "
            "deterministic template when the cap is exhausted",
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "llm_budgets" in set(sa.inspect(bind).get_table_names()):
        bind.execute(
            sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"),
            {"purpose": _PURPOSE},
        )
