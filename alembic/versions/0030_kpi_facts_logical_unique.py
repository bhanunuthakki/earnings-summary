"""kpi_facts_logical_unique — collapse multiple source_doc_id rows per KPI period.

The Phase 0011 wider index `uq_kpi_facts_provenance` keyed
(ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id) —
i.e. a re-ingest from a fresher document landed a NEW row instead of replacing
the old one. The renderer (`src/compute/thesis_evaluator.py`'s `_fetch_kpi_history`
+ §2 break-rule table) then read both copies and showed the same observation
twice ("2026-01-31: 46.33%" twice in the RBRK 2026-05-13 report).

This migration:
  1. Purges duplicates, keeping the row with the highest source_doc_id per
     (ticker, period_end, fiscal_period_type, kpi_definition_id) tuple — i.e.
     the value from the most recently ingested document wins.
  2. Drops the wider `uq_kpi_facts_provenance` index.
  3. Adds the narrower `uq_kpi_facts_logical` index on the same four-column
     logical tuple. Paired with the existing `INSERT OR IGNORE` in
     `src/pipeline/kpi_persistence.py::_insert_kpi_fact`, this gives DO NOTHING
     semantics for re-ingestion: the first document to land a value for a
     logical period wins until a future migration switches to REPLACE.

Revision ID: 0030_kpi_facts_logical_unique
Revises: 0029_earnings_surprises
Create Date: 2026-05-16
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030_kpi_facts_logical_unique"
down_revision: str | Sequence[str] | None = "0029_earnings_surprises"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PURGE_SQL = (
    "DELETE FROM kpi_facts "
    "WHERE EXISTS ("
    "  SELECT 1 FROM kpi_facts other "
    "  WHERE other.ticker = kpi_facts.ticker "
    "    AND other.period_end = kpi_facts.period_end "
    "    AND other.fiscal_period_type = kpi_facts.fiscal_period_type "
    "    AND other.kpi_definition_id = kpi_facts.kpi_definition_id "
    "    AND other.source_doc_id > kpi_facts.source_doc_id"
    ")"
)


def upgrade() -> None:
    bind = op.get_bind()
    purge_result = bind.execute(sa.text(_PURGE_SQL))
    sys.stderr.write(
        f"0030: purged {purge_result.rowcount} duplicate kpi_facts rows\n"
    )

    op.drop_index("uq_kpi_facts_provenance", table_name="kpi_facts")
    op.create_index(
        "uq_kpi_facts_logical",
        "kpi_facts",
        ["ticker", "period_end", "fiscal_period_type", "kpi_definition_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_kpi_facts_logical", table_name="kpi_facts")
    op.create_index(
        "uq_kpi_facts_provenance",
        "kpi_facts",
        ["ticker", "period_end", "fiscal_period_type", "kpi_definition_id", "source_doc_id"],
        unique=True,
    )
