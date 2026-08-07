"""analyst_notes.fact_ref — the stable doorway handle a note anchors to (S12).

A note (and the report comment it mirrors) anchors to a metric by its display
name today (``anchor_key``). That name is fragile: rename a KPI in the holdings
JSON and the anchor orphans — the note still exists but no longer resolves to a
live cell. ``fact_ref`` is the Instrument Paradigm Law-2 handle the renderer now
emits on each clickable datum (``kpi:{ticker}:{def_id}`` /
``fin:{ticker}:{line_item}:{fiscal_period_type}``); storing it next to the note
lets the note re-bind by stable identity across a rename, with ``anchor_key``
kept as the human display + degrade path.

  analyst_notes.fact_ref  — TEXT, nullable. The stable handle, when the anchored
      element carried one (KPI cells do; segment / saydo / failure-mode anchors
      stay text-keyed — see directives/design_language.md §4). NULL for every
      note whose anchor has no PK-resolvable series.

A partial index backs the future resolve-by-handle lookup (notes for one
fact_ref) without taxing the NULL majority — same posture as 0093's link
indexes.

Revision ID: 0101_analyst_notes_fact_ref
Revises: 0100_investor_calibration
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0101_analyst_notes_fact_ref"
down_revision: str | Sequence[str] | None = "0100_investor_calibration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in insp.get_table_names()


def _has_column(insp: sa.Inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in insp.get_columns(table))


def _has_index(insp: sa.Inspector, table: str, name: str) -> bool:
    return any(ix["name"] == name for ix in insp.get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "analyst_notes"):
        return  # pre-0074 partial schema — nothing to anchor from
    if not _has_column(insp, "analyst_notes", "fact_ref"):
        # Plain nullable ADD COLUMN — no batch recreate (a recreate would have to
        # reproduce 0074's FK-to-tenants on fixtures where tenants may be absent).
        op.add_column("analyst_notes", sa.Column("fact_ref", sa.Text(), nullable=True))
    insp = sa.inspect(bind)  # re-inspect: the column just landed
    if not _has_index(insp, "analyst_notes", "ix_analyst_notes_fact_ref"):
        op.create_index(
            "ix_analyst_notes_fact_ref",
            "analyst_notes",
            ["fact_ref"],
            sqlite_where=sa.text("fact_ref IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "analyst_notes") or not _has_column(insp, "analyst_notes", "fact_ref"):
        return
    if _has_index(insp, "analyst_notes", "ix_analyst_notes_fact_ref"):
        op.drop_index("ix_analyst_notes_fact_ref", table_name="analyst_notes")
    with op.batch_alter_table("analyst_notes") as batch:
        batch.drop_column("fact_ref")
