"""ask_claim_grounding — per-purpose budget seed for the citation audit (S8).

Grounded narrative ask turns now end with one short fast-model call that
maps the answer's claims to the evidence that backs them
(src/ask/claims.py). Without its own llm_budgets row the purpose would fall
back to '__default__' — shared attribution and the wrong cap-exceeded mode.
The row is seeded ``on_exceed='skip'`` so a blown cap silently disables the
per-claim map (the citations event degrades to the answer-level shape — the
module's documented fail-closed contract) rather than blocking or
overspending; the cap is modest because each call is one short Haiku audit
over a few KB (fractions of a cent).

Idempotent: INSERT ... ON CONFLICT DO NOTHING; skips silently when
llm_budgets doesn't exist (fixture DBs stamped past 0052).

Revision ID: 0090_ask_claim_grounding_budget
Revises: 0089_ask_pack_router_budget
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0090_ask_claim_grounding_budget"
down_revision: str | Sequence[str] | None = "0089_ask_pack_router_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PURPOSE = "ask_claim_grounding"
# One short Haiku call per grounded narrative ask turn (~4KB prompt, ~300B
# output) ≈ $0.002; $5/month covers thousands of turns while bounding a
# runaway.
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
            "notes": "seeded by migration 0090 — S8 ask claim-grounding audit (skip "
            "mode: cap hit degrades citations to answer-level, turns keep answering)",
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
