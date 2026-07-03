"""Widen ``ck_advisor_memos_kind`` to admit 'position_review'.

The position-review service (``advisor.position_review.review_position``) has
shipped a fourth memo kind — ``advisor.store.MEMO_KINDS`` has carried
``'position_review'`` since that module landed — but 0077's CHECK constraint
was never widened to match. Every unit test builds ``review_position`` with
``persist=False`` or a monkeypatched ``persist_memo``, so nothing ever
INSERTed a real ``kind='position_review'`` row against a real (alembic-built)
DB; the mismatch only surfaced at a live smoke test against prod, which is
still pinned at 0077's original three-kind CHECK
(``'next_dollar', 'swap_check', 'socratic'``) and raises
``sqlite3.IntegrityError: CHECK constraint failed: ck_advisor_memos_kind`` the
moment a position-review memo tries to persist.

SQLite cannot ALTER a CHECK in place — batch-recreate the table, same
guarded + idempotent pattern as 0077 (``analyst_notes`` widening) and 0086
(``alerts`` widening). ``batch_alter_table`` re-copies every column
(plain-INTEGER ``note_id``/``ledger_entry_id`` — no ``REFERENCES``, per the
FK-poisoning invariant) and recreates ``ix_advisor_memos_user_kind_created`` +
``ix_advisor_memos_user_score_status`` automatically as part of the table
recreate; both are re-declared explicitly below so schema intent stays
readable and downgrade has something concrete to drop.

Revision ID: 0140_advisor_memos_position_review_kind
Revises: 0139_artifact_brief_budget
Create Date: 2026-07-02
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0140_advisor_memos_position_review_kind"
down_revision: str | Sequence[str] | None = "0139_artifact_brief_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirror of advisor.store.MEMO_KINDS after this migration.
_KINDS_WIDENED = "('next_dollar', 'swap_check', 'socratic', 'position_review')"
_KINDS_ORIGINAL = "('next_dollar', 'swap_check', 'socratic')"


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in insp.get_table_names()


def _has_index(insp: sa.Inspector, table: str, name: str) -> bool:
    return any(ix["name"] == name for ix in insp.get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not _has_table(insp, "advisor_memos"):
        return  # partial test DB predating 0077 — nothing to widen

    with op.batch_alter_table("advisor_memos") as batch:
        with contextlib.suppress(ValueError, sa.exc.OperationalError):  # already absent
            batch.drop_constraint("ck_advisor_memos_kind", type_="check")
        batch.create_check_constraint("ck_advisor_memos_kind", f"kind IN {_KINDS_WIDENED}")

    insp = sa.inspect(bind)
    if not _has_index(insp, "advisor_memos", "ix_advisor_memos_user_kind_created"):
        op.create_index(
            "ix_advisor_memos_user_kind_created",
            "advisor_memos",
            ["user_id", "kind", "created_at"],
        )
    if not _has_index(insp, "advisor_memos", "ix_advisor_memos_user_score_status"):
        op.create_index(
            "ix_advisor_memos_user_score_status",
            "advisor_memos",
            ["user_id", "score_status"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not _has_table(insp, "advisor_memos"):
        return

    # Restore the original three-kind CHECK only if no position_review rows
    # exist — otherwise the recreate would fail row validation; leave the
    # widened constraint in place (downgrade stays safe, never destructive).
    widened_rows = bind.execute(
        sa.text("SELECT COUNT(*) FROM advisor_memos WHERE kind = 'position_review'")
    ).scalar()
    if not widened_rows:
        with op.batch_alter_table("advisor_memos") as batch:
            with contextlib.suppress(ValueError, sa.exc.OperationalError):
                batch.drop_constraint("ck_advisor_memos_kind", type_="check")
            batch.create_check_constraint("ck_advisor_memos_kind", f"kind IN {_KINDS_ORIGINAL}")
