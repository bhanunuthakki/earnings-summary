"""etf_profile and etf_holdings — first cross-asset detail tables.

Adds two tables that materialize ETFs as a first-class instrument kind in the
schema:

  etf_profile     — one row per ETF ticker; identity + descriptive fields
                    (issuer, AUM, expense ratio, inception, benchmark index,
                    asset class, sector label, etc.). Ticker is the natural
                    primary key — there's exactly one canonical profile per
                    listed ETF symbol.

  etf_holdings    — point-in-time holdings; (ticker, as_of_date,
                    constituent_ticker) primary key. Powers the look-through
                    questions: "what's my net NVDA exposure including SOXX?"

Why no instrument_id FK
-----------------------
Every fact table in this DB joins on ticker string (financial_facts,
kpi_facts, segment_facts, dcf_runs, thesis_state, thesis_evaluations,
management_commitments, expected_earnings, earnings_surprises, predictions,
insights, llm_calls, llm_artifacts, insider_transactions,
exec_comp_packages, …). Adding an instrument_id column on these two new
tables would create asymmetry without any caller able to take advantage.
We stay with ticker-string joins; the registry side (tracked_companies)
holds the kind discriminator (`instrument_type`).

Why no tracked_companies CHECK changes
--------------------------------------
`tracked_companies.list_type` already accepts `'etf'` (legacy artifact from
the FMP backfill). `tracked_companies.instrument_type` already accepts
`'etf'` via the InstrumentType StrEnum. Both columns exist; no DDL change
needed. The separate-orthogonality cleanup (drop `'etf'` from list_type
CHECK) is flagged as Phase B in directives/cross_asset_data_model.md and is
out of scope for this PR.

Revision ID: 0044_etf_profile_and_holdings
Revises: 0043_self_update_triggers
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0044_etf_profile_and_holdings"
down_revision: str | Sequence[str] | None = "0043_self_update_triggers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    # ------------------------------------------------------------------
    # etf_profile — one row per ETF symbol
    # ------------------------------------------------------------------
    if "etf_profile" not in existing:
        op.create_table(
            "etf_profile",
            sa.Column("ticker", sa.String(length=16), primary_key=True),
            sa.Column("name", sa.String(length=255), nullable=True),
            sa.Column("issuer", sa.String(length=128), nullable=True),
            # Decimal expressed as a Float in [0, 1]; e.g. 0.0035 = 35 bps.
            sa.Column("expense_ratio", sa.Float(), nullable=True),
            # AUM in millions USD (matches FMP /etf-info contract).
            sa.Column("aum_usd_m", sa.Float(), nullable=True),
            sa.Column("inception_date", sa.Date(), nullable=True),
            # 'equity' | 'fixed_income' | 'commodity' | 'mixed' | 'currency'
            sa.Column("asset_class", sa.String(length=32), nullable=True),
            sa.Column("benchmark_index", sa.String(length=128), nullable=True),
            sa.Column("domicile", sa.String(length=8), nullable=True),  # 'US' | 'IE' | 'LU'
            sa.Column("listed_exchange", sa.String(length=16), nullable=True),
            # TTM 12-month distribution yield as decimal (0.012 = 1.2%)
            sa.Column("distribution_yield", sa.Float(), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            # Free-form sector label from FMP (e.g. 'Technology', 'Semiconductors').
            sa.Column("sector_label", sa.String(length=64), nullable=True),
            # NAV vs market close — premium/discount tracking. Decimal
            # (0.0005 = 5 bps premium).
            sa.Column("nav", sa.Float(), nullable=True),
            sa.Column("price", sa.Float(), nullable=True),
            sa.Column("premium_discount_pct", sa.Float(), nullable=True),
            sa.Column("source", sa.String(length=32), nullable=False, server_default="fmp"),
            sa.Column("profile_fetched_at", sa.DateTime(), nullable=False),
        )
        op.create_index("idx_etf_profile_issuer", "etf_profile", ["issuer"])
        op.create_index("idx_etf_profile_asset_class", "etf_profile", ["asset_class"])

    # ------------------------------------------------------------------
    # etf_holdings — point-in-time constituent weightings
    # ------------------------------------------------------------------
    if "etf_holdings" not in existing:
        op.create_table(
            "etf_holdings",
            sa.Column("ticker", sa.String(length=16), nullable=False),
            sa.Column("as_of_date", sa.Date(), nullable=False),
            # Allow NULL constituent_ticker for things like 'CASH & EQUIVALENTS'
            # rows that aren't backed by a ticker — they still belong in the table
            # for weight accounting, but won't join to tracked_companies.
            sa.Column("constituent_ticker", sa.String(length=16), nullable=True),
            sa.Column("name", sa.String(length=255), nullable=True),
            # Weight as decimal: 0.0742 = 7.42%
            sa.Column("weight_pct", sa.Float(), nullable=True),
            sa.Column("shares_held", sa.Float(), nullable=True),
            sa.Column("market_value_usd", sa.Float(), nullable=True),
            sa.Column("sector", sa.String(length=64), nullable=True),
            sa.Column("asset_class", sa.String(length=32), nullable=True),
            # 1-based rank by weight at fetch time. Useful for "top 10"
            # filtering without re-sorting.
            sa.Column("rank_position", sa.Integer(), nullable=True),
            sa.Column("source", sa.String(length=32), nullable=False, server_default="fmp"),
            sa.Column("fetched_at", sa.DateTime(), nullable=False),
            # Natural key: one snapshot row per (ETF, as_of, constituent).
            # constituent_ticker can be NULL for cash sleeves — coerce to an
            # empty string in the unique key by using a uniqueness on the
            # COALESCE.  SQLite treats NULLs as distinct in UNIQUE, which would
            # actually permit multiple "cash" rows per (ticker, as_of) — fine.
            sa.PrimaryKeyConstraint(
                "ticker", "as_of_date", "constituent_ticker", name="pk_etf_holdings"
            ),
        )
        op.create_index(
            "idx_etf_holdings_constituent",
            "etf_holdings",
            ["constituent_ticker", "as_of_date"],
        )
        op.create_index(
            "idx_etf_holdings_ticker_date_rank",
            "etf_holdings",
            ["ticker", "as_of_date", "rank_position"],
        )
        op.create_index(
            "idx_etf_holdings_sector",
            "etf_holdings",
            ["sector", "as_of_date"],
        )


def downgrade() -> None:
    for t in ("etf_holdings", "etf_profile"):
        op.execute(f"DROP TABLE IF EXISTS {t}")
