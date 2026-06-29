"""wondering_detect — per-purpose budget seed (Ledger Phase-1 W1-2).

The research-loop gate resolves through ``LLM_MODELS`` (FAST_CLASSIFIER pin).
Without its own ``llm_budgets`` row it falls back to ``__default__`` — wrong
attribution + the wrong cap-exceeded mode. Seeded ``on_exceed='skip'``
(``hard_block=0``): it is an AUTONOMOUS, fire-and-forget tap behind a regex
pre-gate, so a blown cap should SKIP (no detection that day) rather than block a
capture. Tiny per-call (Flash tier, one short classify), so a low cap bounds a
runaway cheaply.

Idempotent (ON CONFLICT DO NOTHING); skips when ``llm_budgets`` is absent.

Revision ID: 0123_wondering_detect_budget
Revises: 0122_research_hot_flags
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0123_wondering_detect_budget"
down_revision: str | Sequence[str] | None = "0122_research_hot_flags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURPOSE = "wondering_detect"
_MONTHLY_CAP_USD = 5.00


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
            VALUES (:purpose, :cap, 0.80, 0, 'skip', :now, :now, :notes)
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
        {
            "purpose": _PURPOSE,
            "cap": _MONTHLY_CAP_USD,
            "now": now,
            "notes": "seeded by 0123 — Ledger Phase-1 research-loop gate (skip mode: "
            "autonomous fire-and-forget tap behind a regex pre-gate)",
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "llm_budgets" not in set(sa.inspect(bind).get_table_names()):
        return
    bind.execute(sa.text("DELETE FROM llm_budgets WHERE purpose = :p"), {"p": _PURPOSE})
