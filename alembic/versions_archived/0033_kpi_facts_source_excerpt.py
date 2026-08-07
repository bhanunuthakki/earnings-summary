"""kpi_facts_source_excerpt — add a source-excerpt text field to kpi_facts.

Enables the comment processor's new `extract_kpi` intent to record the
verbatim quote that produced a manually-entered KPI value. Existing
automated extractors (extract_kpis_from_summaries) can also start
populating it so brief readers see "ROE 28% — from Q4 transcript: '...'".

VARCHAR(1024) chosen as a soft cap — analyst quotes are typically one
sentence; this absorbs ~10 lines of transcript context without forcing
multi-row provenance.

Nullable. Existing rows stay untouched.

Revision ID: 0033_kpi_facts_source_excerpt
Revises: 0032_source_calls_and_brief_efficiency
Create Date: 2026-05-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033_kpi_facts_source_excerpt"
down_revision: str | Sequence[str] | None = "0032_source_calls_and_brief_efficiency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_cols = {c["name"] for c in sa.inspect(bind).get_columns("kpi_facts")}
    if "source_excerpt" not in existing_cols:
        op.add_column(
            "kpi_facts",
            sa.Column("source_excerpt", sa.String(length=1024), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("kpi_facts") as batch:
        batch.drop_column("source_excerpt")
