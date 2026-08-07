"""drop_dead_tables — DROP brief_provenance_log + expected_earnings.

Both tables were created (0023, 0025) with a planned consumer that was
never built, and have been writer-only ever since:

  - brief_provenance_log: written on every build_artifacts.py render
    (audit trail of source supersession). Never SELECTed; the design
    intent was for future provenance UIs but Phase 4 chose section_status
    as a proxy. Writer + this table removed together.

  - expected_earnings: populated by earnings_calendar_watcher.py from
    the FMP earnings calendar cache (queue for the daily worker to
    drain). Never SELECTed — the daily worker drains
    tracked_companies.brief_dirty (set by the 0026 triggers on fact-table
    inserts) instead. Watcher + cron + table removed together.

HeroQuoteSection (the other "designed but unused" candidate from the
8b audit) is intentionally KEPT — it's workspace-renderer-reserved per
src/report/models.py:770, and the workspace format is being actively
iterated on outside this repo.

Revision ID: 0031_drop_dead_tables
Revises: 0030_kpi_facts_logical_unique
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031_drop_dead_tables"
down_revision: str | Sequence[str] | None = "0030_kpi_facts_logical_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_brief_provenance_ticker_date")
    op.execute("DROP TABLE IF EXISTS brief_provenance_log")
    op.execute("DROP INDEX IF EXISTS ix_expected_earnings_pending")
    op.execute("DROP INDEX IF EXISTS ux_expected_earnings_ticker_date")
    op.execute("DROP TABLE IF EXISTS expected_earnings")


def downgrade() -> None:
    op.create_table(
        "brief_provenance_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("generation_date", sa.String(length=10), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("sources_used", sa.Text(), nullable=False),
        sa.Column("sections_status", sa.Text(), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("artifact_path", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_brief_provenance_ticker_date",
        "brief_provenance_log",
        ["ticker", "generation_date"],
    )

    op.create_table(
        "expected_earnings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("expected_date", sa.String(length=10), nullable=False),
        sa.Column("fiscal_period_end", sa.String(length=10)),
        sa.Column("fiscal_period_label", sa.String(length=16)),
        sa.Column("detected_source", sa.String(length=16), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("fact_seen_at", sa.DateTime()),
    )
    op.create_index(
        "ux_expected_earnings_ticker_date",
        "expected_earnings",
        ["ticker", "expected_date"],
        unique=True,
    )
    op.create_index(
        "ix_expected_earnings_pending",
        "expected_earnings",
        ["expected_date"],
        sqlite_where=sa.text("fact_seen_at IS NULL"),
    )
