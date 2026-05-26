"""document_table_extractions — typed tables for extracted filing tables.

Phase 2 MVP of the document-table extraction effort (see
`directives/document_tables_design.md`). The MVP populates two table
kinds:

  customer_concentrations  — already created in 0040_filing_detail_tables;
                              this migration is a no-op for that table.
  lease_commitments        — NEW. Future minimum lease payments ladder
                              (operating + finance), one row per
                              (ticker, fiscal_year, lease_type, ladder_year).

Subsequent table kinds (goodwill_rollforward, debt_maturities,
stock_award_activity, geography_revenue_breakdown, geographic_assets,
supplier_concentrations, contractual_obligations, restructuring_programs,
drug_pipeline, equity_awards [DEF 14A], etc.) will land in their own
migrations as their extractors ship. See §3.3 of the design doc for
proposed DDL.

Revision ID: 0047_document_table_extractions
Revises: 0043_self_update_triggers
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0047_document_table_extractions"
# Origin/main had two parallel heads at the time of this PR
# (0044_etf_profile_and_holdings stayed separate; 0046_decisions merged
# 0044_tracked_companies_processing_tier + 0045_macro_overlay +
# 0052_llm_budgets). This migration merges those two heads back into a
# single linear chain so subsequent migrations have one predecessor.
down_revision: str | Sequence[str] | None = (
    "0044_etf_profile_and_holdings",
    "0046_decisions",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "lease_commitments" not in existing:
        op.create_table(
            "lease_commitments",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            sa.Column("fiscal_year", sa.Integer(), nullable=False),
            sa.Column("as_of_date", sa.Date(), nullable=False),
            sa.Column("filing_doc_id", sa.Integer(), nullable=True),
            # 'operating' | 'finance'
            sa.Column("lease_type", sa.String(length=16), nullable=False),
            # 'Y1'..'Y5' | 'Thereafter' | 'TotalPayments' | 'ImputedInterest' | 'LeaseLiability'
            sa.Column("ladder_year", sa.String(length=16), nullable=False),
            # absolute year (Y1..Y5 only); NULL for Thereafter/Total/Imputed/Liability
            sa.Column("ladder_calendar_year", sa.Integer(), nullable=True),
            sa.Column("amount", sa.Numeric(24, 6), nullable=False),
            sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
            sa.Column("unit", sa.String(length=16), nullable=False, server_default="millions"),
            # The FMP JSON top-level key the row came from. Useful for re-extraction
            # and for surfacing in the workspace renderer as a provenance line.
            sa.Column("source_section_key", sa.String(length=255), nullable=True),
            sa.Column("extracted_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "ticker", "fiscal_year", "lease_type", "ladder_year",
                name="uq_lease_commitments",
            ),
        )
        op.create_index(
            "idx_lease_commitments_ticker_year",
            "lease_commitments",
            ["ticker", "fiscal_year"],
        )
        op.create_index(
            "idx_lease_commitments_type",
            "lease_commitments",
            ["lease_type"],
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lease_commitments")
