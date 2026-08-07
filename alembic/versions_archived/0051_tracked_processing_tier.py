"""tracked_processing_tier — per-ticker cadence tier for the tier-aware runner.

The fresh-review memo flagged that today every ticker (portfolio, watchlist,
evaluation, index_member) gets the same processing cadence. With the universe
expanding past 2k+ index constituents, that doesn't scale: portfolio names
deserve daily attention, watchlist/evaluation weekly, and the broad index
universe only monthly.

This migration adds `tracked_companies.processing_tier VARCHAR(8)` plus a
covering index on (processing_tier, last_built_at) so the tier-aware runner's
"who is due?" query stays index-only.

Default mapping (also used by Week 2's migration 0044 if it lands first):
  portfolio              -> P1
  watchlist | evaluation -> P2
  else (index_member,
        etf, none)       -> P3

Defensive design: if 0044 already added the column, this migration only
creates the index. If 0044 hasn't merged yet, this adds the column + backfills
the tier per the mapping above, then adds the index. The work is in addition
to the brief_dirty queue (gate A), the material-change hash gate (B), and the
evaluation-cadence gate (C) — this is a fourth gate scoped to tier.

Revision ID: 0051_tracked_processing_tier
Revises: 0043_self_update_triggers
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0051_tracked_processing_tier"
# Chains off both surviving heads — 0046_decisions (which already merges
# the three Week-2 / Week-3 / budget heads off 0043) and the parallel
# 0044_etf_profile_and_holdings branch. This makes 0051 the new single
# head; subsequent migrations can chain off it cleanly.
down_revision: str | Sequence[str] | None = (
    "0046_decisions",
    "0044_etf_profile_and_holdings",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("tracked_companies")}
    if "processing_tier" not in cols:
        op.add_column(
            "tracked_companies",
            sa.Column(
                "processing_tier",
                sa.String(length=8),
                nullable=False,
                server_default="P3",
            ),
        )
        op.execute(
            "UPDATE tracked_companies SET processing_tier = 'P1' WHERE list_type = 'portfolio'"
        )
        op.execute(
            "UPDATE tracked_companies SET processing_tier = 'P2' "
            "WHERE list_type IN ('watchlist', 'evaluation')"
        )

    existing_idx = {idx["name"] for idx in sa.inspect(bind).get_indexes("tracked_companies")}
    if "idx_tracked_processing_tier" not in existing_idx:
        op.create_index(
            "idx_tracked_processing_tier",
            "tracked_companies",
            ["processing_tier", "last_built_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing_idx = {idx["name"] for idx in sa.inspect(bind).get_indexes("tracked_companies")}
    if "idx_tracked_processing_tier" in existing_idx:
        op.drop_index("idx_tracked_processing_tier", table_name="tracked_companies")
    cols = {c["name"] for c in sa.inspect(bind).get_columns("tracked_companies")}
    if "processing_tier" in cols:
        with op.batch_alter_table("tracked_companies") as batch:
            batch.drop_column("processing_tier")
