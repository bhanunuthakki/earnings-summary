"""model_pin_overrides — auto-switch infra for the model-eval loop.

Completes the closed-loop model-selection system
(directives/model_eval_loop.md) on top of the parent revision's
``model_eval_verdicts``:

``model_eval_verdicts`` (created by the PARENT revision,
0084_model_eval_verdicts / #441 — the sweep cron's accumulation sink):
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
Revises: 0084_model_eval_verdicts
Create Date: 2026-06-11

Chain repair (2026-06-12): #441 landed PR2's ``0084_model_eval_verdicts``
and #443 landed this file, BOTH revising 0083 — two alembic heads, which
broke every migration-fixture test ("Multiple head revisions"). This file
was always written to coexist with PR2's table (the idempotent guard above);
re-parenting it onto ``0084_model_eval_verdicts`` restores the linear chain
for fresh DBs and for prod regardless of which 0084 it had already applied.
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

    # model_eval_verdicts is created by the PARENT revision
    # (0084_model_eval_verdicts, #441) in the canonical sweep schema
    # (incumbent_model / candidate_model / n_parity / evaluated_at /
    # summary_json). This revision originally carried its own incompatible
    # creation block (candidate / incumbent / recorded_at — the #441+#443
    # parallel-chip divergence); the readers/writers in
    # apply_model_switches.py + llm.model_overrides now target the parent's
    # schema, so this revision only adds model_pin_overrides.

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
    # model_eval_verdicts belongs to the parent revision — its downgrade
    # drops it.
