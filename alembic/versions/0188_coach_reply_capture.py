"""coach_pings gains telegram_message_id + two warn-mode LLM budgets (B3).

``execution/run_coach_pings.py``'s ``_send`` was send-only: it called
``telegram.send_message`` and discarded the returned ``message_id``. When the
owner replied in free text to a pushed coach ping (e.g. the 33% high-conviction
calibration finding), the poller had no way to route that reply back to the
finding — only the ``cp:dismiss``/``cp:review`` BUTTONS worked, and the reply
itself (the most valuable signal a coach ping can get back) fell through to
generic capture with the linkage lost.

``coach_pings`` has no JSON context column (unlike ``analyst_notes``'
``context_json``, which is how a note's card remembers its Telegram message
id — see ``poller._stash_card_mid``), so this is a real column, mirroring that
idiom at the DDL level rather than reinventing a JSON blob for one field.

Also seeds two warn-mode ``llm_budgets`` rows for the two new Haiku legs this
PR wires up on the capture poller's continuous LLM surface (mirrors 0138's
``capture_intent`` pattern exactly — column-shape guard, ``ON CONFLICT DO
NOTHING``, ``hard_block=0``/``on_exceed='warn'`` so a blown cap degrades to a
logged warning, never suppresses classification):

- ``capture_triage`` ($5.00/mo) — Haiku per landed musing/observation, the
  triage classifier that replaces the old regex answer-gate
  (``src/capture/triage.py``, same PR; both legs ride the same
  window-registry update, directives/llm_quota_scheduling.md).
- ``coach_reply_intent`` ($2.00/mo) — Haiku per free-text reply to a coach
  ping (this PR, ``src/capture/coach_reply.py``). Lower cap than
  ``capture_triage``: coach pings are capped at DAILY_CAP=1/WEEKLY_CAP=3
  (``research/governor.py``), so replies to them are a much smaller volume.

Revision ID: 0188_coach_reply_capture
Revises: 0187_wealth_context_snapshot_history
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0188_coach_reply_capture"
down_revision: str | Sequence[str] | None = "0187_wealth_context_snapshot_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BUDGETS: tuple[tuple[str, float], ...] = (
    ("capture_triage", 5.00),
    ("coach_reply_intent", 2.00),
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    names = set(insp.get_table_names())

    if "coach_pings" in names:
        cols = {c["name"] for c in insp.get_columns("coach_pings")}
        if "telegram_message_id" not in cols:
            op.add_column(
                "coach_pings", sa.Column("telegram_message_id", sa.Integer(), nullable=True)
            )

    if "llm_budgets" in names:
        cols = {c["name"] for c in insp.get_columns("llm_budgets")}
        now = datetime.now(UTC).isoformat()
        notes = (
            "seeded by migration 0188 (B3) — warn mode (a blown cap must not suppress "
            "classification; see this migration's docstring for the two legs)"
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
        for purpose, cap in _BUDGETS:
            bind.execute(sa.text(sql), {"purpose": purpose, "cap": cap, "now": now, "notes": notes})


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    names = set(insp.get_table_names())

    if "coach_pings" in names:
        cols = {c["name"] for c in insp.get_columns("coach_pings")}
        if "telegram_message_id" in cols:
            # No FTS triggers on coach_pings (unlike analyst_notes — see
            # reference_fts_trigger_batch_alter.md), but batch_alter's
            # temp-table + rename dance DOES break while v_decision_journal
            # (0179) still references coach_pings — SQLite errors with
            # "error in view v_decision_journal: no such table". Save the
            # view's own SQL, drop it for the rebuild, restore it verbatim.
            view_row = bind.execute(
                sa.text(
                    "SELECT sql FROM sqlite_master WHERE type='view' AND name='v_decision_journal'"
                )
            ).fetchone()
            if view_row is not None:
                op.execute("DROP VIEW v_decision_journal")
            with op.batch_alter_table("coach_pings") as batch:
                batch.drop_column("telegram_message_id")
            if view_row is not None:
                op.execute(str(view_row[0]))

    if "llm_budgets" in names:
        for purpose, _cap in _BUDGETS:
            bind.execute(
                sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"), {"purpose": purpose}
            )
