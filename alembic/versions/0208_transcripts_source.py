"""transcripts.source — provenance label used by the reliability-ranked
period-level idempotency guard (src/transcripts/source_reliability.py).

Plain nullable ADD COLUMN, following 0205's pattern. NULL means unclassified
(pre-existing rows) — `execution/dedupe_transcripts.py` backfills it for
survivors as part of the one-time duplicate sweep; this migration only adds
the column.

Numbered 0208 (not 0206) because origin/main's 0206_llm_calls_trace_context
claimed that number first — renumbered at rebase time per the repo's
alembic-collision convention.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0208_transcripts_source"
down_revision: str | Sequence[str] | None = "0206_llm_calls_trace_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "transcripts"
_COLUMN = "source"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns(_TABLE)}
    if _COLUMN not in existing:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns(_TABLE)}
    if _COLUMN in existing:
        op.drop_column(_TABLE, _COLUMN)
