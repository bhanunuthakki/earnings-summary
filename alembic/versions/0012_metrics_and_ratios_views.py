"""metrics_and_ratios_views — SQL views for cross-period queries.

`metrics` pivots financial_facts (long-form) into one row per (ticker,
fiscal_year, fiscal_period_type), picking the latest source_doc_id per
line_item to dedupe across multiple ingestions of the same period.

Bucketing is by (ticker, fiscal_year, fiscal_period_type) — *not* by exact
period_end — because the FMP regular-statement endpoints and the as-reported
endpoints sometimes report period_end one day apart for the same fiscal close
(e.g. GOOG FY2025: regular returns 2025-12-31; as_reported returns 2025-12-30).
The bucket key tolerates that drift; the view's `period_end` column shows the
maximum of the variants for clarity.

`ratios` adds derived margins and returns over the metrics columns. All
ratios use NULLIF to avoid division-by-zero; consumers see NULL when an
input is zero or missing.

Revision ID: 0012_metrics_and_ratios_views
Revises: 0011_facts_unique_indexes
Create Date: 2026-05-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_metrics_and_ratios_views"
down_revision: str | Sequence[str] | None = "0011_facts_unique_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_METRICS_VIEW = """
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

_RATIOS_VIEW = """
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


def upgrade() -> None:
    op.execute(_METRICS_VIEW)
    op.execute(_RATIOS_VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS ratios")
    op.execute("DROP VIEW IF EXISTS metrics")
