"""capture_intent — per-purpose budget seed (Ledger intent tap).

The intent classifier (``research.intent.classify_intent``) resolves through
``LLM_MODELS`` (Haiku pin). Without its own ``llm_budgets`` row it would fall back
to ``__default__`` — wrong attribution and the wrong cap-exceeded mode. Seeded
``on_exceed='warn'`` (``hard_block=0``): this is the FIRE-AND-FORGET tap that runs
on EVERY owner musing (no lexical pre-gate anymore), so a blown cap must never
SUPPRESS classification — capture intelligence has to stay live, and a Haiku call
per hand-typed musing is pennies/month, so a soft overspend is harmless while the
warn log surfaces any runaway. $5/month bounds that runaway.

Idempotent (ON CONFLICT DO NOTHING); skips when ``llm_budgets`` is absent. Mirrors
0132.

Revision ID: 0138_capture_intent_budget
Revises: 0137_provenance_freshness
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0138_capture_intent_budget"
down_revision: str | Sequence[str] | None = "0137_provenance_freshness"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURPOSE = "capture_intent"
_CAP = 5.00


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "llm_budgets" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("llm_budgets")}
    now = datetime.now(UTC).isoformat()
    notes = (
        "seeded by migration 0138 — Ledger intent tap (capture_intent); warn mode "
        "(fire-and-forget on every musing: a blown cap must not suppress classification)"
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
    bind.execute(sa.text(sql), {"purpose": _PURPOSE, "cap": _CAP, "now": now, "notes": notes})


def downgrade() -> None:
    bind = op.get_bind()
    if "llm_budgets" not in set(sa.inspect(bind).get_table_names()):
        return
    bind.execute(sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"), {"purpose": _PURPOSE})
