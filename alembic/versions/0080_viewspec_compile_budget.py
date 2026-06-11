"""viewspec_compile — per-purpose budget seed for the NL query box (P5.2).

The Explore panel's natural-language box compiles a question into a
ViewSpec via a fast model (master build P5.2). Without its own llm_budgets
row the purpose would fall back to '__default__' — shared attribution and
the wrong cap-exceeded mode. The row is seeded ``on_exceed='skip'`` so a
blown cap silently disables the NL box (the panel degrades to the
structured builder, which is the directive's contract) rather than
blocking or overspending; the cap is modest because each compile is one
short Haiku call (fractions of a cent).

Idempotent: INSERT ... ON CONFLICT DO NOTHING; skips silently when
llm_budgets doesn't exist (fixture DBs stamped past 0052).

Revision ID: 0080_viewspec_compile_budget
Revises: 0079_saved_views
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0080_viewspec_compile_budget"
down_revision: str | Sequence[str] | None = "0079_saved_views"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PURPOSE = "viewspec_compile"
# One short Haiku call per query (~3-6KB prompt, ~300B output) ≈ $0.002;
# $5/month covers thousands of compiles while still bounding a runaway.
_MONTHLY_CAP_USD = 5.00


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "llm_budgets" not in existing:
        return
    cols = {c["name"] for c in sa.inspect(bind).get_columns("llm_budgets")}
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
            "notes": "seeded by migration 0080 — P5.2 NL query box (skip mode: "
            "cap hit disables the NL box, builder UI keeps working)",
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "llm_budgets" not in existing:
        return
    bind.execute(
        sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"),
        {"purpose": _PURPOSE},
    )
