"""panel_activation_counts — durable per-panel per-day activation counter.

navigation_ia.md §5 (instrument-first): the shell's latency samples live in an
in-memory ring by design, so "which surfaces actually get walked" resets on
every server restart — the exact blindness that made the IA debate
unfalsifiable. This table keeps only the count (panel_id, day) → n; the
latency percentiles stay ephemeral. The POST handler upserts lazily with
``CREATE TABLE IF NOT EXISTS`` (a fresh init_db DB works without alembic), so
this migration is the schema-lineage record with the stamped-DB guard
(mirrors 0141's ``sa.inspect`` pattern).

Revision ID: 0147_panel_activation_counts
Revises: 0146_positioning_budgets
Create Date: 2026-07-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0147_panel_activation_counts"
down_revision: str | Sequence[str] | None = "0146_positioning_budgets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DDL = """
CREATE TABLE panel_activation_counts (
    panel_id TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (panel_id, day)
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "panel_activation_counts" in set(inspector.get_table_names()):
        return
    op.execute(sa.text(_DDL))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "panel_activation_counts" not in set(inspector.get_table_names()):
        return
    op.execute(sa.text("DROP TABLE panel_activation_counts"))
