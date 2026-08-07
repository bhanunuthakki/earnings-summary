"""advisor_memos — the durable record of advisor artifacts (master build P2.3).

One row per generated memo: the next-dollar allocation memo (portfolio-level,
ticker NULL), a swap-discipline check (ticker = the holding, counter_ticker =
the watchlist alternative), and — from P2.4 — the Socratic think-through
decision memo (ticker-scoped, carrying the stance-if-forced + horizon).
``score_status`` ships now so the P2.5 scoring job can walk pending rows
without another schema change; it stays 'pending' until that job exists.

``note_id`` / ``ledger_entry_id`` backlink the memory writes (the directive's
"memos persist as notes + ledger entries"): every memo creates an
analyst_notes row, and ticker-scoped memos additionally append a
thesis_ledger entry. The links make the memory trail auditable from the memo.

Also widens ``ck_analyst_notes_source`` to admit 'advisor' — advisor-written
notes are neither comments, chat, alerts, nor manual entries, and labeling
them honestly keeps the priors anchor's provenance readable.

New-table CHECK constraints follow 0071's policy (constrain enums at the
schema, named constraints, guarded + idempotent ops).

Revision ID: 0077_advisor_memos
Revises: 0076_dcf_runs_over_under_check
Create Date: 2026-06-10
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0077_advisor_memos"
down_revision: str | Sequence[str] | None = "0076_dcf_runs_over_under_check"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = "('next_dollar', 'swap_check', 'socratic')"
_SCORE_STATUSES = "('pending', 'scored', 'unscoreable')"
# Stance vocabulary for the Socratic memo's stance-if-forced (P2.4); NULL for
# evidence+framing memos (next-dollar, swap checks) per the advisor posture.
_STANCES = "('buy', 'add', 'hold', 'trim', 'sell')"

_NOTE_SOURCES_WIDENED = "('comment', 'chat', 'alert', 'manual', 'advisor')"


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in insp.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not _has_table(insp, "advisor_memos"):
        op.create_table(
            "advisor_memos",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "user_id",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'bhanu'"),
            ),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("ticker", sa.Text(), nullable=True),
            sa.Column("counter_ticker", sa.Text(), nullable=True),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("body_md", sa.Text(), nullable=False),
            sa.Column("context_json", sa.Text(), nullable=True),
            sa.Column("stance", sa.Text(), nullable=True),
            sa.Column("horizon_days", sa.Integer(), nullable=True),
            sa.Column(
                "score_status",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'pending'"),
            ),
            sa.Column("note_id", sa.Integer(), nullable=True),
            sa.Column("ledger_entry_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.CheckConstraint(f"kind IN {_KINDS}", name="ck_advisor_memos_kind"),
            sa.CheckConstraint(
                f"score_status IN {_SCORE_STATUSES}", name="ck_advisor_memos_score_status"
            ),
            sa.CheckConstraint(
                f"stance IS NULL OR stance IN {_STANCES}", name="ck_advisor_memos_stance"
            ),
        )
        op.create_index(
            "ix_advisor_memos_user_kind_created",
            "advisor_memos",
            ["user_id", "kind", "created_at"],
        )
        op.create_index(
            "ix_advisor_memos_user_score_status",
            "advisor_memos",
            ["user_id", "score_status"],
        )

    # Widen the analyst_notes source CHECK to admit 'advisor'. SQLite cannot
    # alter a CHECK in place — batch_alter recreates the table (same as 0068 /
    # 0076). Guarded: skip when the table predates 0074 in a partial test DB,
    # and tolerate the constraint already being absent.
    if _has_table(insp, "analyst_notes"):
        with op.batch_alter_table("analyst_notes") as batch:
            with contextlib.suppress(ValueError, sa.exc.OperationalError):  # already absent
                batch.drop_constraint("ck_analyst_notes_source", type_="check")
            batch.create_check_constraint(
                "ck_analyst_notes_source", f"source IN {_NOTE_SOURCES_WIDENED}"
            )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if _has_table(insp, "advisor_memos"):
        op.drop_index("ix_advisor_memos_user_score_status", table_name="advisor_memos")
        op.drop_index("ix_advisor_memos_user_kind_created", table_name="advisor_memos")
        op.drop_table("advisor_memos")

    # Restore the original source CHECK only if no 'advisor' rows exist —
    # otherwise the recreate would fail row validation; leave the widened
    # constraint in place (downgrade stays safe, never destructive).
    if _has_table(insp, "analyst_notes"):
        advisor_rows = bind.execute(
            sa.text("SELECT COUNT(*) FROM analyst_notes WHERE source = 'advisor'")
        ).scalar()
        if not advisor_rows:
            with op.batch_alter_table("analyst_notes") as batch:
                with contextlib.suppress(ValueError, sa.exc.OperationalError):
                    batch.drop_constraint("ck_analyst_notes_source", type_="check")
                batch.create_check_constraint(
                    "ck_analyst_notes_source",
                    "source IN ('comment', 'chat', 'alert', 'manual')",
                )
