"""tracked_companies_evaluation_list_type — add `evaluation` to the list_type CHECK.

Introduces `evaluation` as a distinct list_type alongside portfolio/watchlist.
Semantics: a ticker the user is screening (eval-flavor brief produced) but has
not committed to monitoring. Watchlist remains a holding pen with no auto-brief.

SQLite cannot ALTER a CHECK constraint in place, so this migration recreates
`tracked_companies` with the expanded CHECK and copies the data through. The
two partial indexes (`ix_tracked_companies_active` from 0020 and
`ix_tracked_companies_brief_dirty` from 0022) are recreated on the new table.

Triggers on `tracked_companies` itself: none. The 0026 brief_dirty triggers
live on `financial_facts` / `kpi_facts` / `segment_facts` and write into
`tracked_companies` by ticker — recreating the destination is transparent
to them.

Revision ID: 0028_tracked_companies_evaluation_list_type
Revises: 0027_drop_output_consolidator_remnants
Create Date: 2026-05-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028_tracked_companies_evaluation_list_type"
down_revision: str | Sequence[str] | None = "0027_drop_output_consolidator_remnants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_COLUMN_LIST = (
    "id, user_id, ticker, name, list_type, added_at, "
    "sec_validated, ir_url, model_url, "
    "publishes_release, publishes_slides, publishes_transcript, "
    "fmp_data_upto, manual_data_quarters, fmp_data_saved, "
    "instrument_type, filing_regime, fiscal_year_end, "
    "archived_at, brief_dirty"
)


def _recreate(check_constraint: str) -> None:
    conn = op.get_bind()
    # `legacy_alter_table=ON` tells SQLite >=3.25 not to revalidate triggers
    # during ALTER TABLE...RENAME. The 0026 brief_dirty triggers on
    # financial_facts / kpi_facts / segment_facts / dcf_runs reference
    # `tracked_companies` by name; without this PRAGMA the RENAME step trips on
    # the orphaned-during-rename trigger validation. The triggers' SQL is
    # unchanged and resolves correctly once the rename completes.
    conn.execute(sa.text("PRAGMA legacy_alter_table = ON"))
    try:
        conn.execute(sa.text(f"""
            CREATE TABLE tracked_companies_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER DEFAULT 1,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL,
                list_type TEXT NOT NULL CHECK({check_constraint}),
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sec_validated BOOLEAN DEFAULT 0,
                ir_url TEXT DEFAULT NULL,
                model_url TEXT DEFAULT NULL,
                publishes_release BOOLEAN DEFAULT 0,
                publishes_slides BOOLEAN DEFAULT 0,
                publishes_transcript BOOLEAN DEFAULT 0,
                fmp_data_upto TEXT DEFAULT NULL,
                manual_data_quarters TEXT DEFAULT '[]',
                fmp_data_saved BOOLEAN DEFAULT 0,
                instrument_type VARCHAR,
                filing_regime VARCHAR,
                fiscal_year_end VARCHAR(5),
                archived_at TIMESTAMP,
                brief_dirty BOOLEAN DEFAULT 0 NOT NULL,
                UNIQUE(user_id, ticker)
            )
        """))
        conn.execute(sa.text(
            f"INSERT INTO tracked_companies_new ({_COLUMN_LIST}) "
            f"SELECT {_COLUMN_LIST} FROM tracked_companies"
        ))
        conn.execute(sa.text("DROP TABLE tracked_companies"))
        conn.execute(sa.text("ALTER TABLE tracked_companies_new RENAME TO tracked_companies"))
    finally:
        conn.execute(sa.text("PRAGMA legacy_alter_table = OFF"))
    op.create_index(
        "ix_tracked_companies_active",
        "tracked_companies",
        ["user_id", "list_type"],
        unique=False,
        sqlite_where=sa.text("archived_at IS NULL"),
    )
    op.create_index(
        "ix_tracked_companies_brief_dirty",
        "tracked_companies",
        ["brief_dirty"],
        unique=False,
        sqlite_where=sa.text("brief_dirty = 1"),
    )


def upgrade() -> None:
    _recreate(
        "list_type IN ('portfolio','watchlist','evaluation','etf','index_member','none')"
    )


def downgrade() -> None:
    # Refuse to downgrade if any rows are on the new list_type — they'd
    # silently lose their classification or fail the old CHECK.
    conn = op.get_bind()
    count = conn.execute(
        sa.text("SELECT COUNT(*) FROM tracked_companies WHERE list_type = 'evaluation'")
    ).scalar() or 0
    if count > 0:
        raise RuntimeError(
            f"Cannot downgrade: {count} rows still on list_type='evaluation'. "
            "Reclassify them (e.g. to watchlist) before re-running downgrade."
        )
    _recreate(
        "list_type IN ('portfolio','watchlist','etf','index_member','none')"
    )
