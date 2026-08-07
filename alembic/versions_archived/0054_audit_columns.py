"""audit_columns — extend facts + documents with provenance/audit-trail columns.

Week-1 foundational audit pass (see [[project_roadmap_2026_05_25]]). Today the
loaders' `max(id)` dedup is a proxy for "newest wins" that breaks down when:

  * Cross-source ingestion lands rows in id-order that doesn't match
    source-quality order (an LLM extraction with id=42 silently overwrites
    a deterministic SEC XBRL value with id=37 if both target the same
    logical period).
  * A later 10-K restates a value originally reported in an earlier 10-Q.
    Today both rows coexist with no link explaining which is the canonical
    later view; downstream analysis can't reconstruct what was knowable
    at the time of an earlier brief.

This migration adds the columns needed for:

  1. **Tier-aware dedup**. `documents.source_quality_tier` ranks every
     ingested document on the canonical scale
        sec_official > fmp_normalized > llm_extracted > yfinance_fallback.
     Backfilled from `documents.source_type` on upgrade.

  2. **Restatement chains**. `financial_facts.supersedes_id` /
     `kpi_facts.supersedes_id` link a newer restated row to the one it
     replaces. Combined with the time-travel filter in the loaders
     (`--as-of-date`), this lets us reproduce any historical brief
     deterministically.

  3. **Per-row confidence + provenance**. `financial_facts.extracted_by`
     records the extractor (`fmp`, `sec_xbrl`, `llm:claude-sonnet-4-6`,
     `manual_csv`, ...) so audit reports can group "all LLM-derived
     facts" without scanning the documents JOIN. `kpi_facts` gets a
     matching `confidence` column (financial_facts already has one,
     added in 0004 — this brings kpi_facts up to parity).

  Note on kpi_facts.supersedes_id: the existing unique index
  `uq_kpi_facts_logical` (added in 0030) blocks multiple rows for the
  same logical tuple, so the column is nullable schema-level
  forward-compatibility — the kpi-side restatement detector wiring is
  a follow-on PR that will also relax that constraint. Adding the
  column now keeps the two tables symmetric and lets `_latest_in_chain`
  follow a uniform code path.

  Indexes: the new `idx_financial_facts_restatement_lookup` covers
  (ticker, period_end, fiscal_period_type, line_item) — the four-column
  logical key the restatement detector consults before every insert.
  kpi_facts already has `uq_kpi_facts_logical` over the equivalent
  four-column tuple (kpi_definition_id in place of line_item), so no
  new kpi index is added.

Revision ID: 0054_audit_columns
Revises: 0053_strategic_targets, 0053_thesis_evaluations_soft_rules, 0053_timeseries_signals
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0054_audit_columns"
# Three parallel heads at the time this PR was authored: 0053_strategic_targets,
# 0053_thesis_evaluations_soft_rules, and 0053_timeseries_signals. Merging all
# three back into a single chain so subsequent migrations have one predecessor.
down_revision: str | Sequence[str] | None = (
    "0053_strategic_targets",
    "0053_thesis_evaluations_soft_rules",
    "0053_timeseries_signals",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Backfill mapping for source_quality_tier inferred from documents.source_type.
# Keep in sync with src/timeseries/loaders.py::SOURCE_QUALITY_TIER_RANK.
#
# `manual_csv` and `manual_entry` are treated as `fmp_normalized` (analyst-
# entered values are deterministic, not LLM-derived). `ir_doc` and
# `transcript_audio` are also `fmp_normalized` because they are primary
# materials whose facts are typed by deterministic extractors, not LLMs.
# Only `llm_extracted` source rows get the `llm_extracted` tier.
_BACKFILL_TIER_BY_SOURCE_TYPE: list[tuple[str, str]] = [
    ("sec_xbrl", "sec_official"),
    ("fmp", "fmp_normalized"),
    ("ir_doc", "fmp_normalized"),
    ("transcript_audio", "fmp_normalized"),
    ("manual_csv", "fmp_normalized"),
    ("manual_entry", "fmp_normalized"),
    ("llm_extracted", "llm_extracted"),
]


# View DDL — copied verbatim from migrations 0012 (metrics + ratios) and
# 0042 (v_quarters). batch_alter_table on the facts tables drops these
# implicitly during the rebuild; we must recreate them at the end of
# upgrade() so the dashboard queries don't break. Keep these in sync with
# the source migrations if their definitions ever change.

_METRICS_VIEW_DDL = """
CREATE VIEW IF NOT EXISTS metrics AS
WITH dedup AS (
    SELECT
        ticker,
        CAST(substr(period_end, 1, 4) AS INTEGER) AS fiscal_year,
        fiscal_period_type,
        line_item,
        value,
        currency,
        period_end,
        ROW_NUMBER() OVER (
            PARTITION BY
                ticker,
                CAST(substr(period_end, 1, 4) AS INTEGER),
                fiscal_period_type,
                line_item
            ORDER BY source_doc_id DESC, period_end DESC
        ) AS rn
    FROM financial_facts
)
SELECT
    ticker,
    fiscal_year,
    fiscal_period_type,
    MAX(period_end) AS period_end,
    MAX(currency) AS currency,
    MAX(CASE WHEN line_item = 'revenue' THEN value END) AS revenue,
    MAX(CASE WHEN line_item = 'cost_of_revenue' THEN value END) AS cost_of_revenue,
    MAX(CASE WHEN line_item = 'gross_profit' THEN value END) AS gross_profit,
    MAX(CASE WHEN line_item = 'research_and_development' THEN value END) AS rd,
    MAX(CASE WHEN line_item = 'sga' THEN value END) AS sga,
    MAX(CASE WHEN line_item = 'operating_expenses' THEN value END) AS operating_expenses,
    MAX(CASE WHEN line_item = 'operating_income' THEN value END) AS operating_income,
    MAX(CASE WHEN line_item = 'ebit' THEN value END) AS ebit,
    MAX(CASE WHEN line_item = 'ebitda' THEN value END) AS ebitda,
    MAX(CASE WHEN line_item = 'net_income' THEN value END) AS net_income,
    MAX(CASE WHEN line_item = 'eps' THEN value END) AS eps,
    MAX(CASE WHEN line_item = 'eps_diluted' THEN value END) AS eps_diluted,
    MAX(CASE WHEN line_item = 'weighted_avg_shares_diluted' THEN value END)
        AS weighted_avg_shares_diluted,
    MAX(CASE WHEN line_item = 'total_assets' THEN value END) AS total_assets,
    MAX(CASE WHEN line_item = 'total_current_assets' THEN value END) AS total_current_assets,
    MAX(CASE WHEN line_item = 'cash_and_equivalents' THEN value END) AS cash_and_equivalents,
    MAX(CASE WHEN line_item = 'total_liabilities' THEN value END) AS total_liabilities,
    MAX(CASE WHEN line_item = 'total_current_liabilities' THEN value END)
        AS total_current_liabilities,
    MAX(CASE WHEN line_item = 'total_stockholders_equity' THEN value END) AS total_equity,
    MAX(CASE WHEN line_item = 'total_debt' THEN value END) AS total_debt,
    MAX(CASE WHEN line_item = 'long_term_debt' THEN value END) AS long_term_debt,
    MAX(CASE WHEN line_item = 'net_debt' THEN value END) AS net_debt,
    MAX(CASE WHEN line_item = 'operating_cash_flow' THEN value END) AS operating_cash_flow,
    MAX(CASE WHEN line_item = 'capital_expenditure' THEN value END) AS capex,
    MAX(CASE WHEN line_item = 'free_cash_flow' THEN value END) AS free_cash_flow,
    MAX(CASE WHEN line_item = 'common_dividends_paid' THEN value END) AS dividends_paid,
    MAX(CASE WHEN line_item = 'common_stock_repurchased' THEN value END) AS stock_repurchased,
    MAX(CASE WHEN line_item = 'rpo' THEN value END) AS rpo,
    MAX(CASE WHEN line_item = 'contract_liabilities' THEN value END) AS contract_liabilities,
    MAX(CASE WHEN line_item = 'operating_lease_liability' THEN value END)
        AS operating_lease_liability,
    MAX(CASE WHEN line_item = 'operating_lease_rou_asset' THEN value END)
        AS operating_lease_rou_asset
