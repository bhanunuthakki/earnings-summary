"""positioning_coach_turn + positioning_encode — per-purpose budget seeds.

The positioning coach (conversation) and encoder (structured proposal) resolve
through ``LLM_MODELS`` (Sonnet-tier). Without their own ``llm_budgets`` rows
they'd fall back to ``__default__`` — wrong attribution and the wrong
cap-exceeded mode. Seeded ``on_exceed='warn'`` (``hard_block=0``): both are
OWNER-INITIATED surfaces (a blocked coach turn mid-conversation would read as
a broken app, and persistence is already gated by explicit approval), and a
handful of Sonnet turns a week is single-digit dollars/month — the warn log
surfaces any runaway. $10/month each bounds it.

Idempotent (ON CONFLICT DO NOTHING); skips when ``llm_budgets`` is absent.
Mirrors 0138/0139.

Revision ID: 0146_positioning_budgets
Revises: 0145_positioning_intents
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0146_positioning_budgets"
down_revision: str | Sequence[str] | None = "0145_positioning_intents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEEDS: tuple[tuple[str, float, str], ...] = (
    (
        "positioning_coach_turn",
        10.00,
        "seeded by migration 0146 — positioning coach turns; warn mode "
        "(owner-initiated dialogue: a blown cap must not break the conversation)",
    ),
    (
        "positioning_encode",
        10.00,
        "seeded by migration 0146 — positioning encode proposals; warn mode "
        "(owner approval gates persistence; the cap only bounds runaway spend)",
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "llm_budgets" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("llm_budgets")}
    now = datetime.now(UTC).isoformat()
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
    for purpose, cap, notes in _SEEDS:
        bind.execute(sa.text(sql), {"purpose": purpose, "cap": cap, "now": now, "notes": notes})


def downgrade() -> None:
    bind = op.get_bind()
    if "llm_budgets" not in set(sa.inspect(bind).get_table_names()):
        return
    for purpose, _cap, _notes in _SEEDS:
        bind.execute(
            sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"), {"purpose": purpose}
        )
