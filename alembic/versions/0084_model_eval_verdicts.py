"""model_eval_verdicts — rolling history of model-downgrade sweep verdicts.

Why this table (see project_model_eval_loop.md + directives/llm_evals_plan.md)
------------------------------------------------------------------------------
PR1 (#429) built the one-shot advisory loop: run ``eval_model_downgrade`` on a
capture JSONL, get a verdict to disk. That's fine for a manual spot-check, but
the loop is only useful if it runs continuously and accumulates a history — so
drift can be detected (a SWITCH_DOWN that becomes KEEP_INCUMBENT after a prompt
revision) and the decision log is auditable.

This table is the accumulation sink. Each weekly sweep driver call (PR2:
``execution/run_model_eval_sweep.py``) INSERTs rows here rather than upserting:
the full history is what's valuable, not just the current state.

Schema design notes
-------------------
* ``verdict`` is one of SWITCH_DOWN / KEEP_INCUMBENT / HOLD / INSUFFICIENT_DATA
  (mirroring ``llm.model_eval`` constants).
* ``judge_agreement`` is 0.0–1.0 (fraction of cases where both judges agreed).
* ``n_cases`` is the number of prompt cases evaluated.
* ``n_parity`` is the number of candidate-wins + ties (the parity numerator).
* ``summary_json`` stores the full ``CandidateVerdict`` fields as a compact JSON
  blob — avoids normalising wins/losses into extra columns while keeping the
  data queryable.
* Index on (purpose, candidate_model, evaluated_at) supports the time-series
  view the dashboard panel needs.

Revision ID: 0084_model_eval_verdicts
Revises: 0083_eval_runs
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0084_model_eval_verdicts"
down_revision: str | Sequence[str] | None = "0083_eval_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "model_eval_verdicts" in existing:
        return

    op.create_table(
        "model_eval_verdicts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # Which LLM purpose was evaluated (key in LLM_MODELS, e.g. "bear_case").
        sa.Column("purpose", sa.String(length=64), nullable=False),
        # The pinned production model for this purpose at eval time.
        sa.Column("incumbent_model", sa.String(length=64), nullable=False),
        # The cheaper candidate that was tested.
        sa.Column("candidate_model", sa.String(length=64), nullable=False),
        # Decision from decide_switch: SWITCH_DOWN / KEEP_INCUMBENT / HOLD /
        # INSUFFICIENT_DATA.
        sa.Column("verdict", sa.String(length=32), nullable=False),
        # Fraction 0.0-1.0: cases where both judges agreed on the winner.
        sa.Column("judge_agreement", sa.Float(), nullable=False),
        # Total prompt cases evaluated in this run.
        sa.Column("n_cases", sa.Integer(), nullable=False),
        # Candidate wins + ties (the parity numerator; n_parity / n_cases = parity_rate).
        sa.Column("n_parity", sa.Integer(), nullable=False),
        # Naive-UTC timestamp when this verdict was recorded (project convention).
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        # Full CandidateVerdict fields as compact JSON (reason, per-judge tallies, etc.).
        sa.Column("summary_json", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_model_eval_verdicts_purpose_candidate_at",
        "model_eval_verdicts",
        ["purpose", "candidate_model", "evaluated_at"],
    )
    op.create_index(
        "ix_model_eval_verdicts_evaluated_at",
        "model_eval_verdicts",
        ["evaluated_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "model_eval_verdicts" not in existing:
        return
    op.drop_index(
        "ix_model_eval_verdicts_evaluated_at",
        table_name="model_eval_verdicts",
    )
    op.drop_index(
        "ix_model_eval_verdicts_purpose_candidate_at",
        table_name="model_eval_verdicts",
    )
    op.drop_table("model_eval_verdicts")
