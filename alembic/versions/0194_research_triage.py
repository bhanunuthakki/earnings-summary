"""research_triage warn-mode budget row (B7).

The routing triage behind a positive wondering verdict (``src/research/triage.py``,
called from ``research.proposals.detect_and_create_task`` on every ``wondering``):
one Haiku call routing ``answer_now`` / ``belief_candidate`` / ``research_task``.
No new tables or columns are needed for this feature — the task-level state it
produces (stated cost, session prompt, packeted-at, unanswered-weeks) lands on
the ALREADY-EXISTING ``research_tasks.cost_usd`` / ``research_tasks.run_id``
columns (dead since migration 0120; repurposed, not migrated — see
``research.proposals`` module docstring), matching this program's B-series
convention of landing state on existing substrate rather than new schema (e.g.
0192's Tenet-accountability budget-row-only pattern, which this migration
mirrors almost verbatim).

Seeds one warn-mode ``llm_budgets`` row for the new ``research_triage`` purpose
(cap $3.00/mo — this classifier fires on every 'wondering' verdict, a smaller
and cheaper population than 0192's per-Tenet weekly sweep, so a smaller cap):
``sa.inspect`` column-shape guard, ``ON CONFLICT(purpose) DO NOTHING``,
``hard_block=0``/``on_exceed='warn'`` so a blown cap degrades to a logged
warning rather than blocking capture — ``research.triage.classify_triage``
already fails OPEN to ``research_task`` (today's pre-B7 behavior) on any call
failure, so a budget-driven degrade is a second, independent signal, not a gate
that could make a wondering silently vanish (the exact bug B7 fixes).

Revision ID: 0194_research_triage
Revises: 0193_exit_postmortem
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0194_research_triage"
down_revision: str | Sequence[str] | None = "0193_exit_postmortem"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BUDGET_PURPOSE = "research_triage"
_BUDGET_CAP_USD = 3.00


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    names = set(insp.get_table_names())

    if "llm_budgets" in names:
        cols = {c["name"] for c in insp.get_columns("llm_budgets")}
        now = datetime.now(UTC).isoformat()
        notes = (
            "seeded by migration 0194 (B7) — warn mode (a blown cap must not make a "
            "wondering silently vanish; classify_triage already fails open to "
            "research_task on any call failure, see this migration's docstring)"
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

    if "llm_budgets" in names:
        bind.execute(
            sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"),
            {"purpose": _BUDGET_PURPOSE},
        )
