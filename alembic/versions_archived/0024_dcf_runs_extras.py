"""dcf_runs_extras — fields for the new DCF valuation subsystem.

Adds columns the per-ticker DCF refresher writes after reading the FCF stream
from the workbook and computing PV/share at the holdings JSON's wacc:

  live_price             -- market price snapshot at valuation time
  live_price_at          -- timestamp of the price snapshot
  over_under_pct         -- (live_price - npv_per_share) / npv_per_share
  mos_bar_used           -- holdings.mos_bar at refresh time (audit)
  assumption_snapshot_json
                         -- captured forecast assumption cells from the .xlsx
                            (revenue growth %, margins, terminal multiple, etc.)
                            for diff across quarters

Idempotent on column adds via inspector check; downgrade drops in reverse.

Revision ID: 0024_dcf_runs_extras
Revises: 0023_brief_provenance_log
Create Date: 2026-05-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_dcf_runs_extras"
down_revision: str | Sequence[str] | None = "0023_brief_provenance_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NEW_COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    ("live_price", sa.Column("live_price", sa.Numeric(precision=24, scale=6))),
    ("live_price_at", sa.Column("live_price_at", sa.DateTime())),
    ("over_under_pct", sa.Column("over_under_pct", sa.Float())),
    ("mos_bar_used", sa.Column("mos_bar_used", sa.Float())),
    ("assumption_snapshot_json", sa.Column("assumption_snapshot_json", sa.Text())),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("dcf_runs")}
    for name, col in _NEW_COLUMNS:
        if name not in existing_cols:
            op.add_column("dcf_runs", col)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing_cols = {c["name"] for c in inspector.get_columns("dcf_runs")}
    with op.batch_alter_table("dcf_runs") as batch:
        for name, _ in reversed(_NEW_COLUMNS):
            if name in existing_cols:
                batch.drop_column(name)
