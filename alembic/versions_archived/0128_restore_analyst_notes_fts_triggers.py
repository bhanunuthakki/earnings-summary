"""Restore the analyst_notes_fts sync triggers dropped by 0125's batch recreate.

0118 created the external-content FTS5 index ``analyst_notes_fts`` over
``analyst_notes.body`` plus three sync triggers (ai / ad / au). External-content
FTS5 does NOT auto-sync — the triggers ARE the sync. 0125 (and again 0127) widen
/ narrow the analyst_notes ``kind`` CHECK via ``batch_alter_table``, which in
SQLite recreates the table and silently DROPS its triggers. Since 0125 the
triggers have been gone, so every musing inserted afterwards was invisible to
``search_musings`` — the FTS ``MATCH`` ran without error but returned nothing,
and the reader never fell back to LIKE. Ledger musing search was dead.

This migration re-creates the three triggers (verbatim from 0118) and rebuilds
the external-content index so rows inserted while the triggers were missing
become searchable again. Idempotent + guarded: if ``analyst_notes_fts`` does
not exist (FTS5 not compiled in at 0118) it is a no-op and the reader keeps
using its LIKE fallback.

GOTCHA for future migrations: any ``batch_alter_table('analyst_notes')`` drops
these triggers again and must re-run this restore.

Revision ID: 0128_restore_analyst_notes_fts_triggers
Revises: 0127_retire_influence_kind
Create Date: 2026-07-01
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0128_restore_analyst_notes_fts_triggers"
down_revision: str | Sequence[str] | None = "0127_retire_influence_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Verbatim from 0118 — the external-content FTS5 sync triggers.
_AI = (
    "CREATE TRIGGER analyst_notes_fts_ai AFTER INSERT ON analyst_notes BEGIN "
    "INSERT INTO analyst_notes_fts(rowid, body) VALUES (new.id, new.body); END"
)
_AD = (
    "CREATE TRIGGER analyst_notes_fts_ad AFTER DELETE ON analyst_notes BEGIN "
    "INSERT INTO analyst_notes_fts(analyst_notes_fts, rowid, body) "
    "VALUES ('delete', old.id, old.body); END"
)
_AU = (
    "CREATE TRIGGER analyst_notes_fts_au AFTER UPDATE ON analyst_notes BEGIN "
    "INSERT INTO analyst_notes_fts(analyst_notes_fts, rowid, body) "
    "VALUES ('delete', old.id, old.body); "
    "INSERT INTO analyst_notes_fts(rowid, body) VALUES (new.id, new.body); END"
)


def upgrade() -> None:
    bind = op.get_bind()
    names = set(sa.inspect(bind).get_table_names())
    if "analyst_notes" not in names or "analyst_notes_fts" not in names:
        return  # FTS5 unavailable at 0118 → reader uses LIKE; nothing to restore
    for trig in ("analyst_notes_fts_ai", "analyst_notes_fts_ad", "analyst_notes_fts_au"):
        with contextlib.suppress(sa.exc.SQLAlchemyError):
            op.execute(f"DROP TRIGGER IF EXISTS {trig}")
    # Rebuild the external-content index from analyst_notes so rows inserted
    # while the triggers were missing become searchable.
    with contextlib.suppress(sa.exc.SQLAlchemyError):
        op.execute("INSERT INTO analyst_notes_fts(analyst_notes_fts) VALUES ('rebuild')")
    op.execute(_AI)
    op.execute(_AD)
    op.execute(_AU)


def downgrade() -> None:
    # The triggers are a repair, not a feature — dropping them returns to the
    # (broken) post-0125 state without touching data.
    for trig in ("analyst_notes_fts_au", "analyst_notes_fts_ad", "analyst_notes_fts_ai"):
        with contextlib.suppress(sa.exc.SQLAlchemyError):
            op.execute(f"DROP TRIGGER IF EXISTS {trig}")
