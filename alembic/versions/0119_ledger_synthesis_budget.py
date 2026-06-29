"""theme_synthesis + theme_seed_cluster — per-purpose budget seeds (Ledger Wave B).

The two governed synthesis purposes resolve through ``LLM_MODELS`` (Sonnet pins).
Without their own ``llm_budgets`` rows they would fall back to ``__default__`` —
wrong attribution and the wrong cap-exceeded mode. Seeded ``on_exceed='skip'``
(``hard_block=0``): synthesis is a BATCH, OPTIONAL pass on the morning pipeline,
so a blown monthly cap must SKIP the run (the prior cached insight stands), never
block. $10/month each bounds a runaway while covering incremental per-scope
consolidation (only scopes with new musings call out).

Idempotent (ON CONFLICT DO NOTHING); skips when ``llm_budgets`` is absent.

Revision ID: 0119_ledger_synthesis_budget
Revises: 0118_ledger_insights
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0119_ledger_synthesis_budget"
down_revision: str | Sequence[str] | None = "0118_ledger_insights"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURPOSES: dict[str, float] = {"theme_synthesis": 10.00, "theme_seed_cluster": 10.00}


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
    for purpose, cap in _PURPOSES.items():
        bind.execute(
            sa.text(sql),
            {
                "purpose": purpose,
                "cap": cap,
                "now": now,
                "notes": f"seeded by migration 0119 — Ledger Wave B synthesis ({purpose}); "
                "skip mode (batch/optional: cap hit skips the run, prior insight stands)",
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "llm_budgets" not in set(sa.inspect(bind).get_table_names()):
        return
    for purpose in _PURPOSES:
        bind.execute(
            sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"), {"purpose": purpose}
        )
