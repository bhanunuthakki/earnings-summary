"""prompt_experiments + prompt_ab_verdicts + prompt_pin_overrides — the prompt
A/B harness and its Q1 auto-apply (meta_eval_governance.md §4 + §10, PR5).

``prompt_experiments``: one row per proposed variant (the ordered edit list +
hypothesis + the frozen model — a mid-experiment model switch must not confound
the comparison). ``prompt_ab_verdicts``: rolling per-run verdicts, deliberately
PARALLEL to model_eval_verdicts, never merged (candidate-model semantics and
variant semantics must not pollute each other — §7 non-goal).

``prompt_pin_overrides`` (owner decision Q1): mirrors ``model_pin_overrides`` —
a cleanly-promoted variant auto-applies at call time (production scopes only),
reversible, auto-demoted on regression; ``reason_json.edits`` carries the exact
diff for the git-reconciliation PR that catches the checked-in constant up.

Budget seed: ``prompt_variant_propose`` ($5/mo warn).

Revision ID: 0136_prompt_ab
Revises: 0135_query_criteria
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0136_prompt_ab"
down_revision: str | Sequence[str] | None = "0135_query_criteria"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURPOSE = "prompt_variant_propose"
_CAP = 5.00


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "prompt_experiments" not in existing:
        op.create_table(
            "prompt_experiments",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("experiment_id", sa.Text(), nullable=False, unique=True),
            sa.Column("purpose", sa.Text(), nullable=False),
            # prompt_versions at proposal time — the baseline the edits target.
            sa.Column("baseline_prompt_version", sa.Text(), nullable=False),
            sa.Column("variant_label", sa.Text(), nullable=False),
            sa.Column("hypothesis", sa.Text(), nullable=False),
            # Ordered [{find, replace}] — the ENTIRE intended change (§4.1).
            sa.Column("edits_json", sa.Text(), nullable=False),
            sa.Column("frozen_model", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False, server_default="proposed"),
            sa.Column("decision", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("decided_at", sa.Text(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.CheckConstraint(
                "status IN ('proposed','rejected_anchor','running','decided',"
                "'promoted','abandoned')",
                name="ck_prompt_experiments_status",
            ),
        )

    if "prompt_ab_verdicts" not in existing:
        op.create_table(
            "prompt_ab_verdicts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("experiment_id", sa.Text(), nullable=False),
            sa.Column("purpose", sa.Text(), nullable=False),
            sa.Column("run_id", sa.Text(), nullable=False),
            sa.Column("n_cases", sa.Integer(), nullable=True),
            sa.Column("variant_wins", sa.Integer(), nullable=True),
            sa.Column("baseline_wins", sa.Integer(), nullable=True),
            sa.Column("ties", sa.Integer(), nullable=True),
            sa.Column("win_rate", sa.Float(), nullable=True),
            sa.Column("judge_agreement", sa.Float(), nullable=True),
            sa.Column("recommendation", sa.Text(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            # Per-case audit + sample_manifest + checklist detail.
            sa.Column("summary_json", sa.Text(), nullable=True),
            sa.Column("recorded_at", sa.Text(), nullable=False),
        )
        op.create_index("ix_prompt_ab_verdicts_experiment", "prompt_ab_verdicts", ["experiment_id"])

    if "prompt_pin_overrides" not in existing:
        op.create_table(
            "prompt_pin_overrides",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("purpose", sa.Text(), nullable=False),
            sa.Column("edits_json", sa.Text(), nullable=False),
            sa.Column("experiment_id", sa.Text(), nullable=False),
            sa.Column("set_by", sa.Text(), nullable=False),
            sa.Column("set_at", sa.Text(), nullable=False),
            sa.Column("reason_json", sa.Text(), nullable=True),
            sa.Column("active", sa.Integer(), nullable=False, server_default="1"),
        )
        op.create_index(
            "ix_prompt_pin_overrides_purpose_active",
            "prompt_pin_overrides",
            ["purpose", "active"],
        )

    if "llm_budgets" not in existing:
        return
    cols = {c["name"] for c in sa.inspect(bind).get_columns("llm_budgets")}
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    notes = (
        "seeded by migration 0136 — prompt-variant proposer for the A/B harness "
        "(Opus, ~2-4 experiments/mo; warn mode: proposing is steering, never "
        "load-bearing)"
    )
    if "on_exceed" in cols:
        sql = """
            INSERT INTO llm_budgets
                (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                 on_exceed, created_at, updated_at, notes)
            VALUES (:purpose, :cap, 0.80, 0, 'warn', :now, :now, :notes)
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
    bind.execute(sa.text(sql), {"purpose": _PURPOSE, "cap": _CAP, "now": now, "notes": notes})


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "prompt_pin_overrides" in existing:
        op.drop_index("ix_prompt_pin_overrides_purpose_active", table_name="prompt_pin_overrides")
        op.drop_table("prompt_pin_overrides")
    if "prompt_ab_verdicts" in existing:
        op.drop_index("ix_prompt_ab_verdicts_experiment", table_name="prompt_ab_verdicts")
        op.drop_table("prompt_ab_verdicts")
    if "prompt_experiments" in existing:
        op.drop_table("prompt_experiments")
    if "llm_budgets" in existing:
        bind.execute(
            sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"), {"purpose": _PURPOSE}
        )
