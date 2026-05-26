"""decisions — audit ledger for LLM recommendations + user actions + outcomes.

Closes the learning loop: the system already emits LLM recommendations via
the `lens:five_min_reread` artifact ("TRIM 20%", "ADD 15%", "HOLD", "SELL").
What was missing was the durable ledger of:

  - which recommendations were made and with what conviction
  - whether the user acted (followed / ignored / partial / reversed)
  - whether the recommendation panned out N months later vs. realized price

That ledger feeds two new lenses:

  mgmt_credibility_score — read pre-existing predictions outcomes (saydo +
                            bear-case grading) for a ticker and emit a
                            management-track-record memo
  llm_calibration        — read 180 days of decisions, compute the curve of
                            stated conviction vs. realized outcome. Used to
                            calibrate the LLM's own conviction language.

One row per recommendation. `source_artifact_id` ties back to the lens
artifact that emitted it (typically lens:five_min_reread, but extensible).
Idempotency lives in the recorder, not the schema — re-running the
extractor on the same artifact is a no-op.

Revision ID: 0046_decisions
Revises: 0044_tracked_companies_processing_tier, 0045_macro_overlay, 0052_llm_budgets
Create Date: 2026-05-25

Note: chains off all three sibling heads from Weeks 2-3 (tier, macro, budget)
so this migration ALSO serves as the merge point. After this lands, `alembic
upgrade head` resolves to a single head again.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0046_decisions"
down_revision: str | Sequence[str] | None = (
    "0044_tracked_companies_processing_tier",
    "0045_macro_overlay",
    "0052_llm_budgets",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if "decisions" in set(sa.inspect(bind).get_table_names()):
        return

    op.create_table(
        "decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("recommendation_kind", sa.String(length=32), nullable=False),
        # 'add' | 'trim' | 'hold' | 'sell' | 'initiate' | 'avoid'
        sa.Column("recommendation_value", sa.Float(), nullable=True),  # size pct
        sa.Column("conviction", sa.String(length=16), nullable=True),  # 'low'|'medium'|'high'
        sa.Column("source_artifact_id", sa.Integer(), nullable=False),  # FK to llm_artifacts
        sa.Column("source_lens", sa.String(length=64), nullable=True),
        sa.Column("rationale_excerpt", sa.Text(), nullable=True),
        sa.Column("made_at", sa.DateTime(), nullable=False),
        sa.Column("user_acted_at", sa.DateTime(), nullable=True),
        sa.Column(
            "user_action_kind", sa.String(length=32), nullable=True
        ),  # 'followed'|'ignored'|'partial'|'reversed'
        sa.Column("user_notes", sa.Text(), nullable=True),
        sa.Column("outcome_at", sa.DateTime(), nullable=True),
        sa.Column(
            "outcome_label", sa.String(length=16), nullable=True
        ),  # 'correct'|'wrong'|'mixed'|'unfalsifiable'|'pending'
        sa.Column("outcome_pct", sa.Float(), nullable=True),  # price move from made_at
        sa.Column("outcome_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("idx_decisions_ticker", "decisions", ["ticker"])
    op.create_index("idx_decisions_made_at", "decisions", ["made_at"])
    op.create_index("idx_decisions_outcome_label", "decisions", ["outcome_label"])
    op.create_index(
        "idx_decisions_source_artifact", "decisions", ["source_artifact_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("idx_decisions_source_artifact", table_name="decisions")
    op.drop_index("idx_decisions_outcome_label", table_name="decisions")
    op.drop_index("idx_decisions_made_at", table_name="decisions")
    op.drop_index("idx_decisions_ticker", table_name="decisions")
    op.drop_table("decisions")
