"""senior_partner_brief llm_budgets seed — P2.2 Senior Partner Brief (PRD
``docs/design/personal_investment_partner_prd.md`` §9.1/§10).

No schema change: the brief reuses ``llm_artifacts`` (scope='portfolio',
purpose='senior_partner_brief', mirrors the Incremental Dollar
Recommendation's portfolio-scope shape) and the EXISTING ``coach_pings``
table — its ``status`` column carries no DB-level CHECK constraint (verified
at 0131), so the new ``routed_to_brief`` value (P2.2's governor adapter,
``research.governor.BRIEF_ROUTED_CLASSES``) needs no DDL either.

``llm_budgets`` seed: $6/month, warn at 80%, ``on_exceed='block'`` — PRD
§10.4/§10.6: ONE governed call per week (``compose_senior_partner_brief.py``
is idempotent per ISO-week via the artifact's ``cache_inputs``), so a blown
cap is explicit unavailability — the composer's deterministic fallback (a
labeled mechanical digest of artifact pointers, no synthesized confidence)
ships instead of a silent downgrade to a cheaper model. Mirrors 0188's/
0191's idempotent ``ON CONFLICT DO NOTHING`` seed pattern.

Revision ID: 0201_senior_partner_brief_budget
Revises: 0199_risk_snapshot_provenance
Create Date: 2026-07-24

Numbered 0199 rather than 0198: PR #989
(``claude/section-partition-data-richness-5616ec``) independently claimed
``0198_filing_sections`` off the same 0197 parent while this branch was in
flight (checked via ``gh pr diff 989 --name-only`` at commit time per repo
convention — two parallel sessions push to this repo). Both chain off
0197_decision_drafts; the resulting two-head fork is reconciled by a merge
migration once both land, matching this repo's existing pattern (e.g.
0188/0189, 0192/0193).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0201_senior_partner_brief_budget"
down_revision: str | Sequence[str] | None = "0199_risk_snapshot_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BUDGET_PURPOSE = "senior_partner_brief"
_BUDGET_CAP = 6.00


def _seed_budget(bind: sa.Connection) -> None:
    insp = sa.inspect(bind)
    if "llm_budgets" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("llm_budgets")}
    now = datetime.now(UTC).isoformat()
    notes = (
        "seeded by migration 0199 — P2.2 Senior Partner Brief; block mode "
        "(PRD §10.6: one governed call/week — a blown cap is explicit "
        "unavailability via the labeled deterministic fallback, never a "
        "silent cheaper-model swap)"
    )
    if "on_exceed" in cols:
        sql = """
            INSERT INTO llm_budgets
                (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                 on_exceed, created_at, updated_at, notes)
            VALUES (:purpose, :cap, 0.80, 0, 'block', :now, :now, :notes)
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
