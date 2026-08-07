"""discovery_candidates — the surfaced-names inbox (master build P5.3).

One row per (user, ticker) the discovery pipelines surfaced from the
tracked universe: factor screens over the local FMP caches and the
adjacency miner over transcripts / news / the holdings' competitive
watchlists. ``evidence_json`` is the "why surfaced" record — a list of
``{"source": ..., "detail": ...}`` objects the queue UI renders verbatim,
so a candidate always explains itself.

Lifecycle (the P5.4 queue drives it): ``new`` → ``queued`` → ``building``
→ ``built``, or → ``dismissed``. Re-running discovery refreshes
evidence/score/last_seen_at but never touches status — a dismissed name
stays dismissed (the owner said no), a built name stays built.

New-table CHECKs per 0071's policy.

Revision ID: 0081_discovery_candidates
Revises: 0080_viewspec_compile_budget
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0081_discovery_candidates"
down_revision: str | Sequence[str] | None = "0080_viewspec_compile_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = "('new', 'queued', 'building', 'built', 'dismissed')"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "discovery_candidates" in insp.get_table_names():
        return  # idempotent
    op.create_table(
        "discovery_candidates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'bhanu'"),
        ),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'new'")),
        sa.Column("score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(f"status IN {_STATUSES}", name="ck_discovery_candidates_status"),
        sa.CheckConstraint(
            "json_valid(evidence_json)", name="ck_discovery_candidates_evidence_json"
        ),
        sa.UniqueConstraint("user_id", "ticker", name="uq_discovery_candidates_user_ticker"),
    )
    op.create_index(
        "ix_discovery_candidates_user_status", "discovery_candidates", ["user_id", "status"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "discovery_candidates" not in insp.get_table_names():
        return
    op.drop_index("ix_discovery_candidates_user_status", table_name="discovery_candidates")
    op.drop_table("discovery_candidates")
