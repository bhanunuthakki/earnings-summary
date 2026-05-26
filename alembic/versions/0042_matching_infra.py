"""matching_infra — segment_aliases, kpi_aliases, fx_rates + period normalization.

Cross-cutting matching infrastructure that solves the per-ticker comparability
problem the portfolio dashboard needs:

  segment_aliases   — same shape as entity_aliases but lighter (used by the
                      segment_facts join path); maps reported segment names to
                      canonical names with effective dates.
  kpi_aliases       — same for kpi_facts: ticker's "Active Customers" → "MAU"
                      → "Monthly Engaged" all resolve to one canonical name.
  fx_rates          — daily FX rates (USD reporting requires translation for NU
                      (BRL), MELI (ARS), ASML (EUR), NVO (DKK), TSM (TWD), etc.).
  quarters          — normalized view: every (ticker, period_end) gets both
                      fiscal-period labels and calendar-quarter projections so
                      the dashboard can show all 11 holdings on one time axis.

The aliases tables overlap with entity_aliases / concept_aliases (Phase 1.5)
intentionally — those are the canonical store, but join performance on the
hot path (financials display) favors a denormalized lookup table here.
Resolver is responsible for keeping them in sync (or we drop these and join
through entity_aliases / concept_aliases — fine if perf allows).

Revision ID: 0042_matching_infra
Revises: 0041_insider_and_exec_comp
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0042_matching_infra"
down_revision: str | Sequence[str] | None = "0041_insider_and_exec_comp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "segment_aliases" not in existing:
        op.create_table(
            "segment_aliases",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            sa.Column("canonical_name", sa.String(length=128), nullable=False),
            sa.Column("reported_name", sa.String(length=128), nullable=False),
            sa.Column("effective_from", sa.DateTime(), nullable=True),
            sa.Column("effective_to", sa.DateTime(), nullable=True),
            sa.Column("evidence_source", sa.String(length=255), nullable=True),
            sa.Column("entity_id", sa.Integer(), nullable=True),  # FK to entities (loose)
            sa.UniqueConstraint(
                "ticker", "reported_name", "effective_from", name="uq_segment_aliases"
            ),
        )
        op.create_index(
            "idx_segment_aliases_lookup",
            "segment_aliases",
            ["ticker", "reported_name"],
        )
        op.create_index(
            "idx_segment_aliases_canonical",
            "segment_aliases",
            ["ticker", "canonical_name"],
        )

    if "kpi_aliases" not in existing:
        op.create_table(
            "kpi_aliases",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=True),  # null = universal
            sa.Column("canonical_name", sa.String(length=128), nullable=False),
            sa.Column("reported_name", sa.String(length=128), nullable=False),
            sa.Column("effective_from", sa.DateTime(), nullable=True),
            sa.Column("effective_to", sa.DateTime(), nullable=True),
            sa.Column("concept_id", sa.Integer(), nullable=True),  # FK to concepts (loose)
            sa.UniqueConstraint(
                "ticker", "reported_name", "effective_from", name="uq_kpi_aliases"
            ),
        )
        op.create_index(
            "idx_kpi_aliases_lookup", "kpi_aliases", ["ticker", "reported_name"]
        )

    if "fx_rates" not in existing:
        op.create_table(
            "fx_rates",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("rate_date", sa.Date(), nullable=False),
            sa.Column("from_ccy", sa.String(length=3), nullable=False),
            sa.Column("to_ccy", sa.String(length=3), nullable=False),
            sa.Column("rate", sa.Numeric(20, 8), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False, server_default="FMP"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("rate_date", "from_ccy", "to_ccy", name="uq_fx_rates"),
        )
        op.create_index("idx_fx_rates_pair", "fx_rates", ["from_ccy", "to_ccy", "rate_date"])

    # Normalized view: gives every (ticker, period_end) row both fiscal-period
    # labels and calendar-quarter projections. Lets the dashboard align all
    # 11 holdings on one time axis even though NU/MELI have different fiscal
    # years.
    # SQLite VIEW is fine — read-only by definition.
    op.execute(
        """
        CREATE VIEW IF NOT EXISTS v_quarters AS
        SELECT
            ff.ticker,
            ff.period_end,
            ff.fiscal_period_type,
            -- Calendar quarter label projected from period_end
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
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_quarters")
    for t in ("fx_rates", "kpi_aliases", "segment_aliases"):
        op.execute(f"DROP TABLE IF EXISTS {t}")
