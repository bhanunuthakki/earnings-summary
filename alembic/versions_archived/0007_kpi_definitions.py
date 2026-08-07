"""kpi_definitions — per-ticker KPI registry, seeded from the IR-override memory.

Encodes the IR-override registry from project_data_process.md so ingestion can
route data-drivenly: any ticker with `primary_source != 'fmp'` here pulls
ir_doc / sec_xbrl / transcript_audio in addition to the FMP base set.

Revision ID: 0007_kpi_definitions
Revises: 0006_runs_and_validation
Create Date: 2026-05-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_kpi_definitions"
down_revision: str | Sequence[str] | None = "0006_runs_and_validation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (ticker, kpi_name, primary_source, fallback_source, ir_url)
_SEED: list[tuple[str, str, str, str | None, str | None]] = [
    (
        "GOOG",
        "Segment OI (Services / Cloud / Other Bets); Cloud RPO; paid-clicks; CPC; YouTube ads",
        "sec_xbrl",
        "ir_doc",
        None,
    ),
    (
        "META",
        "Family-of-Apps OI vs Reality Labs OI; DAP; ad impressions; ad price; capex by category",
        "sec_xbrl",
        "ir_doc",
        None,
    ),
    ("AMZN", "AWS / NA-retail / Intl OI; ads revenue; Prime metrics", "sec_xbrl", "ir_doc", None),
    (
        "NOW",
        "cRPO; RPO; NRR; customers >$1M ACV; sub gross margin",
        "ir_doc",
        "transcript_audio",
        None,
    ),
    (
        "VEEV",
        "Subscription rev split; billings; NRR; customer count",
        "ir_doc",
        "transcript_audio",
        None,
    ),
    (
        "RBRK",
        "Subscription ARR; NRR; $100k+ customers; FCF margin",
        "ir_doc",
        "transcript_audio",
        None,
    ),
    (
        "WIX",
        "Bookings; Creative Subs vs Business Solutions; take rate; GPV",
        "ir_doc",
        "transcript_audio",
        None,
    ),
    (
        "NVO",
        "Drug-level rev (Wegovy/Ozempic/Rybelsus); insulin volume; region mix; pipeline",
        "ir_doc",
        "sec_xbrl",
        None,
    ),
    (
        "MELI",
        "GMV; items sold; TPV; fintech MAU; credit NPL; take rate",
        "ir_doc",
        "transcript_audio",
        None,
    ),
    (
        "NU",
        "15-90d NPL; 90+ NPL; NIM; NIMAL; ARPAC; MAU; deposit cost; cost-to-serve",
        "ir_doc",
        "sec_xbrl",
        "https://investors.nu/",
    ),
    (
        "BN",
        "Distributable earnings/share; fee-bearing capital; BAM stake; deployed capital",
        "ir_doc",
        "sec_xbrl",
        None,
    ),
    (
        "LLY",
        "Drug-level rev (Mounjaro/Zepbound/Trulicity/Verzenio/Taltz); pipeline readouts",
        "sec_xbrl",
        "ir_doc",
        None,
    ),
    ("AMAT", "Bookings/backlog by segment; DRAM vs NAND mix", "ir_doc", "transcript_audio", None),
    ("FCX", "Copper volumes; gold by-product; AISC; Grasberg ramp", "sec_xbrl", "ir_doc", None),
    (
        "WY",
        "SF housing demand; mill volumes; log/lumber pricing",
        "sec_xbrl",
        "transcript_audio",
        None,
    ),
    (
        "JPM",
        "NII; NIM; ROTCE; CET1; charge-offs; IB fees; deposit beta",
        "ir_doc",
        "sec_xbrl",
        None,
    ),
    ("TOL", "Orders; backlog; ASP; community count; cancellation rate", "ir_doc", "sec_xbrl", None),
    ("ASML", "Bookings (EUV/DUV); backlog; China revenue mix", "ir_doc", "transcript_audio", None),
    ("BHP", "Production by commodity; AISC; capex by project", "ir_doc", "sec_xbrl", None),
    (
        "RIO",
        "Production by commodity; AISC; project capex (Simandou, Oyu Tolgoi)",
        "ir_doc",
        "sec_xbrl",
        None,
    ),
    (
        "VALE",
        "Iron ore production; premium product mix; AISC; base metals",
        "ir_doc",
        "sec_xbrl",
        None,
    ),
    ("ABNB", "Nights & Experiences; GBV; ADR; take rate; regional mix", "sec_xbrl", "ir_doc", None),
    (
        "SOFI",
        "Members; products per member; NIM; charge-offs; Galileo accounts",
        "sec_xbrl",
        "ir_doc",
        None,
    ),
    (
        "LMND",
        "In-force premium; customer count; loss ratio; gross loss ratio; IFP/customer",
        "sec_xbrl",
        "ir_doc",
        None,
    ),
    (
        "HDB",
        "NIM; gross/net NPA; deposit growth; advances growth; cost-to-income",
        "ir_doc",
        "manual_csv",
        None,
    ),
    ("FNV", "GEOs sold; mining royalty mix; energy royalty mix", "sec_xbrl", "ir_doc", None),
    ("CNQ", "Production by basin; AECO/WCS realized prices; FCF", "sec_xbrl", "ir_doc", None),
]


def upgrade() -> None:
    op.create_table(
        "kpi_definitions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("unit", sa.String(), nullable=False, server_default="actual"),
        sa.Column("primary_source", sa.String(), nullable=False),
        sa.Column("fallback_source", sa.String(), nullable=True),
        sa.Column("ir_url", sa.String(), nullable=True),
        sa.Column("threshold_tier", sa.String(), nullable=True),
        sa.Column("threshold_low", sa.Float(), nullable=True),
        sa.Column("threshold_high", sa.Float(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.UniqueConstraint("ticker", "name", name="uq_kpi_definitions_ticker_name"),
    )
    op.create_index("ix_kpi_definitions_ticker", "kpi_definitions", ["ticker"])

    insert_sql = sa.text(
        "INSERT INTO kpi_definitions "
        "(ticker, name, unit, primary_source, fallback_source, ir_url) "
        "VALUES (:ticker, :name, 'actual', :primary, :fallback, :ir_url)"
    )
    bind = op.get_bind()
    for ticker, name, primary, fallback, ir_url in _SEED:
        bind.execute(
            insert_sql,
            {
                "ticker": ticker,
                "name": name,
                "primary": primary,
                "fallback": fallback,
                "ir_url": ir_url,
            },
        )


def downgrade() -> None:
    op.drop_index("ix_kpi_definitions_ticker", table_name="kpi_definitions")
    op.drop_table("kpi_definitions")
