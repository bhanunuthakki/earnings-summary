"""pre_earnings_brief plumbing — opt-in flag + per-purpose budget seed.

Owner ruling 2026-07-31 (relaxing D2 for this one artifact): the PRE-earnings
brief may be pre-generated. Scope: every portfolio name, plus the evaluation
names the owner marks. Two pieces:

1. ``ticker_settings.auto_pre_earnings_brief`` — the owner's sticky per-ticker
   opt-in for evaluation names (portfolio is always in scope as a READ-side
   rule in ``earnings_brief.eligible_tickers``; no stored row needed). Lives on
   ``ticker_settings`` (0067) beside ``bypass_budget`` — same table, same
   endpoint, same toggle pattern. Deliberately NOT ``research_hot_flags``:
   that flag raises research spend tiers (budget semantics) and decays on a
   72h TTL; this mark is a durable reading-lane preference.

2. ``llm_budgets`` seed for purpose ``pre_earnings_brief`` (one Sonnet call
   per ticker per earnings cycle, at most twice per cycle on a T-1 input
   refresh). ~11 portfolio + a few marked names × ~4 cycles/yr ≈ single-digit
   calls/month; $5/month bounds a runaway. ``on_exceed='skip'`` — a blown cap
   skips generation (the prep peek falls back to its deterministic assembly),
   never blocks the morning pipeline.

Both steps are guarded + idempotent; ``ticker_settings`` is a plain table
(no anonymous CHECKs), so a bare ADD COLUMN is safe.

Revision ID: 0260_pre_earnings_brief_plumbing
Revises: 0259_source_definition_identity
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0260_pre_earnings_brief_plumbing"
down_revision: str | Sequence[str] | None = "0259_source_definition_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ticker_settings"
_COLUMN = "auto_pre_earnings_brief"

_PURPOSE = "pre_earnings_brief"
# One Sonnet call (~3-6k token prompt, ~1k output) ≈ $0.02-0.05; a book of
# ~15 in-scope names reporting quarterly is well under $1/month — $5 bounds
# a runaway regeneration loop.
_MONTHLY_CAP_USD = 5.00


def _has_column(insp: sa.Inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())

    if _TABLE in existing and not _has_column(insp, _TABLE, _COLUMN):
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} INTEGER NOT NULL DEFAULT 0")

    if "llm_budgets" in existing:
        cols = {c["name"] for c in insp.get_columns("llm_budgets")}
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
                "notes": "seeded by migration 0260 — pre-earnings brief generator (skip "
                "mode: cap hit skips generation; the prep peek falls back to its "
                "deterministic assembly)",
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())
    if _TABLE in existing and _has_column(insp, _TABLE, _COLUMN):
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {_COLUMN}")
    if "llm_budgets" in existing:
        bind.execute(
            sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"),
            {"purpose": _PURPOSE},
        )
