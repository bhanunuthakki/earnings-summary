"""research_proposals.artifact_json — carry a drafted non-memo artifact payload.

Phase-1 Wave 2: the saved-view artifact drafts a ``ViewSpec`` that must survive
draft → preview → approve (where it becomes a real ``saved_views`` row). The
Wave-1 ``research_proposals`` table (memo-only) had nowhere to hold a
machine-readable artifact — ``body_md`` is display markdown, ``evidence_json`` is
doorways. This adds a generic nullable ``artifact_json`` the view artifact fills
with the ViewSpec dict (Wave 3's DCF/thesis drafts reuse it). Additive, nullable,
idempotent — no backfill; memo rows keep NULL.

Revision ID: 0126_research_proposal_artifact
Revises: 0125_investor_influences_kind
Create Date: 2026-07-01
"""

from __future__ import annotations

import contextlib

import sqlalchemy as sa

from alembic import op

revision = "0126_research_proposal_artifact"
down_revision = "0125_investor_influences_kind"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if _has_column("research_proposals", "artifact_json"):
        return
    op.add_column("research_proposals", sa.Column("artifact_json", sa.Text(), nullable=True))


def downgrade() -> None:
    if not _has_column("research_proposals", "artifact_json"):
        return
    with contextlib.suppress(Exception):
        op.drop_column("research_proposals", "artifact_json")
