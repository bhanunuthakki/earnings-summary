"""Widen analyst_notes kind/source CHECKs for The Ledger capture vocab.

The Ledger lands a stream-of-consciousness musing as an ``analyst_notes`` row
with ``kind='musing'``, ``source='capture'`` — both new enum values. The table
carries CHECK constraints (``ck_analyst_notes_kind`` from 0074,
``ck_analyst_notes_source`` widened to admit 'advisor' in 0077), so the code-side
tuple edit in ``src/user_state/notes.py`` is not enough: the DB CHECK must be
widened too or every musing insert fails ``ck_analyst_notes_kind``.

This mirrors how 0077 admitted 'advisor' and 0108 admitted 'restatement':
SQLite cannot ALTER a CHECK, so ``batch_alter_table`` recreates the table with
the widened constraint. Keep in lockstep with ``NOTE_KINDS`` / ``NOTE_SOURCES``.

No new table — purely the two constraint widenings.

Revision ID: 0115_analyst_notes_capture_vocab
Revises: 0114_decision_process_quality
Create Date: 2026-06-28
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0115_analyst_notes_capture_vocab"
down_revision: str | Sequence[str] | None = "0114_decision_process_quality"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors of NOTE_KINDS / NOTE_SOURCES (src/user_state/notes.py) after this run.
_KINDS_WIDENED = "('question', 'decision', 'watch', 'assumption', 'observation', 'musing')"
_KINDS_PRIOR = "('question', 'decision', 'watch', 'assumption', 'observation')"
_SOURCES_WIDENED = "('comment', 'chat', 'alert', 'manual', 'advisor', 'capture')"
_SOURCES_PRIOR = "('comment', 'chat', 'alert', 'manual', 'advisor')"


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in insp.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "analyst_notes"):
        return
    with op.batch_alter_table("analyst_notes") as batch:
        with contextlib.suppress(ValueError, sa.exc.OperationalError):  # already absent
            batch.drop_constraint("ck_analyst_notes_kind", type_="check")
        batch.create_check_constraint("ck_analyst_notes_kind", f"kind IN {_KINDS_WIDENED}")
        with contextlib.suppress(ValueError, sa.exc.OperationalError):
            batch.drop_constraint("ck_analyst_notes_source", type_="check")
        batch.create_check_constraint("ck_analyst_notes_source", f"source IN {_SOURCES_WIDENED}")


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "analyst_notes"):
        return
    # Never destructive: restore the narrower CHECKs only when no row would
    # violate them (the recreate copies rows and would fail validation otherwise).
    has_musing = bind.execute(
        sa.text("SELECT COUNT(*) FROM analyst_notes WHERE kind = 'musing'")
    ).scalar()
    has_capture = bind.execute(
        sa.text("SELECT COUNT(*) FROM analyst_notes WHERE source = 'capture'")
    ).scalar()
    with op.batch_alter_table("analyst_notes") as batch:
        if not has_musing:
            with contextlib.suppress(ValueError, sa.exc.OperationalError):
                batch.drop_constraint("ck_analyst_notes_kind", type_="check")
            batch.create_check_constraint("ck_analyst_notes_kind", f"kind IN {_KINDS_PRIOR}")
        if not has_capture:
            with contextlib.suppress(ValueError, sa.exc.OperationalError):
                batch.drop_constraint("ck_analyst_notes_source", type_="check")
            batch.create_check_constraint("ck_analyst_notes_source", f"source IN {_SOURCES_PRIOR}")
