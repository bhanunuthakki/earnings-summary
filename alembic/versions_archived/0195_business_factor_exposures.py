"""business_factor_exposures — C3 business-factor taxonomy substrate.

Program plan (2026-07-19, Workstream C, C3 keystone): LLM-derived loadings of
each holding onto a small controlled business-factor taxonomy ("Brazil
consumer credit", "LatAm consumer/FX", "GLP-1/obesity reimbursement", ...;
see ``src/risk_factors.TAXONOMY``), so the Risk tab can show what the book is
actually exposed to beneath ticker-level correlation/beta stats.

Append-only-with-supersede, same is_latest/superseded_by shape 0185
(``portfolio_risk_snapshot_history``) and 0193 established for this program:
a re-derivation NEVER overwrites a row in place — it flips the prior row's
``is_latest`` to 0, sets its ``superseded_by`` to the new row's id, and
inserts the new row. ``owner_edited`` is the supremacy flag: any row with
``owner_edited=1`` is a manual correction that ``risk_factors.persist_exposures``
must never touch, auto-derived or not — see that module's docstring for the
skip-if-owner-edited rule this column exists to enforce.

``superseded_by`` carries NO foreign key, per this repo's standing
FK-poisoning invariant (a superseding row can itself be deleted/pruned later
without needing to rewrite every row that points at it).

``input_sha`` is NOT the ``llm_artifacts`` cache key (that lives in the
``llm_artifacts`` table itself, scoped ``(ticker, purpose=
'business_factor_taxonomy')``) — it is a second, independent idempotency
check at the PERSISTENCE layer: ``persist_exposures`` skips writing entirely
when every existing auto-derived row for a ticker already carries the fresh
input_sha, so a cache-hit LLM re-run (same thesis + same segment mix) never
touches this table at all, not even a superseding no-op write.

Revision ID: 0195_business_factor_exposures
Revises: 0194_research_triage
Create Date: 2026-07-24


# the PRD session's own migrations may have already claimed 0195/0196+ by the
# time this branch is rebased. The orchestrator renumbers both this file's
# revision id AND down_revision at push time to slot in after the true
# current alembic head. Do not treat 0194 as a verified parent.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0195_business_factor_exposures"

down_revision: str | Sequence[str] | None = "0194_research_triage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "business_factor_exposures"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE in insp.get_table_names():
        return  # idempotent — guards a fixture DB already stamped past this point
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.Text(), nullable=False),
        # The taxonomy label (src.risk_factors.TAXONOMY member) — NOT
        # constrained by a CHECK here (the taxonomy is a Python-side controlled
        # vocabulary the grounding gate enforces at derivation time; a DB CHECK
        # would need a migration every time the taxonomy is tuned).
        sa.Column("factor", sa.Text(), nullable=False),
        sa.Column("loading", sa.Float(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        # 'segment_derived' | 'thesis_derived' | 'owner' — see
        # src.risk_factors module docstring for what selects each.
        sa.Column("provenance", sa.Text(), nullable=False),
        # The persistence-layer idempotency key (see module docstring above) —
        # nullable because an owner-authored row (provenance='owner') has no
        # LLM input to hash.
        sa.Column("input_sha", sa.Text(), nullable=True),
        sa.Column("owner_edited", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_latest", sa.Integer(), nullable=False, server_default=sa.text("1")),
        # No FK — repo FK-poisoning invariant (see reference_platform_invariants).
        sa.Column("superseded_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_business_factor_exposures_ticker_latest",
        _TABLE,
        ["ticker", "is_latest"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    op.drop_table(_TABLE)
