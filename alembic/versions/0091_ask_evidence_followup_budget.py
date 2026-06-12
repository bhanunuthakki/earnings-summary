"""ask_evidence_followup — per-purpose budget seed for the S7 evidence loop.

When a narrative ask turn's first pass replies with a structured evidence
request, the engine retrieves the requested items and makes up to two real
``call_llm`` follow-up calls (src/ask/followup.py). Without its own
llm_budgets row the purpose would fall back to '__default__' — shared
attribution and the wrong cap-exceeded mode. The row is seeded
``on_exceed='skip'`` so a blown cap disables the loop pre-call (turns fall
back to one-shot retrieval — the module's documented fail-closed contract)
rather than blocking or overspending. The cap is higher than the router's
because each round is a Sonnet-tier narrative answer, not a Haiku selection.

Idempotent: INSERT ... ON CONFLICT DO NOTHING; skips silently when
llm_budgets doesn't exist (fixture DBs stamped past 0052).

Revision ID: 0091_ask_evidence_followup_budget
Revises: 0090_ask_claim_grounding_budget
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0091_ask_evidence_followup_budget"
down_revision: str | Sequence[str] | None = "0090_ask_claim_grounding_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PURPOSE = "ask_evidence_followup"
# Each loop round is one Sonnet call over an interactive-sized prompt
# (~10-20KB in, a chat answer out) ≈ $0.02-0.05; loops fire only on the
# subset of narrative turns where pass 1 requests more evidence. $10/month
# covers hundreds of looped turns while bounding a runaway.
_MONTHLY_CAP_USD = 10.00


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
            "notes": "seeded by migration 0091 — S7 ask evidence loop (skip mode: "
            "cap hit disables the follow-up loop, turns fall back to one-shot retrieval)",
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
