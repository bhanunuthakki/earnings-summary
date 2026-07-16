"""signals.quality_score — LLM information-quality score, stamped at ingest.

The diet reading lane filtered low-quality publishers with a static substring
denylist (``_LOW_QUALITY_SOURCE_TOKENS`` in ``src/signals/store.py``) — a
keyword gate where semantics decide (LLM-maximalist directive). This lands the
intelligent version's storage: a nullable REAL 0..1 scored ONCE at ingest by
the ``diet_source_quality`` purpose (``src/signals/quality.py``, Haiku,
batched), read-time filtered by ``load_diet_signals`` on the STORED value.

Nullable on purpose: NULL = not yet scored (the reader falls back to the
static denylist — belt-and-braces until the batch pass catches up), so the
migration needs no backfill and the diet guard's non-decaying invariant is
untouched (a stored-field filter, never a wall-clock decay).

Plain nullable ``ADD COLUMN`` — no batch-table rebuild (and none of the
``batch_alter_table`` FTS-trigger hazards). Mirrors the 0142 stamped-DB guard
(``sa.inspect`` before DDL): fixture DBs that never ran 0095 have no
``signals`` table — skip, don't crash.

Revision ID: 0150_signals_quality_score
Revises: 0149_weekly_packet_and_nudges
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0150_signals_quality_score"
down_revision: str | Sequence[str] | None = "0149_weekly_packet_and_nudges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in insp.get_table_names()


def _has_column(insp: sa.Inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "signals"):
        return
    if _has_column(insp, "signals", "quality_score"):
        return
    op.add_column("signals", sa.Column("quality_score", sa.Float(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "signals"):
        return
    if not _has_column(insp, "signals", "quality_score"):
        return
    with op.batch_alter_table("signals") as batch:
        batch.drop_column("quality_score")
