"""UNIQUE(ticker, fiscal_period_type, period_end) on transcripts.

Defense-in-depth for the reliability-ranked ingest-time guard added in
src/compute/transcript_ingest.py: the guard supersedes-or-skips in
application code, but nothing previously stopped two different documents
from both claiming the same real earnings call (root cause of the
2026-07-25 transcript-duplication incident — 29 duplicate period groups
across 8 tickers, fixed by `execution/dedupe_transcripts.py`). This
migration must run AFTER that one-time sweep; it will fail loudly on a
UNIQUE violation if stale duplicates remain, which is the desired behavior
(don't silently add a constraint that can't hold).

fiscal_period_type/period_end are both nullable in the base schema, so the
index is a plain UNIQUE index rather than a table constraint — SQLite (and
Postgres) treat NULL as distinct-from-NULL in a unique index, matching the
existing nullable columns without forcing a backfill of historical rows
that predate fiscal_period_type tracking.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0209_transcripts_period_unique"
down_revision: str | Sequence[str] | None = "0208_transcripts_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "transcripts"
_INDEX = "uq_transcripts_ticker_period_type_end"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    existing = {ix["name"] for ix in insp.get_indexes(_TABLE)}
    if _INDEX not in existing:
        op.create_index(
            _INDEX,
            _TABLE,
            ["ticker", "fiscal_period_type", "period_end"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    existing = {ix["name"] for ix in insp.get_indexes(_TABLE)}
    if _INDEX in existing:
        op.drop_index(_INDEX, table_name=_TABLE)
