"""Recreate brief_provenance_log — revive the live per-render provenance writer.

The table was created in 0023 and dropped in 0031 as "dead", but
``execution/build_artifacts.py::_log_brief_provenance`` still writes to it,
guarded only by a table-existence check. So in prod (where the table is absent)
the per-line-item / per-section render provenance is silently dropped on every
brief — the writer is a no-op. Recreate the table with its exact 0023 schema so
the writer persists again, restoring the source-supersession audit trail.

Idempotent: only creates the table/index when absent (a DB that still has the
table — e.g. one that never ran 0031 — is left untouched), so this is safe to
run regardless of a given DB's 0031 history.

Revision ID: 0069_recreate_brief_provenance_log
Revises: 0068_substrate_constraints
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0069_recreate_brief_provenance_log"
down_revision: str | Sequence[str] | None = "0068_substrate_constraints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(inspector: sa.Inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_table(insp, "brief_provenance_log"):
        return
    # Exact 0023 schema — keep in lockstep with the INSERT in
    # execution/build_artifacts.py::_log_brief_provenance.
    op.create_table(
        "brief_provenance_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("generation_date", sa.String(length=10), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("sources_used", sa.Text(), nullable=False),
        sa.Column("sections_status", sa.Text(), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("artifact_path", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_brief_provenance_ticker_date",
        "brief_provenance_log",
        ["ticker", "generation_date"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "brief_provenance_log"):
        return
    op.drop_index("ix_brief_provenance_ticker_date", table_name="brief_provenance_log")
    op.drop_table("brief_provenance_log")
