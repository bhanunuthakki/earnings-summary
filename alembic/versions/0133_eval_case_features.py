"""eval_case_features — the forever-cache for case-difficulty classification
(meta_eval_governance.md §2, PR2).

The stratified sampler (``src/evals/sampler.py``) oversamples HARD cases when
drawing eval samples; difficulty comes from the ``case_difficulty_classify``
FAST-tier purpose, which reads THE PROMPT ONLY (never any model's response) so
stratification cannot encode outcome knowledge. Classification is a pure
function of the prompt ⇒ cached forever here, keyed
(purpose, prompt_sha256, classifier_version): one Haiku call per new distinct
prompt, ever. Bumping the classifier's ``prompt_versions`` entry forks history
cleanly by key.

Also seeds the ``llm_budgets`` row for ``case_difficulty_classify`` ($2/mo,
``on_exceed='warn'`` — 0083/0089/0132 precedent): a steering call, cheap and
lazily invoked inside sweeps; a blown cap should warn, never stall the sweep.

Revision ID: 0133_eval_case_features
Revises: 0132_tenet_distill_budget
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0133_eval_case_features"
down_revision: str | Sequence[str] | None = "0132_tenet_distill_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURPOSE = "case_difficulty_classify"
_CAP = 2.00


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "eval_case_features" not in existing:
        op.create_table(
            "eval_case_features",
            sa.Column("purpose", sa.Text(), nullable=False),
            sa.Column("prompt_sha256", sa.Text(), nullable=False),
            sa.Column("classifier_version", sa.Text(), nullable=False),
            sa.Column("ticker", sa.Text(), nullable=True),
            sa.Column("scope", sa.Text(), nullable=True),
            sa.Column("prompt_chars", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("difficulty", sa.Text(), nullable=False),
            sa.Column("case_type", sa.Text(), nullable=False, server_default=""),
            sa.Column("hard_signals_json", sa.Text(), nullable=False, server_default="[]"),
            # Naive-UTC ISO (repo convention).
            sa.Column("classified_at", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint(
                "purpose", "prompt_sha256", "classifier_version", name="pk_eval_case_features"
            ),
            sa.CheckConstraint(
                "difficulty IN ('easy','moderate','hard')", name="ck_eval_case_features_difficulty"
            ),
        )

    # Budget seed — idempotent, skips when llm_budgets is absent (fixture DBs).
    if "llm_budgets" not in existing:
        return
    cols = {c["name"] for c in sa.inspect(bind).get_columns("llm_budgets")}
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    notes = (
        "seeded by migration 0133 — case-difficulty classifier for the stratified "
        "eval sampler (FAST tier, cached forever per distinct prompt; warn mode: "
        "steering must never stall the sweep)"
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
    if "eval_case_features" in existing:
        op.drop_table("eval_case_features")
    if "llm_budgets" in existing:
        bind.execute(
            sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"), {"purpose": _PURPOSE}
        )
