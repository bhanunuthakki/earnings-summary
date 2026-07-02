"""query_criteria — the per-case checklist cache (meta_eval_governance.md §3, PR4).

Criteria are derived ONCE per (purpose, prompt_sha256, criteria_version) by the
``query_criteria_derive`` purpose (Sonnet, prompt-only input) and cached here
forever; every later evaluation of that prompt scores against the IDENTICAL
checklist (reproducibility by construction — §3.2). Bumping the deriver's
prompt version forks history by key.

Also seeds the ``llm_budgets`` row for ``query_criteria_derive`` ($5/mo,
``on_exceed='warn'`` — steady-state ≈ new-distinct-cases-per-week × ~$0.04).

Revision ID: 0135_query_criteria
Revises: 0134_optimizer_nominations
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0135_query_criteria"
down_revision: str | Sequence[str] | None = "0134_optimizer_nominations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURPOSE = "query_criteria_derive"
_CAP = 5.00


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "query_criteria" not in existing:
        op.create_table(
            "query_criteria",
            sa.Column("purpose", sa.Text(), nullable=False),
            sa.Column("prompt_sha256", sa.Text(), nullable=False),
            sa.Column("criteria_version", sa.Text(), nullable=False),
            # The validated JSON array of {id, kind, weight, statement}.
            sa.Column("criteria_json", sa.Text(), nullable=False),
            sa.Column("derived_by_model", sa.Text(), nullable=False),
            # Naive-UTC ISO.
            sa.Column("derived_at", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint(
                "purpose", "prompt_sha256", "criteria_version", name="pk_query_criteria"
            ),
        )

    if "llm_budgets" not in existing:
        return
    cols = {c["name"] for c in sa.inspect(bind).get_columns("llm_budgets")}
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    notes = (
        "seeded by migration 0135 — per-case checklist deriver for the pairwise "
        "judge (Sonnet, cached forever per distinct prompt; warn mode: a blown "
        "cap degrades cases to facet-only, never blocks a sweep)"
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
    if "query_criteria" in existing:
        op.drop_table("query_criteria")
    if "llm_budgets" in existing:
        bind.execute(
            sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"), {"purpose": _PURPOSE}
        )
