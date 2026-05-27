"""position_sizing_intent — per-user, per-ticker sizing intent declarations.

Why this exists
---------------
The alerting layer's `sizing_update` action kind needs a durable home for
the user's stated sizing posture per ticker — e.g. "target 5%", "max
8%", "trim above $X", "cut if down 25%". The trigger framework reads
these rows when proposing queued actions (e.g. an inflection that pushes
position to >max_pct can pre-draft a trim memo) and the dashboard
surfaces them as context next to the position card.

Multiple intent rows per (user_id, ticker) are allowed and expected — a
single ticker often carries a target_pct AND a max_pct AND a free-form
sizing_note. `intent_kind` is the per-row qualifier; `intent_value` is
the numeric (where applicable) and `narrative` is the always-present
free-text explanation.

Schema
------
  id           INTEGER PRIMARY KEY
  user_id      TEXT NOT NULL DEFAULT 'bhanu'
  ticker       TEXT NOT NULL
  intent_kind  TEXT NOT NULL   — 'target_pct' | 'max_pct' | 'trim_at_price'
                                | 'stop_at_loss_pct' | 'sizing_note'
  intent_value REAL            — null for free-text 'sizing_note'
  narrative    TEXT
  created_at   TEXT NOT NULL   — ISO 8601 UTC
  updated_at   TEXT NOT NULL   — ISO 8601 UTC

Non-unique index on (user_id, ticker) — the dashboard's per-ticker
"all sizing intents" query is the hot path.

Revision ID: 0061_position_sizing_intent
Revises: 0060_user_kpi_registry
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0061_position_sizing_intent"
down_revision: str | Sequence[str] | None = "0060_user_kpi_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "position_sizing_intent" in inspector.get_table_names():
        return  # idempotent

    op.create_table(
        "position_sizing_intent",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'bhanu'"),
        ),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("intent_kind", sa.Text(), nullable=False),
        sa.Column("intent_value", sa.Float(), nullable=True),
        sa.Column("narrative", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_position_sizing_intent_user_ticker",
        "position_sizing_intent",
        ["user_id", "ticker"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "position_sizing_intent" not in inspector.get_table_names():
        return
    existing = {idx["name"] for idx in inspector.get_indexes("position_sizing_intent")}
    if "ix_position_sizing_intent_user_ticker" in existing:
        op.drop_index(
            "ix_position_sizing_intent_user_ticker",
            table_name="position_sizing_intent",
        )
    op.drop_table("position_sizing_intent")
