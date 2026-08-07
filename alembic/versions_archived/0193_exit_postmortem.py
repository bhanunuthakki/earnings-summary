"""exit_postmortem_draft warn-mode budget row (B6).

LLM-drafted exit post-mortems (``src/synthesis/exit_postmortem.py``,
``execution/run_session_distill.py``'s second sweep leg, daily 18:00 cron):
for every CLOSED ``position_entries`` row whose ``exit_reason`` /
``lessons`` / ``outcome_vs_thesis`` are ALL NULL (pending is DERIVED — no
stamp column), one Sonnet call drafts the three fields from the ticker's
decision trail (``v_decision_journal``) + realized alpha + the deterministic
breach-history hint (``position_lifecycle.infer_outcome_vs_thesis``), written
via ``update_exit_fields`` write-once. NO new tables or columns are needed
(same B-workstream rule 0192 documents: this program's synthesis passes land
on the existing ``position_entries``/``analyst_notes`` substrate — the
``llm_draft`` provenance marker is a linked ``analyst_notes`` row via the
0093 ``position_entry_id`` column, not a new column on ``position_entries``).

Seeds one warn-mode ``llm_budgets`` row for the new ``exit_postmortem_draft``
purpose (cap $5.00/mo — a handful of calls/night at most, capped further by
the sweep's own per-run pacing; mirrors 0190/0192's pattern: ``sa.inspect``
column-shape guard, ``ON CONFLICT(purpose) DO NOTHING``, ``hard_block=0``/
``on_exceed='warn'`` so a blown cap degrades to a logged warning rather than
blocking the whole nightly sweep — the per-entry transient/hard-stop degrade
in ``run_postmortem_drafts`` already handles individual failures; a soft
budget warning is a second, independent signal, not a gate).

Revision ID: 0193_exit_postmortem
Revises: 0192_tenet_accountability
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0193_exit_postmortem"
down_revision: str | Sequence[str] | None = "0192_tenet_accountability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BUDGET_PURPOSE = "exit_postmortem_draft"
_BUDGET_CAP_USD = 5.00


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    names = set(insp.get_table_names())

    if "llm_budgets" in names:
        cols = {c["name"] for c in insp.get_columns("llm_budgets")}
        now = datetime.now(UTC).isoformat()
        notes = (
            "seeded by migration 0193 (B6) — warn mode (a blown cap must not block the "
            "18:00 session-distill sweep's post-mortem leg; see this migration's docstring)"
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
