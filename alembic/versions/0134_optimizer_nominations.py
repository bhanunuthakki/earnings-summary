"""optimizer_nominations + candidate_models — the self-steering optimizer's
nomination feed and the data-refreshed candidate frontier
(meta_eval_governance.md §1.3 + §10.1, PR3).

``optimizer_nominations``: one row per nominated action from the monthly Opus
nominator (or its deterministic fallback) — ranked model-downgrade tests,
prompt-experiment nominations, and (owner decision Q2) EXCLUSIONS with a TTL
(``expires_at``) so a bad negative nomination self-heals: past its TTL the
purpose re-enters the nominable universe automatically, and the sweep's
rotation floor force-reincludes anything unswept too long regardless.

``candidate_models``: the frontier-research overlay (owner decision Q5b). The
static ``model_ladder.MODEL_LADDER`` stays the code seed; monthly
``model_frontier_research`` upserts newly-discovered / newly-cheap models here
with verified prices + a promise score, and discovered rows AUTO-ENTER the TEST
pool (eligible for scope='model_eval' replay immediately — production routing
is untouched and still gated by the full switch bar).

Budget seeds: ``optimizer_nominator`` ($3/mo warn) + ``model_frontier_research``
($3/mo warn) — steering calls; a blown cap warns, never stalls the loop
(0083/0089/0132/0133 precedent).

Revision ID: 0134_optimizer_nominations
Revises: 0133_eval_case_features
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0134_optimizer_nominations"
down_revision: str | Sequence[str] | None = "0133_eval_case_features"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BUDGET_SEEDS: list[tuple[str, float, str]] = [
    (
        "optimizer_nominator",
        3.00,
        "seeded by migration 0134 — monthly Opus nominator (meta-eval steering); "
        "warn mode: steering must never stall the loop",
    ),
    (
        "model_frontier_research",
        3.00,
        "seeded by migration 0134 — monthly pareto-frontier research (Opus+web, "
        "candidate_models refresh); warn mode",
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "optimizer_nominations" not in existing:
        op.create_table(
            "optimizer_nominations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            # uuid4 hex per nominator invocation — one run emits many rows.
            sa.Column("nomination_run_id", sa.Text(), nullable=False),
            sa.Column("purpose", sa.Text(), nullable=False),
            sa.Column(
                "kind",
                sa.Text(),
                nullable=False,
            ),
            sa.Column("priority", sa.Integer(), nullable=False),
            sa.Column("headroom_usd_30d", sa.Float(), nullable=True),
            sa.Column("cost_usd_30d", sa.Float(), nullable=True),
            sa.Column("calls_30d", sa.Integer(), nullable=True),
            sa.Column("incumbent_model", sa.Text(), nullable=False),
            # JSON array of ladder/candidate model ids (empty for exclusions).
            sa.Column("candidates_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("rationale", sa.Text(), nullable=False),
            sa.Column("risk_tier", sa.Text(), nullable=False),
            sa.Column("suggested_min_n", sa.Integer(), nullable=True),
            sa.Column("source", sa.Text(), nullable=False),
            # Fingerprint of the candidate frontier at nomination time — a
            # changed frontier is the re-nomination trigger.
            sa.Column("ladder_sha", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
            # Naive-UTC ISO. expires_at only for kind='exclude' (Q2 TTL).
            sa.Column("expires_at", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.CheckConstraint(
                "kind IN ('model_downgrade','prompt_experiment','exclude')",
                name="ck_optimizer_nominations_kind",
            ),
            sa.CheckConstraint(
                "risk_tier IN ('safe','candidate','risky')",
                name="ck_optimizer_nominations_risk",
            ),
            sa.CheckConstraint(
                "source IN ('opus','deterministic_fallback')",
                name="ck_optimizer_nominations_source",
            ),
            sa.CheckConstraint(
                "status IN ('pending','swept','skipped','expired')",
                name="ck_optimizer_nominations_status",
            ),
        )
        op.create_index(
            "ix_optimizer_nominations_status",
            "optimizer_nominations",
            ["status", "priority"],
        )

    if "candidate_models" not in existing:
        op.create_table(
            "candidate_models",
            sa.Column("model_id", sa.Text(), primary_key=True),
            sa.Column("family", sa.Text(), nullable=False),
            sa.Column("input_usd_per_mtok", sa.Float(), nullable=False),
            sa.Column("output_usd_per_mtok", sa.Float(), nullable=False),
            # cheap x plausibly-capable, 0..1 — rank-only, like ladder prices.
            sa.Column("promise", sa.Float(), nullable=False, server_default="0.5"),
            sa.Column("source", sa.Text(), nullable=False, server_default="frontier_research"),
            sa.Column("status", sa.Text(), nullable=False, server_default="active"),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("research_run_id", sa.Text(), nullable=True),
            # Naive-UTC ISO.
            sa.Column("first_seen_at", sa.Text(), nullable=False),
            sa.Column("verified_at", sa.Text(), nullable=False),
            sa.CheckConstraint(
                "family IN ('claude','gemini','openrouter')",
                name="ck_candidate_models_family",
            ),
            sa.CheckConstraint(
                "source IN ('frontier_research','seed','manual')",
                name="ck_candidate_models_source",
            ),
            sa.CheckConstraint("status IN ('active','retired')", name="ck_candidate_models_status"),
        )

    # Budget seeds — idempotent, skip when llm_budgets absent (fixture DBs).
    if "llm_budgets" not in existing:
        return
    cols = {c["name"] for c in sa.inspect(bind).get_columns("llm_budgets")}
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
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
    for purpose, cap, notes in _BUDGET_SEEDS:
        bind.execute(sa.text(sql), {"purpose": purpose, "cap": cap, "now": now, "notes": notes})


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "optimizer_nominations" in existing:
        op.drop_index("ix_optimizer_nominations_status", table_name="optimizer_nominations")
        op.drop_table("optimizer_nominations")
    if "candidate_models" in existing:
        op.drop_table("candidate_models")
    if "llm_budgets" in existing:
        for purpose, _cap, _notes in _BUDGET_SEEDS:
            bind.execute(
                sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"),
                {"purpose": purpose},
            )
