"""model_eval_verdicts + model_pin_overrides — auto-switch infra for the model-eval loop.

Two tables that together form the closed-loop model-selection system
(directives/model_eval_loop.md):

``model_eval_verdicts`` (what the PR2 sweep cron would write; idempotent
guard so when PR2 lands it skips this table):
  One row per (purpose, candidate) CandidateVerdict from a sweep run.
  A rolling window of these tells ``apply_model_switches`` whether the
  evidence for a downgrade is consistent enough to act on.

``model_pin_overrides`` (new for PR3):
  Reversible, DB-backed model pin for one purpose. When the switch loop
  determines a cheaper model holds sustained parity, it writes an active
  row here. ``_model_for`` in ``src/llm/cli.py`` consults this BEFORE the
  hardcoded ``LLM_MODELS`` dict, so the override takes immediate effect for
  the next call with no code change. On regression the loop sets
  ``active=0`` and production reverts to the code pin — the full history
  stays in the table as an audit trail.

Why store verdicts in the DB instead of on disk:
  The ``data/model_eval/verdicts_*.jsonl`` files from eval_model_downgrade.py
  are one-run snapshots; they can't be queried across runs without reimporting.
  The DB table is the authoritative rolling tally: the sweep cron appends here
  and the switch loop reads an arbitrary lookback window from a single query.

Revision ID: 0084_model_eval_verdicts_and_pin_overrides
Revises: 0083_eval_runs
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0084_model_eval_verdicts_and_pin_overrides"
down_revision: str | Sequence[str] | None = "0083_eval_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "model_eval_verdicts" not in existing:
        op.create_table(
            "model_eval_verdicts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("purpose", sa.Text(), nullable=False),
            sa.Column("candidate", sa.Text(), nullable=False),
            sa.Column("incumbent", sa.Text(), nullable=False),
            # 'SWITCH_DOWN' | 'KEEP_INCUMBENT' | 'HOLD' | 'INSUFFICIENT_DATA'
            sa.Column("verdict", sa.Text(), nullable=False),
            sa.Column("run_id", sa.Text(), nullable=False),
            sa.Column("parity_rate", sa.Float(), nullable=True),
            sa.Column("judge_agreement", sa.Float(), nullable=True),
            sa.Column("n_cases", sa.Integer(), nullable=True),
            # Naive UTC ISO-8601 string (project naive-UTC convention).
            sa.Column("recorded_at", sa.Text(), nullable=False),
        )
        op.create_index(
            "ix_mev_purpose_candidate_recorded",
            "model_eval_verdicts",
            ["purpose", "candidate", sa.text("recorded_at DESC")],
        )
        op.create_index("ix_mev_run_id", "model_eval_verdicts", ["run_id"])

    if "model_pin_overrides" not in existing:
        op.create_table(
            "model_pin_overrides",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("purpose", sa.Text(), nullable=False),
            sa.Column("model", sa.Text(), nullable=False),
            # Who wrote this row: 'auto:model_eval_loop' or 'manual:<user>'.
            sa.Column("set_by", sa.Text(), nullable=False),
            sa.Column("set_at", sa.Text(), nullable=False),
            # JSON dict: run_id, parity_rate, judge_agreement, n_verdicts, …
            sa.Column("reason_json", sa.Text(), nullable=False),
            # 1 = active (overrides code pin); 0 = deactivated (historical record).
            sa.Column(
                "active",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            ),
        )
        op.create_index(
            "ix_mpo_purpose_active",
            "model_pin_overrides",
            ["purpose", "active"],
        )
        op.create_index("ix_mpo_set_at", "model_pin_overrides", [sa.text("set_at DESC")])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "model_pin_overrides" in existing:
        for idx_name in ("ix_mpo_set_at", "ix_mpo_purpose_active"):
            if any(i["name"] == idx_name for i in inspector.get_indexes("model_pin_overrides")):
                op.drop_index(idx_name, table_name="model_pin_overrides")
        op.drop_table("model_pin_overrides")

    if "model_eval_verdicts" in existing:
        for idx_name in ("ix_mev_run_id", "ix_mev_purpose_candidate_recorded"):
            if any(i["name"] == idx_name for i in inspector.get_indexes("model_eval_verdicts")):
                op.drop_index(idx_name, table_name="model_eval_verdicts")
        op.drop_table("model_eval_verdicts")
