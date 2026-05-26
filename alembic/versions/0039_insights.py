"""insights — free-form analytical content driven by lenses.

A lens is a prompt + cadence + input contract that produces analytical
content the system can't pre-schema. Adding a new lens = drop a markdown
file under prompts/lenses/<name>.md; no model edits, no renderer edits.

The table is intentionally generic: lens name + scope + ticker + content +
provenance. Renderers render markdown + a provenance footer.

Examples of lenses (starter set in prompts/lenses/):
  underweighted_facts       — "5 things consensus is missing this quarter"
  consensus_blind_spots     — "where sell-side is wrong"
  quarter_over_quarter_drift— bear-case diff vs prior quarter
  risk_factor_delta         — 10-K risk additions/removals/rewords
  mgmt_credibility_score    — weighted SayDo outcome over 8Q
  cross_ticker_theme        — portfolio-scope thematic synthesis

Revision ID: 0039_insights
Revises: 0038_predictions
Create Date: 2026-05-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0039_insights"
down_revision: str | Sequence[str] | None = "0038_predictions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if "insights" in set(sa.inspect(bind).get_table_names()):
        return

    op.create_table(
        "insights",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(length=32), nullable=False, server_default="ticker"),
        sa.Column("ticker", sa.String(length=16), nullable=True),  # null when scope=portfolio
        sa.Column("lens", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False, server_default="v1"),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=True),  # structured sidecar when lens emits both
        # Provenance: JSON arrays of upstream artifact_ids + (extracted) fact spans
        sa.Column("input_artifact_ids", sa.Text(), nullable=True),
        sa.Column("input_facts_window", sa.String(length=128), nullable=True),
        sa.Column(
            "superseded_by_id",
            sa.Integer(),
            sa.ForeignKey("insights.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("dirty", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("dirty_reason", sa.String(length=128), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("llm_call_id", sa.Integer(), nullable=True),
    )
    op.create_index("idx_insights_lens", "insights", ["lens"])
    op.create_index("idx_insights_ticker", "insights", ["ticker", "lens"])
    op.create_index("idx_insights_scope", "insights", ["scope", "lens"])
    op.create_index("idx_insights_generated_at", "insights", ["generated_at"])
    op.execute(
        "CREATE INDEX idx_insights_dirty "
        "ON insights(ticker, lens) WHERE dirty = 1"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_insights_dirty")
    op.drop_index("idx_insights_generated_at", table_name="insights")
    op.drop_index("idx_insights_scope", table_name="insights")
    op.drop_index("idx_insights_ticker", table_name="insights")
    op.drop_index("idx_insights_lens", table_name="insights")
    op.drop_table("insights")