FROM dedup
WHERE rn = 1
GROUP BY ticker, fiscal_year, fiscal_period_type
"""


_RATIOS_VIEW_DDL = """
CREATE VIEW IF NOT EXISTS ratios AS
SELECT
    ticker,
    fiscal_year,
    fiscal_period_type,
    period_end,
    currency,
    revenue,
    gross_profit,
    operating_income,
    net_income,
    free_cash_flow,
    total_assets,
    total_equity,
    total_debt,
    capex,
    operating_cash_flow,
    rpo,
    CAST(gross_profit AS REAL) / NULLIF(revenue, 0) AS gross_margin,
    CAST(operating_income AS REAL) / NULLIF(revenue, 0) AS operating_margin,
    CAST(net_income AS REAL) / NULLIF(revenue, 0) AS net_margin,
    CAST(free_cash_flow AS REAL) / NULLIF(revenue, 0) AS fcf_margin,
    CAST(net_income AS REAL) / NULLIF(total_assets, 0) AS roa,
    CAST(net_income AS REAL) / NULLIF(total_equity, 0) AS roe,
    CAST(operating_income AS REAL) / NULLIF(total_assets, 0) AS roa_op,
    CAST(capex AS REAL) / NULLIF(revenue, 0) AS capex_intensity
FROM metrics
"""


_V_QUARTERS_VIEW_DDL = """
CREATE VIEW IF NOT EXISTS v_quarters AS
SELECT
    ff.ticker,
    ff.period_end,
    ff.fiscal_period_type,
    CAST(STRFTIME('%Y', ff.period_end) AS INTEGER) AS calendar_year,
    CASE
        WHEN CAST(STRFTIME('%m', ff.period_end) AS INTEGER) BETWEEN 1 AND 3 THEN 1
        WHEN CAST(STRFTIME('%m', ff.period_end) AS INTEGER) BETWEEN 4 AND 6 THEN 2
        WHEN CAST(STRFTIME('%m', ff.period_end) AS INTEGER) BETWEEN 7 AND 9 THEN 3
        ELSE 4
    END AS calendar_quarter
