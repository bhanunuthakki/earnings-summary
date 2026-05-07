"""transcripts_has_qa_section — flag prepared-only vs full Q&A transcripts.

Some transcripts (notably IR-published prepared-remarks PDFs) cut off at the
hand-off to Q&A. Downstream consumers — Say-Do extraction, analyst-question
KPI mining, thesis evaluators — depend on the Q&A portion and silently produce
worse output on prepared-only inputs. Adding a tri-state column lets the
fetch/ingest layer surface "Q&A missing" up the stack so the orchestrator can
either re-pull from a full source or prompt the user.

Tri-state semantics on `has_qa_section`:
  - NULL  : not yet inspected (legacy rows; backfill via re-ingest)
  - 1     : Q&A section detected (analyst questions present)
  - 0     : prepared remarks only (no analyst Q&A content)

Revision ID: 0019_transcripts_has_qa_section
Revises: 0018_dcf_runs_breakdown
Create Date: 2026-05-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_transcripts_has_qa_section"
down_revision: str | Sequence[str] | None = "0018_dcf_runs_breakdown"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transcripts",
        sa.Column("has_qa_section", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transcripts", "has_qa_section")
