"""positioning_intents — the owner's durable, versioned portfolio positioning.

Fit v2 (evaluation-list scoring) needs a target book to score candidates
against — "the positioning I want", not just "the book I have". The owner
expresses that positioning conversationally (the positioning coach, PR 3 of
the program) or via the manual form; the encoded quantitative profile is
persisted HERE, and the prior saved state stays authoritative until the owner
expresses new views.

Append-and-supersede, the ``dcf_runs`` 0137 versioning precedent
(``model_provenance/versioning.py``): every save is a new row; the previous
row flips ``is_latest=0``, gets ``superseded_at`` stamped and
``superseded_by_id`` back-linked (no REFERENCES — the repo-wide FK-poisoning
invariant). A partial unique index keeps exactly one authoritative profile
per user. ``coach_session_id`` ties a coach-sourced profile back to its
``ask_sessions`` thread for provenance (again no FK).

Revision ID: 0145_positioning_intents
Revises: 0144_etf_published_data
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0145_positioning_intents"
down_revision: str | Sequence[str] | None = "0144_etf_published_data"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "positioning_intents"
_INDEX = "ux_positioning_intents_latest"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.Text(), nullable=False, server_default="bhanu"),
            # The approved plain-English positioning statement (owner's words
            # + the coach's grounded summary) — kept verbatim; the profile is
            # the machine-readable encoding of it.
            sa.Column("narrative", sa.Text(), nullable=False),
            # PositioningProfile (src/positioning/profile.py), validated
            # before every write via model_dump_json.
            sa.Column("profile_json", sa.Text(), nullable=False),
            sa.Column(
                "source",
                sa.Text(),
                nullable=False,
                # 'coach' = encoded from a coach conversation and approved;
                # 'manual' = the owner typed targets into the form directly.
            ),
            sa.Column("coach_session_id", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),  # naive-UTC ISO
            sa.Column("is_latest", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("superseded_at", sa.Text(), nullable=True),
            sa.Column("superseded_by_id", sa.Integer(), nullable=True),
            sa.CheckConstraint("source IN ('coach','manual')", name="ck_positioning_source"),
        )
        op.execute(f"CREATE UNIQUE INDEX {_INDEX} ON {_TABLE}(user_id) WHERE is_latest = 1")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
