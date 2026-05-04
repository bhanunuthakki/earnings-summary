"""facts_unique_indexes — UNIQUE indexes on the three facts tables for idempotent inserts.

Without these indexes, re-running an extractor against the same source document
inserts duplicate rows. Each fact is uniquely identified by:

    financial_facts: (ticker, period_end, fiscal_period_type, line_item, source_doc_id)
    segment_facts:   (ticker, period_end, fiscal_period_type, segment_name, metric, source_doc_id)
    kpi_facts:       (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id)

A new sha256 = new documents row = new source_doc_id, so historical values are
preserved on re-fetch; idempotence applies only within a single document.

Revision ID: 0011_facts_unique_indexes
Revises: 0010_reclassify_fmp_other
Create Date: 2026-05-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_facts_unique_indexes"
down_revision: str | Sequence[str] | None = "0010_reclassify_fmp_other"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_financial_facts_provenance",
        "financial_facts",
        ["ticker", "period_end", "fiscal_period_type", "line_item", "source_doc_id"],
        unique=True,
    )
    op.create_index(
        "uq_segment_facts_provenance",
        "segment_facts",
        ["ticker", "period_end", "fiscal_period_type", "segment_name", "metric", "source_doc_id"],
        unique=True,
    )
    op.create_index(
        "uq_kpi_facts_provenance",
        "kpi_facts",
        ["ticker", "period_end", "fiscal_period_type", "kpi_definition_id", "source_doc_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_kpi_facts_provenance", table_name="kpi_facts")
    op.drop_index("uq_segment_facts_provenance", table_name="segment_facts")
    op.drop_index("uq_financial_facts_provenance", table_name="financial_facts")
