"""prompt_calibration_scores — durable record of LLM output quality per prompt version.

Why this exists
---------------
The audit memo's P2-16 finding: the codebase has decision_audit
(grade_decisions.py) + bear_case_grader scoring the LLM outputs after the
fact, but nothing aggregates those scores by `prompt_version` so the
analyst can answer "is the v3 bear_case prompt actually better than v2?"
without spelunking the per-call ledger.

This table is the calibration aggregation point: one row per
(purpose, prompt_version, ticker, scored_at, score, reason). The graders
write a row whenever they produce a quality score; a dashboard can group
by (purpose, prompt_version) to surface the trend.

Schema
------
  id          INTEGER PRIMARY KEY
  purpose     TEXT NOT NULL    — e.g. 'bear_case', 'pairwise_analysis'
  prompt_version TEXT NOT NULL — matches llm_artifacts.prompt_version
  ticker      TEXT             — optional; some scores are portfolio-scope
  score       REAL NOT NULL    — analyst-decided; convention is 0.0..1.0
  reason      TEXT             — short free-text label for the bucket
  scored_at   TIMESTAMP        — UTC; defaults to CURRENT_TIMESTAMP
  scored_by   TEXT             — 'auto:bear_case_grader' / 'manual' / ...
  artifact_id INTEGER          — optional FK to llm_artifacts.id (provenance)

Index on (purpose, prompt_version) so the dashboard groupby is fast.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0058_prompt_calibration_scores"
down_revision: str | Sequence[str] | None = "0057_drop_segment_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "prompt_calibration_scores" in inspector.get_table_names():
        return  # idempotent

    op.create_table(
        "prompt_calibration_scores",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=True),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column(
            "scored_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("scored_by", sa.String(length=64), nullable=True),
        sa.Column(
            "artifact_id",
            sa.Integer(),
            sa.ForeignKey("llm_artifacts.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_prompt_calibration_purpose_version",
        "prompt_calibration_scores",
        ["purpose", "prompt_version"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "prompt_calibration_scores" not in inspector.get_table_names():
        return
    op.drop_index(
        "ix_prompt_calibration_purpose_version",
        table_name="prompt_calibration_scores",
    )
    op.drop_table("prompt_calibration_scores")
