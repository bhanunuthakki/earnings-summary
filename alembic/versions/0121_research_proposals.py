"""research_proposals — The Ledger Phase-1 reviewable artifact (the inbox source).

Every Phase-1 output is an INERT proposal row + a one-tap affordance; nothing
writes live until the owner approves. The two-pass research draft lands here
(``kind='memo'`` in Wave 1; dcf/thesis/view/code in later waves).

  status: pending → {approved, researching, steered, rejected}

Safety columns:
  provenance              'derived' | 'contains_fetched' (the inert-content marker)
  tainted_by_proposal_id  the TRANSITIVE prompt-injection firebreak — set when this
                          proposal/its grounding descends from a 'contains_fetched'
                          proposal even though its OWN provenance is 'derived'; the
                          Wave-4 code leg hard-refuses on a non-NULL taint chain.
  worktree_ref            code artifact only (Wave 4); NULL here.

``evidence_json`` carries doorways ({claim, fact_ref|news_id|note_id|url}) — Law 2.
NO real FK ``REFERENCES`` (code-level RI). naive-UTC TEXT stamps.

Revision ID: 0121_research_proposals
Revises: 0120_research_tasks
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0121_research_proposals"
down_revision: str | Sequence[str] | None = "0120_research_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if "research_proposals" in set(sa.inspect(bind).get_table_names()):
        return  # idempotent
    op.create_table(
        "research_proposals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=True),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.Column("source_note_ids", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("adversarial_verdict", sa.Text(), nullable=True),
        sa.Column("budget_tier", sa.Text(), nullable=True),
        sa.Column("provenance", sa.Text(), nullable=False, server_default=sa.text("'derived'")),
        sa.Column("tainted_by_proposal_id", sa.Integer(), nullable=True),
        sa.Column("worktree_ref", sa.Text(), nullable=True),
        sa.Column("superseded_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_index("ix_research_proposals_status", "research_proposals", ["status"])
    op.create_index("ix_research_proposals_taint", "research_proposals", ["tainted_by_proposal_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "research_proposals" not in set(inspector.get_table_names()):
        return
    existing = {idx["name"] for idx in inspector.get_indexes("research_proposals")}
    for name in ("ix_research_proposals_taint", "ix_research_proposals_status"):
        if name in existing:
            op.drop_index(name, table_name="research_proposals")
    op.drop_table("research_proposals")
