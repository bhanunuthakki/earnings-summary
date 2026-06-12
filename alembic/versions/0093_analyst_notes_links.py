"""analyst_notes ↔ decisions / position_entries links (fund-grade S15, PR 1).

The process-capture pieces exist separately — analyst_notes (0074, the
journal), decisions with outcome grading (0046/0086), position_entries
(0088, the lifecycle ledger) — but nothing connects them: a journal note
written when a decision was made never resolves when that decision grades,
and a watch-item on a position outlives the position silently. This
migration adds the link half:

  analyst_notes.decision_id        — decisions.id the note is about. Plain
      nullable INTEGER, no REFERENCES clause: ``user_state._db.open_conn``
      runs ``PRAGMA foreign_keys = ON``, and an FK to a parent table that is
      absent in a partial schema (test fixtures stamp pre-0046 heads; the
      decisions table never materializes there) poisons EVERY insert into
      the child — even NULL link values fail with "no such table". Same
      posture as 0086's ``source_memo_id``: code-level validation
      (src/journal_links.py) owns referential integrity.
  analyst_notes.position_entry_id  — position_entries.id, same contract.
  analyst_notes.link_auto_resolve  — 0/1. Chosen at link time: 1 lets the
      morning reconciliation rung resolve the note automatically when the
      linked object concludes (decision graded / position exited); 0 (the
      default) surfaces it in the pending-reconciliation view instead —
      memory is the analyst's, auto-closing it is opt-in.

Partial indexes back the reconciliation queries (open notes whose linked
object concluded) without taxing the unlinked majority.

Revision ID: 0093_analyst_notes_links
Revises: 0092_merge_followup_dcf_heads
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0093_analyst_notes_links"
down_revision: str | Sequence[str] | None = "0092_merge_followup_dcf_heads"
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
        return  # pre-0074 partial schema — nothing to link from
    if not _has_column(insp, "analyst_notes", "decision_id"):
        # Plain ADD COLUMNs (nullable / constant default) — no batch recreate
        # needed, and none wanted: a recreate would have to reproduce 0074's
        # FK-to-tenants on fixtures where tenants may be absent.
        op.add_column("analyst_notes", sa.Column("decision_id", sa.Integer(), nullable=True))
        op.add_column("analyst_notes", sa.Column("position_entry_id", sa.Integer(), nullable=True))
        op.add_column(
            "analyst_notes",
            sa.Column(
                "link_auto_resolve",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
    insp = sa.inspect(bind)  # re-inspect: columns just landed
    if not _has_index(insp, "analyst_notes", "ix_analyst_notes_decision"):
        op.create_index(
            "ix_analyst_notes_decision",
            "analyst_notes",
            ["decision_id"],
            sqlite_where=sa.text("decision_id IS NOT NULL"),
        )
    if not _has_index(insp, "analyst_notes", "ix_analyst_notes_position_entry"):
        op.create_index(
            "ix_analyst_notes_position_entry",
            "analyst_notes",
            ["position_entry_id"],
            sqlite_where=sa.text("position_entry_id IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "analyst_notes") or not _has_column(
        insp, "analyst_notes", "decision_id"
    ):
        return
    for name in ("ix_analyst_notes_position_entry", "ix_analyst_notes_decision"):
        if _has_index(insp, "analyst_notes", name):
            op.drop_index(name, table_name="analyst_notes")
    with op.batch_alter_table("analyst_notes") as batch:
        batch.drop_column("link_auto_resolve")
        batch.drop_column("position_entry_id")
        batch.drop_column("decision_id")
