"""ask_answer — per-purpose budget seed for the conversational answer (L3).

The PRIMARY pass-1 narrative answer of the ask path (both the report-drawer
chat and the Ask tab's portfolio scope, streamed through
``chat_session.stream_llm_text``) historically rode the bare ``claude -p`` CLI
default: no purpose, no budget, no ledger. It now resolves through the
``ask_answer`` purpose (``LLM_MODELS`` pin → the model-downgrade / Gemini
loop). Without its own ``llm_budgets`` row it would fall back to '__default__'
— wrong attribution and the wrong cap-exceeded mode.

Seeded ``on_exceed='warn'`` with ``hard_block=0``: this is an INTERACTIVE
surface, so a blown monthly cap must NEVER block the user mid-conversation —
it logs a warning + records the spend, and the answer still streams. (Contrast
``ask_evidence_followup``, 0091, which is ``skip`` mode because the follow-up
loop is optional; pass-1 is the answer itself and cannot be skipped.) $25/month
covers hundreds of Sonnet-tier conversational answers while making a runaway
visible in the budget panel.

Idempotent: INSERT ... ON CONFLICT DO NOTHING. Silently skips when
``llm_budgets`` is absent (fixture DBs that stamp before 0052).

Revision ID: 0104_ask_answer_budget
Revises: 0103_podcast_takeaway_summary_budget
Create Date: 2026-06-14
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0104_ask_answer_budget"
down_revision: str | Sequence[str] | None = "0103_podcast_takeaway_summary_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PURPOSE = "ask_answer"
# Pass-1 fires on every narrative ask turn ≈ $0.02-0.05 each on the Sonnet chat
# tier; $25/month covers hundreds of conversational answers while bounding a
# runaway. Soft (warn) — never blocks the interactive answer.
_MONTHLY_CAP_USD = 25.00


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
        {
            "purpose": _PURPOSE,
            "cap": _MONTHLY_CAP_USD,
            "now": now,
            "notes": "seeded by migration 0104 — L3 ask conversational answer "
            "(warn mode: cap hit logs + records spend, the interactive answer still streams)",
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
