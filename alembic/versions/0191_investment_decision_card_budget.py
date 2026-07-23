"""investment_decision_card llm_budgets seed — P1.1 Investment Decision Card
(PRD ``docs/design/personal_investment_partner_prd.md`` §8.1/§10).

No schema change: the card reuses ``llm_artifacts`` (scope='ticker',
purpose='investment_decision_card') and the EXISTING ``decisions`` table —
``decisions.advice_artifact_id`` (migration 0188) already links a card's
Pass/Watch/Promote disposition back to the artifact, and
``decisions.recommendation_kind`` has no DB-level CHECK constraint (verified
at 0188), so the three new kind values ('pass', 'watch', 'promote') need no
DDL. No view in this repo joins on ``investment_decision_card`` as a purpose
string, so no view-guard is needed here.

``llm_budgets`` seed: $8/month, warn at 80%, ``on_exceed='skip'`` — PRD §10.4:
per-item degrade for a per-ticker batch-build purpose (a card is generated at
the end of every evaluation build; a blown cap should forgo ONE ticker's card
generation, not block the whole build the way 'block' mode does for the
portfolio-wide Incremental Dollar Recommendation). Mirrors 0188's/0132's
idempotent ``ON CONFLICT DO NOTHING`` seed pattern.

Revision ID: 0191_investment_decision_card_budget
Revises: 0189_merge_0188_heads
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0191_investment_decision_card_budget"
down_revision: str | Sequence[str] | None = "0189_merge_0188_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BUDGET_PURPOSE = "investment_decision_card"
_BUDGET_CAP = 8.00


def _seed_budget(bind: sa.Connection) -> None:
    insp = sa.inspect(bind)
    if "llm_budgets" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("llm_budgets")}
    now = datetime.now(UTC).isoformat()
    notes = (
        "seeded by migration 0190 — P1.1 Investment Decision Card; skip mode "
        "(PRD §10.4: a per-ticker batch-build purpose degrades one card at a "
        "time on a blown cap, never blocks the whole evaluation build)"
    )
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
        sa.text(sql), {"purpose": _BUDGET_PURPOSE, "cap": _BUDGET_CAP, "now": now, "notes": notes}
    )


def upgrade() -> None:
    bind = op.get_bind()
    _seed_budget(bind)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "llm_budgets" in set(insp.get_table_names()):
        bind.execute(
            sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"),
            {"purpose": _BUDGET_PURPOSE},
        )
