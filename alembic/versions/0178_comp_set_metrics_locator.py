"""Add a nullable ``locator`` TEXT(JSON) column to comp_set_metrics_daily —
Phase 2 of the bottoms-up comparable-set program
(docs/design/comparable_sets_bottoms_up.md §5-§7).

Why now: Phase 2 introduces the first vendor-served rows in this table
(``scope_type='fmp_snapshot'``, the §7 drift check's independent reference)
alongside the computed bottoms-up aggregates. Under the provenance program's
persist-time locator rule (docs/design/provenance_clickthrough.md, hard
enforcement flipped in #911) every persisted fact-like row carries a locator:
``derived``-kind for computed aggregates, ``vendor_field`` for FMP snapshot
values. Phase 1 (#914) predated any vendor rows in this table and shipped
without the column; this closes that gap additively.

Additive-only; both the table-existence and column-existence guards follow
the ``sa.inspect(bind)`` convention (0044 / 0170). Plain ``op.add_column`` —
NEVER ``batch_alter_table`` (the FTS-trigger landmine; not applicable here
since comp_set_metrics_daily has no triggers, but the convention is
repo-wide).

Revision ID: 0178_comp_set_metrics_locator
Revises: 0174_behavior_distill_budget
Create Date: 2026-07-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0178_comp_set_metrics_locator"
down_revision: str | Sequence[str] | None = "0174_behavior_distill_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "comp_set_metrics_daily"
_COLUMN = "locator"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        # Fixture stamped before 0170 — nothing to alter; 0170 creates the
        # table and a re-run of this revision (idempotent guard) adds the
        # column when the table exists.
        return
    columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN not in columns:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN in columns:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {_COLUMN}")
