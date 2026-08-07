"""alerts.dismiss_reason — the ritual's captured signal on a dismiss.

Consequence-receipts PR (2026-07-03): dismissing an alert only ever recorded
*that* it happened (``dismissed_at``), never *why*. The ritual calls a
dismissal reason a signal — the coach needs it to tell "this alert kind
stopped mattering" from "wrong ticker" from "already knew, acted elsewhere" —
but nothing captured it. This lands the column; the UI affordance is
skippable (the dismiss itself never blocks on supplying one) and v1 is
scoped to the alerts lane only (queued-action dismiss stays a bare ✕).

Plain nullable ``TEXT`` column, no FK, no CHECK — free text, capped
client-side, not a controlled vocabulary. A single ``ADD COLUMN`` needs no
batch-table rebuild in SQLite.

Revision ID: 0142_alerts_dismiss_reason
Revises: 0141_reader_perf_indexes
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0142_alerts_dismiss_reason"
down_revision: str | Sequence[str] | None = "0141_reader_perf_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in insp.get_table_names()


def _has_column(insp: sa.Inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    # Stamped-DB tolerance: some test fixtures stamp a pre-0063 head and
    # upgrade only partway (or hand-build a narrower substrate entirely), so
    # ``alerts`` may simply not exist yet — skip explicitly, like every other
    # post-baseline migration that touches core tables.
    if not _has_table(insp, "alerts"):
        return
    if _has_column(insp, "alerts", "dismiss_reason"):
        return
    op.add_column("alerts", sa.Column("dismiss_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "alerts"):
        return
    if not _has_column(insp, "alerts", "dismiss_reason"):
        return
    with op.batch_alter_table("alerts") as batch:
        batch.drop_column("dismiss_reason")
