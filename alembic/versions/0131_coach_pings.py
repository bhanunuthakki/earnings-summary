"""coach_pings + coach_mutes — the nag governor's ledger (W2, governed initiation).

The 2026-07-02 grill granted the coach a FULL-but-governed initiation license
(rescinding master_build's pull-only) with the owner's own kill bar: "kill it
if it reads as nagging." The governor is deterministic — zero LLM — and these
two tables are its memory:

- ``coach_pings``: one row per initiation moment ever considered.
  ``status``: sent | digest (over the frequency cap — waits in the morning
  digest) | skipped_muted | skipped_stale (failed the freshness gate) |
  dismissed | acted. UNIQUE (class, key) makes every moment once-only.
- ``coach_mutes``: a class the owner dismissed 3 consecutive times mutes
  itself until explicitly cleared — dismissals train it, it never argues.

No FKs by repo-wide invariant (FK-poisoning); naive-UTC stamps.

Revision ID: 0131_coach_pings
Revises: 0130_owner_decision_extension
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0131_coach_pings"
down_revision: str | None = "0130_owner_decision_extension"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    names = set(sa.inspect(bind).get_table_names())
    if "coach_pings" not in names:
        op.create_table(
            "coach_pings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("class_", sa.String(32), nullable=False),
            sa.Column("key", sa.String(128), nullable=False),
            sa.Column("ticker", sa.String(16), nullable=True),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("source_ref", sa.String(128), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.UniqueConstraint("class_", "key", name="uq_coach_pings_class_key"),
        )
        op.create_index("ix_coach_pings_status", "coach_pings", ["status"])
    if "coach_mutes" not in names:
        op.create_table(
            "coach_mutes",
            sa.Column("class_", sa.String(32), primary_key=True),
            sa.Column("muted_at", sa.Text(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    names = set(sa.inspect(bind).get_table_names())
    if "coach_pings" in names:
        op.drop_table("coach_pings")
    if "coach_mutes" in names:
        op.drop_table("coach_mutes")
