"""macro_sensitivities gains n_obs — the beta-quality floor's denominator.

Program review 2026-07-19: the macro betas feeding 20% of the next-dollar
blend carried r² of 0.01-0.10 with no quality floor and no persisted
observation count — MELI, NU and NVO (a LatAm marketplace, a Brazilian bank,
a Danish pharma) showed indistinguishable usd_cad betas, i.e. regression noise
dressed as exposure. ``compute_sensitivities`` always RETURNED n_obs; this
column persists it so the read-side floor (``allocation.model._macro_tilt``:
skip r² < 0.05 or n < 40) can tell a thin fit from a real one.

Additive, nullable INTEGER. NULL = legacy row written before the column; the
floor treats unknown n as "r² gate only" until the next recompute backfills.

Downgrade is a deliberate no-op (the 0182 lesson: a batch table rebuild breaks
any dependent readers, and keeping a nullable additive column is harmless).

Revision ID: 0184_macro_sensitivities_n_obs
Revises: 0183_data_feed_stale_trigger_kind
Create Date: 2026-07-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0184_macro_sensitivities_n_obs"
down_revision: str | Sequence[str] | None = "0183_data_feed_stale_trigger_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "macro_sensitivities" not in insp.get_table_names():
        return  # table is init_db-owned; absent on a bare migration-only DB
    existing_cols = {c["name"] for c in insp.get_columns("macro_sensitivities")}
    if "n_obs" in existing_cols:
        return  # idempotent
    op.add_column("macro_sensitivities", sa.Column("n_obs", sa.Integer(), nullable=True))


def downgrade() -> None:
    return  # deliberate no-op — see module docstring
