"""companies_provenance — instrument_type, filing_regime, fiscal_year_end on tracked_companies.

Backfills the 28 names in the user's portfolio + watchlist with their instrument
type and SEC filing regime per the project memory. The other ~170 tickers from
the index-wide FMP backfill stay NULL until classified.

Revision ID: 0001_companies_provenance
Revises: 0000_baseline
Create Date: 2026-05-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_companies_provenance"
down_revision: str | Sequence[str] | None = "0000_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (ticker, instrument_type, filing_regime)
_BACKFILL: list[tuple[str, str, str | None]] = [
    # Portfolio
    ("AMZN", "equity", "10-K"),
    ("GOOG", "equity", "10-K"),
    ("MELI", "adr", "20-F"),
    ("META", "equity", "10-K"),
    ("VEEV", "equity", "10-K"),
    ("NOW", "equity", "10-K"),
    ("WIX", "adr", "20-F"),
    ("NVO", "adr", "20-F"),
    ("RBRK", "equity", "10-K"),
    ("NU", "adr", "20-F"),
    ("BN", "equity", "40-F"),
    ("FLKR", "etf", None),
    # Watchlist
    ("LLY", "equity", "10-K"),
    ("AMAT", "equity", "10-K"),
    ("FCX", "equity", "10-K"),
    ("WY", "equity", "10-K"),
    ("JPM", "equity", "10-K"),
    ("TOL", "equity", "10-K"),
    ("ASML", "adr", "20-F"),
    ("BHP", "adr", "20-F"),
    ("RIO", "adr", "20-F"),
    ("VALE", "adr", "20-F"),
    ("ABNB", "equity", "10-K"),
    ("SOFI", "equity", "10-K"),
    ("LMND", "equity", "10-K"),
    ("HDB", "adr", "20-F"),
    ("FNV", "equity", "40-F"),
    ("CNQ", "equity", "40-F"),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("tracked_companies")}

    if "instrument_type" not in existing_cols:
        op.add_column(
            "tracked_companies",
            sa.Column("instrument_type", sa.String(), nullable=True),
        )
    if "filing_regime" not in existing_cols:
        op.add_column(
            "tracked_companies",
            sa.Column("filing_regime", sa.String(), nullable=True),
        )
    if "fiscal_year_end" not in existing_cols:
        op.add_column(
            "tracked_companies",
            sa.Column("fiscal_year_end", sa.String(length=5), nullable=True),
        )

    update_sql = sa.text(
        "UPDATE tracked_companies "
        "SET instrument_type = :instr, filing_regime = :regime "
        "WHERE ticker = :ticker"
    )
    for ticker, instr, regime in _BACKFILL:
        bind.execute(update_sql, {"instr": instr, "regime": regime, "ticker": ticker})


def downgrade() -> None:
    with op.batch_alter_table("tracked_companies") as batch:
        batch.drop_column("fiscal_year_end")
        batch.drop_column("filing_regime")
        batch.drop_column("instrument_type")
