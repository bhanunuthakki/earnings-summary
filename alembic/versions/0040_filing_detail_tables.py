"""filing_detail_tables — granular structured extraction targets.

Five new tables for content that today gets paraphrased into a single
filing_intelligence synthesis. Surfacing them as rows lets the renderer
build per-table tabs (risk-factor delta, footnote ledger, litigation list)
and lets cross-ticker queries answer questions like "which holdings have
material litigation with the same counterparty?"

  risk_factors                 — Item 1A risks per 10-K, with year-over-year
                                  diff flag and reword summary.
  critical_accounting_estimates— Critical-estimate policies + assumptions
                                  extracted from Item 7 (MD&A).
  footnote_facts               — Structured rows from Item 8 footnotes: lease
                                  commitments, contingent liabilities, related-
                                  party transactions, SBC, off-BS items.
  litigation_matters           — Named legal matters with status, amount-at-
                                  risk, counterparty entity link.
  capital_actions              — Buyback authorizations, dividend changes,
                                  acquisition intent, with amount + timeframe.
  customer_concentrations      — % of revenue by named / anonymized customer
                                  (Item 1 + footnotes). Feeds entity_relationships.
  forward_looking_statements   — Sentences with future-tense quantifiers, extracted
                                  from transcripts/MD&A. Cross-references predictions
                                  table via predictions.source_doc_id.
  numerical_claims             — Every $X figure with context, normalized to a
                                  concept. Searchable: "AMZN ad revenue >$X bn"

Revision ID: 0040_filing_detail_tables
Revises: 0039_insights
Create Date: 2026-05-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0040_filing_detail_tables"
down_revision: str | Sequence[str] | None = "0039_insights"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "risk_factors" not in existing:
        op.create_table(
            "risk_factors",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            sa.Column("fiscal_year", sa.Integer(), nullable=False),
            sa.Column("filing_doc_id", sa.Integer(), nullable=True),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("heading", sa.String(length=512), nullable=True),
            sa.Column("body_md", sa.Text(), nullable=False),
            sa.Column("category", sa.String(length=32), nullable=True),  # 'regulatory','operational','financial','technology','macro','litigation'
            sa.Column("vs_prior_year", sa.String(length=16), nullable=True),  # 'new','removed','reworded','unchanged'
            sa.Column("reword_diff_md", sa.Text(), nullable=True),
            sa.Column("body_sha256", sa.String(length=64), nullable=True),
            sa.Column("extracted_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("ticker", "fiscal_year", "ordinal", name="uq_risk_factors"),
        )
        op.create_index("idx_risk_factors_ticker_year", "risk_factors", ["ticker", "fiscal_year"])
        op.create_index("idx_risk_factors_category", "risk_factors", ["category"])
        op.create_index("idx_risk_factors_change", "risk_factors", ["vs_prior_year"])

    if "critical_accounting_estimates" not in existing:
        op.create_table(
            "critical_accounting_estimates",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            sa.Column("fiscal_year", sa.Integer(), nullable=False),
            sa.Column("filing_doc_id", sa.Integer(), nullable=True),
            sa.Column("policy_name", sa.String(length=128), nullable=False),
            sa.Column("body_md", sa.Text(), nullable=False),
            sa.Column("assumptions_extracted", sa.Text(), nullable=True),  # JSON
            sa.Column("vs_prior_year", sa.String(length=16), nullable=True),
            sa.Column("change_summary_md", sa.Text(), nullable=True),
            sa.Column("extracted_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("ticker", "fiscal_year", "policy_name", name="uq_critical_est"),
        )
        op.create_index("idx_critical_est_ticker", "critical_accounting_estimates", ["ticker", "fiscal_year"])

    if "footnote_facts" not in existing:
        op.create_table(
            "footnote_facts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            sa.Column("period_end", sa.DateTime(), nullable=False),
            sa.Column("fiscal_period_type", sa.String(length=4), nullable=False),  # Q1..Q4, FY
            sa.Column("filing_doc_id", sa.Integer(), nullable=True),
            sa.Column("footnote_number", sa.String(length=16), nullable=True),  # 'Note 7'
            sa.Column("fact_type", sa.String(length=32), nullable=False),
            # 'lease_commitment' | 'contingent_liability' | 'related_party_tx' |
            # 'stock_based_comp' | 'off_balance_sheet' | 'goodwill_change' |
            # 'segment_realignment' | 'restructuring_charge'
            sa.Column("description_md", sa.Text(), nullable=True),
            sa.Column("amount", sa.Numeric(24, 6), nullable=True),
            sa.Column("currency", sa.String(length=3), nullable=True),
            sa.Column("unit", sa.String(length=16), nullable=True),
            sa.Column("due_period", sa.DateTime(), nullable=True),
            sa.Column("counterparty", sa.String(length=255), nullable=True),
            sa.Column("counterparty_entity_id", sa.Integer(), nullable=True),  # FK to entities (loose)
            sa.Column("status", sa.String(length=24), nullable=True),  # 'active'|'resolved'|'reclassified'
            sa.Column("source_excerpt", sa.Text(), nullable=True),
            sa.Column("extracted_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "idx_footnote_facts_ticker_period",
            "footnote_facts",
            ["ticker", "period_end", "fact_type"],
        )
        op.create_index("idx_footnote_facts_type", "footnote_facts", ["fact_type"])
        op.create_index("idx_footnote_facts_counterparty", "footnote_facts", ["counterparty_entity_id"])

    if "litigation_matters" not in existing:
        op.create_table(
            "litigation_matters",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            sa.Column("matter_name", sa.String(length=512), nullable=False),
            sa.Column("first_disclosed_at", sa.DateTime(), nullable=True),
            sa.Column("counterparty", sa.String(length=255), nullable=True),
            sa.Column("counterparty_entity_id", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=True),  # 'pending','settled','dismissed','appealing','active'
            sa.Column("amount_at_risk", sa.Numeric(24, 6), nullable=True),
            sa.Column("amount_currency", sa.String(length=3), nullable=True),
            sa.Column("amount_unit", sa.String(length=16), nullable=True),
            sa.Column("expected_resolution_date", sa.DateTime(), nullable=True),
            sa.Column("category", sa.String(length=32), nullable=True),  # 'antitrust','ip','consumer','employment','tax','environmental'
            sa.Column("description_md", sa.Text(), nullable=True),
            sa.Column("latest_update_md", sa.Text(), nullable=True),
            sa.Column("latest_update_at", sa.DateTime(), nullable=True),
            sa.Column("latest_doc_id", sa.Integer(), nullable=True),
            sa.UniqueConstraint("ticker", "matter_name", name="uq_litigation_matters"),
        )
        op.create_index("idx_litigation_ticker", "litigation_matters", ["ticker", "status"])

    if "capital_actions" not in existing:
        op.create_table(
            "capital_actions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            sa.Column("action_kind", sa.String(length=32), nullable=False),
            # 'buyback_authorization','buyback_executed','dividend_initiation',
            # 'dividend_increase','dividend_cut','dividend_suspension','split',
            # 'spinoff','acquisition_announced','acquisition_closed','disposition',
            # 'debt_issuance','debt_retirement','equity_issuance'
            sa.Column("announced_at", sa.DateTime(), nullable=False),
            sa.Column("effective_at", sa.DateTime(), nullable=True),
            sa.Column("amount", sa.Numeric(24, 6), nullable=True),
            sa.Column("amount_currency", sa.String(length=3), nullable=True),
            sa.Column("amount_unit", sa.String(length=16), nullable=True),
            sa.Column("per_share_amount", sa.Numeric(24, 6), nullable=True),
            sa.Column("counterparty", sa.String(length=255), nullable=True),
            sa.Column("counterparty_entity_id", sa.Integer(), nullable=True),
            sa.Column("description_md", sa.Text(), nullable=True),
            sa.Column("source_doc_id", sa.Integer(), nullable=True),
            sa.Column("source_excerpt", sa.Text(), nullable=True),
        )
        op.create_index("idx_capital_actions_ticker", "capital_actions", ["ticker", "announced_at"])
        op.create_index("idx_capital_actions_kind", "capital_actions", ["action_kind"])

    if "customer_concentrations" not in existing:
        op.create_table(
            "customer_concentrations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            sa.Column("fiscal_period", sa.String(length=10), nullable=False),  # YYYY or YYYY-MM-DD
            sa.Column("fiscal_period_type", sa.String(length=4), nullable=False),
            sa.Column("customer_label", sa.String(length=255), nullable=False),  # 'Customer A' | 'Amazon' | etc.
            sa.Column("customer_entity_id", sa.Integer(), nullable=True),  # resolved entity (when named)
            sa.Column("pct_of_revenue", sa.Float(), nullable=False),  # 0..1
            sa.Column("revenue_amount", sa.Numeric(24, 6), nullable=True),
            sa.Column("revenue_currency", sa.String(length=3), nullable=True),
            sa.Column("source_doc_id", sa.Integer(), nullable=True),
            sa.Column("source_excerpt", sa.Text(), nullable=True),
            sa.Column("extracted_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "ticker",
                "fiscal_period",
                "customer_label",
                name="uq_cust_concentration",
            ),
        )
        op.create_index(
            "idx_cust_concentration_ticker",
            "customer_concentrations",
            ["ticker", "fiscal_period"],
        )
        op.create_index(
            "idx_cust_concentration_entity",
            "customer_concentrations",
            ["customer_entity_id"],
        )

    if "numerical_claims" not in existing:
        op.create_table(
            "numerical_claims",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            sa.Column("source_doc_id", sa.Integer(), nullable=False),
            sa.Column("char_offset_start", sa.Integer(), nullable=True),
            sa.Column("char_offset_end", sa.Integer(), nullable=True),
            sa.Column("claim_text", sa.Text(), nullable=False),
            sa.Column("amount", sa.Numeric(24, 6), nullable=False),
            sa.Column("currency", sa.String(length=3), nullable=True),
            sa.Column("unit", sa.String(length=16), nullable=True),
            sa.Column("scale", sa.String(length=16), nullable=True),  # 'thousand','million','billion','trillion'
            sa.Column("concept_id", sa.Integer(), nullable=True),  # FK to concepts (loose)
            sa.Column("period_end", sa.DateTime(), nullable=True),
            sa.Column("extracted_at", sa.DateTime(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        )
        op.create_index("idx_num_claims_ticker", "numerical_claims", ["ticker"])
        op.create_index("idx_num_claims_concept", "numerical_claims", ["concept_id"])
        op.create_index("idx_num_claims_doc", "numerical_claims", ["source_doc_id"])

    if "forward_looking_statements" not in existing:
        op.create_table(
            "forward_looking_statements",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            sa.Column("source_doc_id", sa.Integer(), nullable=False),
            sa.Column("char_offset_start", sa.Integer(), nullable=True),
            sa.Column("char_offset_end", sa.Integer(), nullable=True),
            sa.Column("sentence", sa.Text(), nullable=False),
            sa.Column("speaker", sa.String(length=128), nullable=True),  # mgmt or other
            sa.Column("tense", sa.String(length=16), nullable=True),  # 'will','expect','plan','target','believe'
            sa.Column("quantifier", sa.String(length=64), nullable=True),  # extracted quantifier
            sa.Column("kpi_name", sa.String(length=128), nullable=True),
            sa.Column("kpi_concept_id", sa.Integer(), nullable=True),
            sa.Column("target_value", sa.Numeric(24, 6), nullable=True),
            sa.Column("target_period", sa.DateTime(), nullable=True),
            sa.Column("prediction_id", sa.Integer(), nullable=True),  # FK to predictions when materialized
            sa.Column("extracted_at", sa.DateTime(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        )
        op.create_index(
            "idx_forward_stmt_ticker", "forward_looking_statements", ["ticker", "target_period"]
        )
        op.create_index(
            "idx_forward_stmt_prediction", "forward_looking_statements", ["prediction_id"]
        )


def downgrade() -> None:
    for table in (
        "forward_looking_statements",
        "numerical_claims",
        "customer_concentrations",
        "capital_actions",
        "litigation_matters",
        "footnote_facts",
        "critical_accounting_estimates",
        "risk_factors",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
