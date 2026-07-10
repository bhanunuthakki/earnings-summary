"""ETF published-data enrichment — country look-through + basket characteristics.

The thematic-ETF evaluation lane (directives/etf_data.md) sources holdings from
SEC N-PORT filings and issuer fund pages instead of the plan-gated FMP ETF
endpoints. Two enrichments the 0044 tables need for that:

  etf_holdings.country          — per-constituent domicile from N-PORT
                                  ``invCountry`` (ISO-3166 alpha-2). Powers the
                                  geography look-through ("does AVDV actually
                                  close my intl sleeve gap?") that a single
                                  profile-level ``domicile`` cannot.

  etf_profile characteristics   — basket-level valuation published by issuers
                                  (pe_ratio, pb_ratio as decimals-of-1x, e.g.
                                  14.3 = 14.3x; weighted_avg_mktcap_usd_m in
                                  millions USD) with their own as-of stamp and
                                  source, because characteristics and the
                                  profile row refresh on different cadences
                                  from different publishers.

Plain nullable ADD COLUMNs — no batch rebuild needed in SQLite. Guarded per
the stamped-DB convention (0142): test substrates may lack the 0044 tables
entirely.

Revision ID: 0144_etf_published_data
Revises: 0143_v_thesis_status_view
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0144_etf_published_data"
down_revision: str | Sequence[str] | None = "0143_v_thesis_status_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _profile_columns() -> tuple[sa.Column[object], ...]:
    # Fresh Column objects per call — a Column can only be bound to one table.
    return (
        sa.Column("pe_ratio", sa.Float(), nullable=True),
        sa.Column("pb_ratio", sa.Float(), nullable=True),
        sa.Column("weighted_avg_mktcap_usd_m", sa.Float(), nullable=True),
        sa.Column("characteristics_as_of", sa.Date(), nullable=True),
        sa.Column("characteristics_source", sa.String(length=32), nullable=True),
    )


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in insp.get_table_names()


def _has_column(insp: sa.Inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_table(insp, "etf_holdings") and not _has_column(insp, "etf_holdings", "country"):
        op.add_column("etf_holdings", sa.Column("country", sa.String(length=64), nullable=True))
    if _has_table(insp, "etf_profile"):
        for col in _profile_columns():
            if not _has_column(insp, "etf_profile", str(col.name)):
                op.add_column("etf_profile", col)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_table(insp, "etf_holdings") and _has_column(insp, "etf_holdings", "country"):
        with op.batch_alter_table("etf_holdings") as batch:
            batch.drop_column("country")
    if _has_table(insp, "etf_profile"):
        present = [
            str(c.name) for c in _profile_columns() if _has_column(insp, "etf_profile", str(c.name))
        ]
        if present:
            with op.batch_alter_table("etf_profile") as batch:
                for name in present:
                    batch.drop_column(name)
