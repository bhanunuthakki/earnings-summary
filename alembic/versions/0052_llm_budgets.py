"""llm_budgets + llm_budget_alerts — per-purpose monthly LLM spend caps.

The Phase 0 ledger (migration 0034, `llm_calls`) gave us cost visibility but
no guardrails: any new lens or extractor added to the pipeline grows spend
organically. With 11 lenses × 11 holdings × 4 weeks = thousands of calls/
month, a single buggy purpose can rack up unexpected cost before the spend
report surfaces it.

This migration adds two tables:

  llm_budgets         — one row per purpose with monthly_cap_usd, warn
                        threshold, hard_block flag. Seeded with reasonable
                        starting caps for every known purpose; unknown
                        purposes fall back to the '__default__' row.
  llm_budget_alerts   — idempotent record of (purpose, month, threshold)
                        crossings so the warning log doesn't spam every call
                        once burn > 80%.

Pre-call enforcement lives in `src/llm_budget.py` (best-effort: if either
table is missing or unreadable, the LLM call proceeds — telemetry must never
break production). The dashboard surfaces remaining headroom per purpose.

Revision ID: 0052_llm_budgets
Revises: 0043_self_update_triggers
Create Date: 2026-05-25

Note on the numeric gap: slots 0044-0051 are reserved for other in-flight
scalability work (doc tables, tiered processing). Alembic's chain is by
revision id, not numeric order, so the gap is harmless — `down_revision`
points at 0043 which is the current head on this branch.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0052_llm_budgets"
down_revision: str | Sequence[str] | None = "0043_self_update_triggers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (purpose, monthly_cap_usd) tuples. Seeded into llm_budgets on upgrade.
# '__default__' is the catch-all for purposes not explicitly listed — the
# budget enforcer falls back to this row when the requested purpose has no
# specific cap. Tune individual caps via the manage_llm_budget CLI; this
# seed only fills empty rows (INSERT ... ON CONFLICT DO NOTHING).
_DEFAULT_BUDGETS: list[tuple[str, float]] = [
    ("bear_case", 50.00),
    ("recent_developments", 80.00),  # web search is expensive
    ("lens:cross_portfolio_synthesis", 20.00),
    ("lens:five_min_reread", 30.00),
    ("lens:thesis_drift_qoq", 25.00),
    ("lens:bull_case", 20.00),
    ("lens:reverse_dcf", 15.00),
    ("lens:catalyst_calendar", 15.00),
    ("lens:underweighted_facts", 20.00),
    ("lens:filing_diff_narrative", 10.00),
    ("lens:footnote_anomaly", 10.00),
    ("transcript_summary", 80.00),
    ("pairwise_analysis", 30.00),
    ("company_description", 20.00),
    ("valuation_basis", 15.00),
    ("exec_comp_alignment", 15.00),
    ("risk_factor_classify", 5.00),
    ("footnote_extraction", 20.00),
    ("exec_comp_extraction", 30.00),
    ("canonicalize_segments", 5.00),
    ("bear_case_grading", 15.00),
    ("__default__", 25.00),
]


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "llm_budgets" not in existing:
        op.create_table(
            "llm_budgets",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            # Purpose key from llm_client.LLM_MODELS (e.g. "bear_case",
            # "lens:five_min_reread"). The special key "__default__" is the
            # fallback row for unknown purposes — always present after seed.
            sa.Column("purpose", sa.String(length=64), nullable=False),
            # Monthly USD cap. Numeric(10,2) gives 8 digits before the
            # decimal — plenty of headroom for a $50/mo bear-case budget
            # without falling into float precision quirks.
            sa.Column("monthly_cap_usd", sa.Numeric(10, 2), nullable=False),
            # Soft warning threshold as a fraction (0.80 = warn at 80% burn).
            # The budget enforcer logs a warning + records an alert row when
            # this is crossed but allows the call to proceed.
            sa.Column(
                "warn_threshold_pct", sa.Float(), nullable=False, server_default="0.80"
            ),
            # When True, calls above monthly_cap_usd raise LLMBudgetExceeded.
            # When False, calls above cap log a warning and proceed (soft cap).
            # Defaulted to 0 (soft) so seeding doesn't immediately block any
            # existing pipeline; operators flip individual purposes to hard
            # via the manage CLI once they're confident the cap is right.
            sa.Column("hard_block", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.UniqueConstraint("purpose", name="uq_llm_budgets_purpose"),
        )
        op.create_index(
            "idx_llm_budgets_purpose", "llm_budgets", ["purpose"], unique=True
        )

    if "llm_budget_alerts" not in existing:
        op.create_table(
            "llm_budget_alerts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("purpose", sa.String(length=64), nullable=False),
            # 'YYYY-MM' — the calendar month the alert fired for. Combined
            # with purpose + threshold_pct, this is the idempotency key:
            # once an 80% alert fires for ('bear_case', '2026-05'), every
            # subsequent call that crosses 80% during May 2026 is a no-op.
            sa.Column("month", sa.String(length=7), nullable=False),
            sa.Column("threshold_pct", sa.Float(), nullable=False),
            sa.Column("alerted_at", sa.DateTime(), nullable=False),
            # Spend at the moment of the alert — useful for forensics if the
            # cap is revisited mid-month and we want to see when burn hit X.
            sa.Column("spend_at_alert_usd", sa.Numeric(10, 4), nullable=False),
            sa.UniqueConstraint(
                "purpose", "month", "threshold_pct", name="uq_alerts"
            ),
        )
        op.create_index("idx_llm_budget_alerts_month", "llm_budget_alerts", ["month"])
        op.create_index(
            "idx_llm_budget_alerts_purpose", "llm_budget_alerts", ["purpose"]
        )

    # Seed defaults via INSERT ... ON CONFLICT DO NOTHING (SQLite >= 3.24).
    # Idempotent: re-running upgrade on a populated DB leaves operator-edited
    # caps intact. Using sa.text + parameter binding for safety.
    now = datetime.now(UTC).isoformat()
    insert_sql = sa.text(
        """
        INSERT INTO llm_budgets
            (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
             created_at, updated_at, notes)
        VALUES (:purpose, :cap, 0.80, 0, :now, :now, :notes)
        ON CONFLICT(purpose) DO NOTHING
        """
    )
    for purpose, cap in _DEFAULT_BUDGETS:
        bind.execute(
            insert_sql,
            {
                "purpose": purpose,
                "cap": cap,
                "now": now,
                "notes": "seeded by migration 0052",
            },
        )


def downgrade() -> None:
    op.drop_index("idx_llm_budget_alerts_purpose", table_name="llm_budget_alerts")
    op.drop_index("idx_llm_budget_alerts_month", table_name="llm_budget_alerts")
    op.drop_table("llm_budget_alerts")
    op.drop_index("idx_llm_budgets_purpose", table_name="llm_budgets")
    op.drop_table("llm_budgets")
