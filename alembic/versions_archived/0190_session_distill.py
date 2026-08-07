"""session_distill substrate (B4 keystone) — distilled_at markers + budget row.

The keystone of the LLM-native memory rebuild (2026-07-19 program overhaul):
conversations feed ONE distillation tap (``src/synthesis/session_distill.py``)
that turns idle Ask threads and landed Claude-session transcripts into typed
candidates — musings, resolved questions, tenet/stance revisions — which
AUTO-ADOPT with a one-tap undo (owner ruling: distilled belief revisions go
live immediately, announced with a Revert button; ratify-first walks only
apply where #939 law requires it).

This migration adds the two ``distilled_at`` markers the sweep's candidate
query and completion step need, plus one warn-mode ``llm_budgets`` row for the
new ``session_distill`` purpose:

- ``ask_sessions.distilled_at`` (TEXT, nullable) — set once a thin in-app Ask
  thread has been swept; ``NULL`` is the "still a candidate" state.
- ``raw_capture_sessions.distilled_at`` (TEXT, nullable) — same marker for a
  landed Claude-session transcript (``channel='claude_session'``); the sweep
  also blanks the transcript on completion (mirrors ``mark_landed``'s privacy
  floor — raw words are transient once the durable artifact exists).
- ``llm_budgets`` row for ``session_distill`` (cap $15.00/mo, warn mode) —
  mirrors 0188's ``coach_reply_capture`` pattern: ``sa.inspect`` column-shape
  guard, ``ON CONFLICT(purpose) DO NOTHING``, ``hard_block=0``/
  ``on_exceed='warn'`` so a blown cap degrades to a logged warning rather than
  suppressing distillation outright (a transient/budget failure already
  degrades per-item via the sweep's own retry-next-run contract).

Revision ID: 0190_session_distill
Revises: 0189_merge_0188_heads
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0190_session_distill"
down_revision: str | Sequence[str] | None = "0189_merge_0188_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BUDGET_PURPOSE = "session_distill"
_BUDGET_CAP_USD = 15.00


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    names = set(insp.get_table_names())

    if "ask_sessions" in names:
        cols = {c["name"] for c in insp.get_columns("ask_sessions")}
        if "distilled_at" not in cols:
            op.add_column("ask_sessions", sa.Column("distilled_at", sa.Text(), nullable=True))

    if "raw_capture_sessions" in names:
        cols = {c["name"] for c in insp.get_columns("raw_capture_sessions")}
        if "distilled_at" not in cols:
            op.add_column(
                "raw_capture_sessions", sa.Column("distilled_at", sa.Text(), nullable=True)
            )

    if "llm_budgets" in names:
        cols = {c["name"] for c in insp.get_columns("llm_budgets")}
        now = datetime.now(UTC).isoformat()
        notes = (
            "seeded by migration 0190 (B4) — warn mode (a blown cap must not block "
            "distillation; see this migration's docstring)"
        )
        if "on_exceed" in cols:
            sql = """
                INSERT INTO llm_budgets
                    (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                     on_exceed, created_at, updated_at, notes)
                VALUES (:purpose, :cap, 0.80, 0, 'warn', :now, :now, :notes)
                ON CONFLICT(purpose) DO NOTHING
                """
        else:  # pre-0066 shape (hand-built fixture DBs)
            sql = """
                INSERT INTO llm_budgets
                    (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                     created_at, updated_at, notes)
                VALUES (:purpose, :cap, 0.80, 0, :now, :now, :notes)
                ON CONFLICT(purpose) DO NOTHING
                """
        bind.execute(
            sa.text(sql),
            {"purpose": _BUDGET_PURPOSE, "cap": _BUDGET_CAP_USD, "now": now, "notes": notes},
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    names = set(insp.get_table_names())

    # No view in this codebase references ask_sessions or raw_capture_sessions
    # (verified via a repo-wide CREATE VIEW grep before writing this migration),
    # so the plain batch_alter drop is safe — unlike 0188's coach_pings downgrade,
    # which had to save/drop/restore v_decision_journal around its batch_alter.
    if "ask_sessions" in names:
        cols = {c["name"] for c in insp.get_columns("ask_sessions")}
        if "distilled_at" in cols:
            with op.batch_alter_table("ask_sessions") as batch:
                batch.drop_column("distilled_at")

    if "raw_capture_sessions" in names:
        cols = {c["name"] for c in insp.get_columns("raw_capture_sessions")}
        if "distilled_at" in cols:
            with op.batch_alter_table("raw_capture_sessions") as batch:
                batch.drop_column("distilled_at")

    if "llm_budgets" in names:
        bind.execute(
            sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"),
            {"purpose": _BUDGET_PURPOSE},
        )
