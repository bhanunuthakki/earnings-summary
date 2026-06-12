"""kpi_facts.computed_from — input lineage for derived KPI rows.

Why (S2 PR3, directives/fund_grade_build_2026_06.md):
  ~14k kpi_facts rows are DERIVED (extracted_by 'fmp_derived' /
  'kpi_transform_derived'): margins, YoY growth rates, same-fiscal-quarter
  transforms. Their source chip pointed at the statement document the
  inputs came from, but nothing recorded WHICH inputs — "Operating Margin
  (GAAP) = ?" had no answer at the cell. ``computed_from`` is a nullable
  JSON TEXT column carrying the derivation recipe:

      {"display": "operating_income ÷ revenue (%)",
       "inputs": [{"ref": "financial_fact", "item": "operating_income",
                   "period_end": "2025-12-31", "doc_id": 45,
                   "tier": "fmp_normalized"}, ...]}

  ``display`` is the human-readable formula the chip popover shows
  ("derived from: …"); each ``inputs`` entry names the source row by
  logical key (ref kind + item + period_end) plus the source document id
  and its quality tier captured at derivation time, so the popover can
  render one mini-chip per input linking the /source viewer. Written by
  ``compute/fmp_derived_kpis.py`` (both derivation categories); NULL for
  directly-extracted rows. Schema documented in
  directives/data_provenance.md §9.

  Deliberately no DB-level JSON CHECK — same rationale as the 0075
  locator column (adding a CHECK to a large existing SQLite table forces
  a full-table rebuild); validity is the write path's job.

Revision ID: 0087_kpi_computed_from
Revises: 0086_decision_conditions
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0087_kpi_computed_from"
down_revision: str | Sequence[str] | None = "0086_decision_conditions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in insp.get_table_names()


def upgrade() -> None:
    # No-op when kpi_facts is absent (minimal test DBs run the whole chain —
    # the test_locator_schema contract; same guard pattern as 0075/0076) and
    # when the column already exists (idempotent re-run).
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "kpi_facts"):
        return
    cols = {c["name"] for c in insp.get_columns("kpi_facts")}
    if "computed_from" not in cols:
        op.add_column("kpi_facts", sa.Column("computed_from", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "kpi_facts"):
        return
    cols = {c["name"] for c in insp.get_columns("kpi_facts")}
    if "computed_from" in cols:
        op.drop_column("kpi_facts", "computed_from")
