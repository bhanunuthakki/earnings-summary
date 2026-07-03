"""Covering index for the whole-book fact readers at 1.1M rows.

The reader-tier-hardening audit measured ``cockpit_fundamentals.compute_from_db``
(the ``research_cockpit._eval_fundamentals`` materializer — ~1.2s at 726k rows)
at ~410ms against the 1,112,242-row prod ``financial_facts``. Its two SQL blocks
scan the WHOLE table: they filter ``line_item IN (4 items)`` + ``fiscal_period_type
LIKE 'Q%'``/``= 'TTM'`` with NO ticker predicate (they compute for every ticker at
once), so the planner fell back to a full ``SCAN … USING INDEX
uq_financial_facts_provenance``.

This lands a covering index led by the columns those queries actually filter on:

    idx_ff_lineitem_fpt_ticker_covering
        (line_item, fiscal_period_type, ticker, period_end, value, source_doc_id)

``line_item`` leads (the ``IN`` predicate is the selective one — 4 of ~35 line
items), then ``fiscal_period_type`` (the LIKE/= filter), then the partition keys
``ticker, period_end``. Trailing ``value, source_doc_id`` make it a COVERING index
for the cockpit + §3-financials reads (SELECT value/source_doc_id, ORDER BY the
tier CASE + id) so those queries never touch the base table. Measured against a
prod-backup copy: cockpit 410ms → 124ms, §3 GOOG+AMAT quarterly+annual 79ms →
48ms (both WITH the ``ANALYZE`` this migration also runs).

We also run ``ANALYZE`` at the end so the query planner has fresh stats to pick
the new index (and the existing ``ix_financial_facts_ticker_lineitem_period``)
instead of the full scan — the measurement showed ANALYZE alone recovers roughly
half the win.

FK-poisoning invariant: a plain ``CREATE INDEX`` (no REFERENCES anywhere). No
table rebuild — a pure index add + ANALYZE is transparent to every reader.

Revision ID: 0141_reader_perf_indexes
Revises: 0140_advisor_memos_position_review_kind
Create Date: 2026-07-02
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0141_reader_perf_indexes"
down_revision: str | Sequence[str] | None = "0140_advisor_memos_position_review_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "idx_ff_lineitem_fpt_ticker_covering"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":  # pragma: no cover - the app is sqlite-only
        return
    # Stamped-DB tolerance: test fixtures stamp an empty DB mid-chain and
    # upgrade to head, so financial_facts (created by init_db, not by any
    # migration in the stamped range) may simply not exist. CREATE INDEX
    # IF NOT EXISTS guards the index name, NOT the table — skip explicitly,
    # like every other post-baseline migration that touches core tables.
    if "financial_facts" not in sa.inspect(bind).get_table_names():
        return
    # IF NOT EXISTS keeps the migration idempotent across partial/re-run states.
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_INDEX_NAME} "
        "ON financial_facts "
        "(line_item, fiscal_period_type, ticker, period_end, value, source_doc_id)"
    )
    # Refresh planner stats so the whole-book readers pick the covering index
    # (and the existing ticker/line_item index) over a full-table scan. Best-effort:
    # ANALYZE never fails a migration.
    with contextlib.suppress(Exception):
        op.execute("ANALYZE")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":  # pragma: no cover
        return
    op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
