"""dcf_runs: persist the workbook→assumptions-JSON sync outcome per refresh.

``refresh_dcf.sync_assumptions_json`` mirrors edited workbook assumptions back
into ``data/dcf_assumptions/<T>.json`` so a from-scratch rebuild reproduces
them — but its outcome was a bool buried in the CLI's stdout JSON: a sync that
silently no-ops (43 of ~91 workbooks have no assumptions JSON at all) leaves
the JSON stale, and the next ``build_all_redesigned_dcf`` would quietly revert
the user's edits. S11 PR2 makes the sync result durable:

* ``assumptions_sync_status`` TEXT — 'synced' (block updated), 'created'
  (no JSON existed; one was created from the workbook so from-scratch builds
  can't revert edits), or 'failed: <detail>' (unreadable JSON / write error).
  NULL for rows that predate this migration and for archetypes whose builders
  don't run the redesign sync (bank / holdco / fintech / platform).
* ``assumptions_synced_at`` DATETIME — naive-UTC stamp of the last sync
  attempt (the project's naive-UTC convention).

The valuation card and the DCF coverage panel read these to show sync
staleness/failures instead of leaving the contract invisible.

Idempotent: skips columns that already exist; skips silently when dcf_runs
doesn't exist (fixture DBs stamped past the early schema).

Revision ID: 0091_dcf_runs_assumptions_sync
Revises: 0090_ask_claim_grounding_budget
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0091_dcf_runs_assumptions_sync"
down_revision: str | Sequence[str] | None = "0090_ask_claim_grounding_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS: tuple[sa.Column[object], ...] = (
    sa.Column("assumptions_sync_status", sa.Text(), nullable=True),
    sa.Column("assumptions_synced_at", sa.DateTime(), nullable=True),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "dcf_runs" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("dcf_runs")}
    for col in _COLUMNS:
        if col.name not in existing:
            op.add_column("dcf_runs", col)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "dcf_runs" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("dcf_runs")}
    with op.batch_alter_table("dcf_runs") as batch:
        for col in _COLUMNS:
            if col.name in existing:
                batch.drop_column(col.name)
