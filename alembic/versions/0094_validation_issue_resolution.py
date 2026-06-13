"""validation_issues resolution audit — resolved_by + resolution_note.

The quarantine ledger (0006) already carries ``resolved_at`` but nothing
records WHO resolved an issue or WHY. The Instrument-Paradigm "provenance is
actionable" work (S10) adds a per-row resolve action: the analyst marks a
data-quality issue resolved from the console, and that needs an audit trail.

  validation_issues.resolved_by      — the actor (DEFAULT_USER_ID, e.g.
      'bhanu'). NULL on open issues and legacy rows.
  validation_issues.resolution_note  — free-text "why this is fine / what was
      done". NULL when the analyst resolved without a note.

Plain nullable ADD COLUMNs — no batch recreate (and none wanted: a recreate
would have to reproduce 0006's FK-to-documents on fixtures where ``documents``
may be absent). The resolve writer (src/validation_issues_store.py) sets all
three columns in one UPDATE.

Revision ID: 0094_validation_issue_resolution
Revises: 0093_analyst_notes_links
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0094_validation_issue_resolution"
down_revision: str | Sequence[str] | None = "0093_analyst_notes_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in insp.get_table_names()


def _has_column(insp: sa.Inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "validation_issues"):
        return  # pre-0006 partial schema — nothing to extend
    if not _has_column(insp, "validation_issues", "resolved_by"):
        op.add_column("validation_issues", sa.Column("resolved_by", sa.Text(), nullable=True))
    if not _has_column(insp, "validation_issues", "resolution_note"):
        op.add_column("validation_issues", sa.Column("resolution_note", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "validation_issues"):
        return
    with op.batch_alter_table("validation_issues") as batch:
        if _has_column(insp, "validation_issues", "resolution_note"):
            batch.drop_column("resolution_note")
        if _has_column(insp, "validation_issues", "resolved_by"):
            batch.drop_column("resolved_by")
