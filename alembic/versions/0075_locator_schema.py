"""locator_schema — filing identity on documents + sub-document locators on facts.

Why this exists
---------------
P3.1 of directives/master_build_2026_06.md (Wave 3 — Provenance & Governance).
A fact today traces to a *file* (documents.file_path) but not to a *filing*,
and never to a position inside the source. CapIQ/BamSEC-grade provenance
needs both halves:

  1. **Filing identity** — ``documents`` gains ``accession_number`` (the SEC
     EDGAR accession in canonical dashed form, e.g. ``0001628280-21-010389``)
     and ``filing_date`` (ISO ``YYYY-MM-DD`` the filing hit EDGAR). Together
     they make every SEC-derived document addressable on EDGAR and orderable
     by regulatory time, independent of our fetch time.

  2. **Sub-document locator** — ``financial_facts`` and ``kpi_facts`` gain a
     nullable ``locator`` TEXT(JSON) column saying WHERE inside the source
     document the value was read from. Shape (all keys optional):

         {"section": "<parsed 10-K section key>",
          "transcript_line": <int>,
          "pdf_page": <int>,
          "json_path": "<FMP record pointer>"}

     Canonical contract: directives/data_provenance.md §7; typed model:
     src/models/facts.py::FactLocator. Writers serialize through the model
     (the fact-store insert helpers take pre-serialized JSON), so validity
     is enforced at the write path.

Design notes
------------
* Plain ``op.add_column`` (no batch_alter_table): every new column is
  nullable with no FK/CHECK, so SQLite's native ALTER TABLE ADD COLUMN
  suffices. This deliberately avoids a copy-and-move rebuild of
  financial_facts (~727k rows) and keeps the views that reference it
  (``metrics``, ``v_quarters``) intact — no view drop/recreate dance on
  upgrade. The downgrade DOES rebuild (drop_column under batch mode), so it
  mirrors 0054: drop the dependent views first, recreate them after.
* No DB-level ``json_valid`` CHECK on ``locator``: adding a CHECK to an
  existing SQLite table forces the same full-table rebuild (0071 precedent —
  it deliberately left kpi_facts unrebuilt). Validity rides on the writers
  + tests instead.
* Backfill is a CLI, not in-migration: deriving accession/filing_date needs
  file parsing (EDGAR URL shapes, companyfacts JSON ``filed`` dates) — same
  rationale as the ir_doc backfill (data_provenance.md §6). Run
  ``python execution/backfill_document_accessions.py`` after upgrading.
* Guards: ``_has_table`` for stamped partial-schema test DBs (a tmp DB
  stamped at 0072 and upgraded to head never creates the fact tables — this
  migration must no-op there), column-existence checks for idempotence.
* ``ix_documents_accession_number`` is partial (NOT NULL only): the
  accession is the documented idempotence key for sec_xbrl ingestion
  (data_provenance.md §4) and the lookup key for "is this filing already
  ingested?", but most documents rows (FMP endpoint dumps, IR PDFs) have no
  accession — the partial index keeps them out.

Revision ID: 0075_locator_schema
Revises: 0074_analyst_notes
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0075_locator_schema"
down_revision: str | Sequence[str] | None = "0074_analyst_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# View DDL — copied verbatim from migration 0054 (which copied 0012 + 0042).
# The downgrade's batch_alter_table rebuild of financial_facts drops these
# implicitly; they are recreated at the end of downgrade(). Keep in sync with
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


def _has_table(inspector: sa.Inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # ----- documents: filing identity -----
    if _has_table(insp, "documents"):
        doc_cols = {c["name"] for c in insp.get_columns("documents")}
        if "accession_number" not in doc_cols:
            op.add_column(
                "documents",
                sa.Column("accession_number", sa.Text(), nullable=True),
            )
        if "filing_date" not in doc_cols:
            op.add_column(
                "documents",
                sa.Column("filing_date", sa.Text(), nullable=True),
            )
        doc_idx = {idx["name"] for idx in insp.get_indexes("documents")}
        if "ix_documents_accession_number" not in doc_idx:
            op.create_index(
                "ix_documents_accession_number",
                "documents",
                ["accession_number"],
                sqlite_where=sa.text("accession_number IS NOT NULL"),
            )

    # ----- fact tables: sub-document locator -----
    for table in ("financial_facts", "kpi_facts"):
        if not _has_table(insp, table):
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        if "locator" not in cols:
            op.add_column(table, sa.Column("locator", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # SQLite emulates DROP COLUMN via a batch_alter_table copy-and-move
    # rebuild, which implicitly drops views referencing the table — drop the
    # dependent views first and recreate them after (same dance as 0054).
    # Guarded on financial_facts existing: SQLite doesn't validate a view's
    # SELECT at CREATE time, so recreating the views on a partial-schema test
    # DB without the fact tables would poison every later ALTER TABLE in the
    # downgrade chain ("error in view metrics: no such table").
    ff_present = _has_table(insp, "financial_facts")
    if ff_present:
        op.execute("DROP VIEW IF EXISTS ratios")
        op.execute("DROP VIEW IF EXISTS metrics")
        op.execute("DROP VIEW IF EXISTS v_quarters")

    if _has_table(insp, "documents"):
        doc_idx = {idx["name"] for idx in insp.get_indexes("documents")}
        if "ix_documents_accession_number" in doc_idx:
            op.drop_index("ix_documents_accession_number", table_name="documents")
        doc_cols = {c["name"] for c in insp.get_columns("documents")}
        if {"accession_number", "filing_date"} & doc_cols:
            with op.batch_alter_table("documents") as batch:
                if "filing_date" in doc_cols:
                    batch.drop_column("filing_date")
                if "accession_number" in doc_cols:
                    batch.drop_column("accession_number")

    for table in ("financial_facts", "kpi_facts"):
        if not _has_table(insp, table):
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        if "locator" in cols:
            with op.batch_alter_table(table) as batch:
                batch.drop_column("locator")

    if ff_present:
        op.execute(_METRICS_VIEW_DDL)
        op.execute(_RATIOS_VIEW_DDL)
        op.execute(_V_QUARTERS_VIEW_DDL)
