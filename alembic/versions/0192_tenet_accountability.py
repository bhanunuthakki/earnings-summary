"""tenet_accountability warn-mode budget row (B5).

Weekly per-Tenet accountability pass (``src/synthesis/tenet_accountability.py``,
``execution/run_tenet_accountability.py``, Sat 09:00 PT cron): for every
``current`` Worldview Tenet, one Sonnet call auditing it against the owner's
own decisions made since the Tenet's ``as_of``. Results are persisted onto
the Tenet's OWN ``insight_notes.meta_json`` row (key ``"accountability"``) —
NO new tables or columns are needed for this feature (a B-workstream rule:
this program's synthesis passes land their state on the existing
``insight_notes``/``decisions`` substrate, not new schema).

Seeds one warn-mode ``llm_budgets`` row for the new ``tenet_accountability``
purpose (cap $10.00/mo — mirrors 0190's ``session_distill`` pattern exactly:
``sa.inspect`` column-shape guard, ``ON CONFLICT(purpose) DO NOTHING``,
``hard_block=0``/``on_exceed='warn'`` so a blown cap degrades to a logged
warning rather than silently skipping the whole weekly sweep — the per-tenet
transient/hard-stop degrade in ``run_accountability`` already handles
individual failures; a soft budget warning is a second, independent signal,
not a gate).

Revision ID: 0192_tenet_accountability
Revises: 0191_investment_decision_card_budget
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0192_tenet_accountability"
down_revision: str | Sequence[str] | None = "0191_investment_decision_card_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BUDGET_PURPOSE = "tenet_accountability"
_BUDGET_CAP_USD = 10.00


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    names = set(insp.get_table_names())

    if "llm_budgets" in names:
        cols = {c["name"] for c in insp.get_columns("llm_budgets")}
        now = datetime.now(UTC).isoformat()
        notes = (
            "seeded by migration 0192 (B5) — warn mode (a blown cap must not block the "
            "weekly accountability sweep; see this migration's docstring)"
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
