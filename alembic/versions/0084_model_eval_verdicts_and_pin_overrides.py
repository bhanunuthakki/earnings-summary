"""model_pin_overrides — auto-switch infra for the model-eval loop (PR3).

``model_pin_overrides``:
  Reversible, DB-backed model pin for one purpose. When the switch loop
  determines a cheaper model holds sustained parity, it writes an active
  row here. ``_model_for`` in ``src/llm/cli.py`` consults this BEFORE the
  hardcoded ``LLM_MODELS`` dict, so the override takes immediate effect for
  the next call with no code change. On regression the loop sets
  ``active=0`` and production reverts to the code pin — the full history
  stays in the table as an audit trail.

The verdict-history table (``model_eval_verdicts``) is created by the PARENT
revision ``0084_model_eval_verdicts`` (PR2's sweep sink, #441); the switch
loop reads that table's shape (candidate_model / incumbent_model /
evaluated_at).

History: this revision originally ALSO created its own divergent
``model_eval_verdicts`` and revised ``0083_eval_runs`` — #441 and #443 landed
as siblings off the same parent, leaving two alembic heads (every
``upgrade head`` failed) and two incompatible writers. Re-parented onto
0084_model_eval_verdicts and reduced to the pin-overrides table only; the
reader SQL in ``execution/apply_model_switches.py`` was converged onto the
sweep's column names.

Revision ID: 0084_model_eval_verdicts_and_pin_overrides
Revises: 0084_model_eval_verdicts
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0084_model_eval_verdicts_and_pin_overrides"
down_revision: str | Sequence[str] | None = "0084_model_eval_verdicts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

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
