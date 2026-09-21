"""Retain corrected duplicate lifecycle identities without treating them as holdings.

Revision ID: 0044_position_entry_supersession
Revises: 0043_etf_profile_field_evidence (integrated graph)
"""

from __future__ import annotations

from alembic import op

revision = "0044_position_entry_supersession"
down_revision = "0043_etf_profile_field_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE position_entries ADD COLUMN superseded_by_entry_id INTEGER REFERENCES position_entries(id) CHECK(superseded_by_entry_id IS NULL OR superseded_by_entry_id <> id)"
    )
    op.execute("DROP INDEX uq_position_entries_open")
    op.execute(
        "CREATE UNIQUE INDEX uq_position_entries_open ON position_entries(user_id,ticker) WHERE exit_date IS NULL AND superseded_by_entry_id IS NULL"
    )
    op.execute("""CREATE TRIGGER position_entry_supersession_insert
        BEFORE INSERT ON position_entries WHEN NEW.superseded_by_entry_id IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'new lifecycle cannot be superseded at creation'); END""")
    op.execute("""CREATE TRIGGER position_entry_supersession_no_chain
        BEFORE UPDATE OF superseded_by_entry_id ON position_entries
        WHEN NEW.superseded_by_entry_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM position_entries WHERE superseded_by_entry_id=OLD.id)
        BEGIN SELECT RAISE(ABORT, 'lifecycle supersession chains require review'); END""")
    op.execute("""CREATE TRIGGER position_entry_supersession_identity
        BEFORE UPDATE OF superseded_by_entry_id ON position_entries
        WHEN NEW.superseded_by_entry_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM position_entries p WHERE p.id=NEW.superseded_by_entry_id
            AND p.ticker=NEW.ticker AND p.user_id=NEW.user_id
            AND p.superseded_by_entry_id IS NULL)
        BEGIN SELECT RAISE(ABORT, 'invalid lifecycle supersession identity'); END""")
    op.execute("""CREATE TRIGGER position_entry_supersession_retain
        BEFORE UPDATE OF superseded_by_entry_id ON position_entries
        WHEN OLD.superseded_by_entry_id IS NOT NULL
        AND NEW.superseded_by_entry_id IS NOT OLD.superseded_by_entry_id
        BEGIN SELECT RAISE(ABORT, 'lifecycle supersession is immutable'); END""")


def downgrade() -> None:
    if (
        op.get_bind()
        .exec_driver_sql(
            "SELECT 1 FROM position_entries WHERE superseded_by_entry_id IS NOT NULL LIMIT 1"
        )
        .fetchone()
    ):
        raise RuntimeError(
            "Retained lifecycle correction lineage requires explicit recovery, not destructive downgrade"
        )
    for suffix in ("insert", "no_chain", "identity", "retain"):
        op.execute(f"DROP TRIGGER position_entry_supersession_{suffix}")
    op.execute("DROP INDEX uq_position_entries_open")
    op.execute("ALTER TABLE position_entries DROP COLUMN superseded_by_entry_id")
    op.execute(
        "CREATE UNIQUE INDEX uq_position_entries_open ON position_entries(user_id,ticker) "
        "WHERE exit_date IS NULL"
    )