FROM financial_facts ff
GROUP BY ff.ticker, ff.period_end, ff.fiscal_period_type
"""


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # SQLite views referencing financial_facts must be dropped before
    # batch_alter_table rebuilds the table — the table-rebuild mid-state
    # leaves the view pointing at a dropped name. The views are recreated
    # at the end of upgrade() from the canonical DDL.
    op.execute("DROP VIEW IF EXISTS ratios")
    op.execute("DROP VIEW IF EXISTS metrics")
    op.execute("DROP VIEW IF EXISTS v_quarters")

    # ----- financial_facts -----
    ff_cols = {c["name"] for c in inspector.get_columns("financial_facts")}
    # SQLite has no ALTER for FK constraints — use batch_alter_table so
    # alembic does a copy-and-move table rebuild. FKs must be named explicitly
    # under batch mode.
    if "extracted_by" not in ff_cols or "supersedes_id" not in ff_cols:
        with op.batch_alter_table("financial_facts") as batch:
            if "extracted_by" not in ff_cols:
                batch.add_column(
                    sa.Column("extracted_by", sa.String(length=64), nullable=True)
                )
            if "supersedes_id" not in ff_cols:
                batch.add_column(
                    sa.Column(
                        "supersedes_id",
                        sa.Integer(),
                        sa.ForeignKey(
                            "financial_facts.id",
                            name="fk_financial_facts_supersedes",
                        ),
                        nullable=True,
                    )
                )

    # Re-inspect after the rebuild — index names sometimes change shape
    # under batch_alter_table.
    inspector = sa.inspect(bind)
    ff_idx = {idx["name"] for idx in inspector.get_indexes("financial_facts")}
    if "idx_financial_facts_restatement_lookup" not in ff_idx:
        op.create_index(
            "idx_financial_facts_restatement_lookup",
            "financial_facts",
            ["ticker", "period_end", "fiscal_period_type", "line_item"],
        )

    # ----- kpi_facts -----
    inspector = sa.inspect(bind)
    kf_cols = {c["name"] for c in inspector.get_columns("kpi_facts")}
    needs_kpi_batch = any(
        c not in kf_cols for c in ("confidence", "extracted_by", "supersedes_id")
    )
    if needs_kpi_batch:
        with op.batch_alter_table("kpi_facts") as batch:
            if "confidence" not in kf_cols:
                batch.add_column(
                    sa.Column(
                        "confidence",
                        sa.Float(),
                        nullable=False,
                        server_default="1.0",
                    )
                )
            if "extracted_by" not in kf_cols:
                batch.add_column(
                    sa.Column("extracted_by", sa.String(length=64), nullable=True)
                )
            if "supersedes_id" not in kf_cols:
                batch.add_column(
                    sa.Column(
                        "supersedes_id",
                        sa.Integer(),
                        sa.ForeignKey(
                            "kpi_facts.id", name="fk_kpi_facts_supersedes"
                        ),
                        nullable=True,
                    )
                )

    # ----- documents -----
    inspector = sa.inspect(bind)
    doc_cols = {c["name"] for c in inspector.get_columns("documents")}
    if "source_quality_tier" not in doc_cols:
        op.add_column(
            "documents",
            sa.Column(
                "source_quality_tier",
                sa.String(length=32),
                nullable=False,
                server_default="fmp_normalized",
            ),
        )
        # Backfill from source_type. The DEFAULT covers any source_type we
        # didn't explicitly map (forward-compat with new SourceType enum
        # values added between this migration and the schema's awareness).
        backfill_sql = sa.text(
            "UPDATE documents SET source_quality_tier = :tier "
            "WHERE source_type = :source_type"
        )
        for source_type, tier in _BACKFILL_TIER_BY_SOURCE_TYPE:
            bind.execute(backfill_sql, {"tier": tier, "source_type": source_type})

    # Backfill `extracted_by` on existing fact rows by joining through the
    # source document's source_type. This is opportunistic — we'd rather
    # have *some* provenance label than NULL. New extractor code should
    # write `extracted_by` explicitly going forward.
    bind.execute(
        sa.text(
            "UPDATE financial_facts "
            "SET extracted_by = (SELECT source_type FROM documents WHERE documents.id = financial_facts.source_doc_id) "
            "WHERE extracted_by IS NULL"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE kpi_facts "
            "SET extracted_by = (SELECT source_type FROM documents WHERE documents.id = kpi_facts.source_doc_id) "
            "WHERE extracted_by IS NULL"
        )
    )

    # Recreate the views dropped at the top of upgrade(). The DDL must
    # match the original from migrations 0012 (metrics + ratios) and 0042
    # (v_quarters) — keep these in sync if those views ever change shape.
    op.execute(_METRICS_VIEW_DDL)
    op.execute(_RATIOS_VIEW_DDL)
    op.execute(_V_QUARTERS_VIEW_DDL)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Drop views before batch_alter_table; recreate after (same rationale
    # as upgrade()).
    op.execute("DROP VIEW IF EXISTS ratios")
    op.execute("DROP VIEW IF EXISTS metrics")
    op.execute("DROP VIEW IF EXISTS v_quarters")

    ff_idx = {idx["name"] for idx in inspector.get_indexes("financial_facts")}
    if "idx_financial_facts_restatement_lookup" in ff_idx:
        op.drop_index(
            "idx_financial_facts_restatement_lookup",
            table_name="financial_facts",
        )

    # SQLite has no DROP COLUMN; batch_alter_table emulates it via table
    # rebuild. Same for FK-bearing columns.
    with op.batch_alter_table("financial_facts") as batch:
        ff_cols = {c["name"] for c in inspector.get_columns("financial_facts")}
        if "supersedes_id" in ff_cols:
            batch.drop_column("supersedes_id")
        if "extracted_by" in ff_cols:
            batch.drop_column("extracted_by")

    inspector = sa.inspect(bind)
    with op.batch_alter_table("kpi_facts") as batch:
        kf_cols = {c["name"] for c in inspector.get_columns("kpi_facts")}
        if "supersedes_id" in kf_cols:
            batch.drop_column("supersedes_id")
        if "extracted_by" in kf_cols:
            batch.drop_column("extracted_by")
        if "confidence" in kf_cols:
            batch.drop_column("confidence")

    inspector = sa.inspect(bind)
    with op.batch_alter_table("documents") as batch:
        doc_cols = {c["name"] for c in inspector.get_columns("documents")}
        if "source_quality_tier" in doc_cols:
            batch.drop_column("source_quality_tier")

    op.execute(_METRICS_VIEW_DDL)
    op.execute(_RATIOS_VIEW_DDL)
    op.execute(_V_QUARTERS_VIEW_DDL)
