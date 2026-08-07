"""thesis_ledger_entries — durable per-user, per-ticker thesis history.

Why this exists
---------------
The Personal CIO loop has a write target for approved queued actions:
when the user clicks "approve" on an alert's drafted memo (thesis
update, bear append, earnings-prep note), the memo's body should land
in a durable, time-ordered ledger so the next earnings-prep pass, the
next thesis read, and the next audit-pass time-travel all see it.

This is intentionally separate from the existing thesis_state and
bear_case caches: those caches hold the *current view*, with churn from
LLM re-generations. The ledger is append-only — every accepted action
adds one row and nothing ever rewrites prior rows. Renderers that need
"current thesis" still read the caches; renderers that need "history of
how the thesis moved" walk this table.

`source_alert_id` is nullable because the ledger also receives manual
entries the user types directly (not all rows originate from an alert).
When it IS set, it points at the alert that produced the queued action
the user approved — provenance link without a hard FK so the table
stays writeable while the alerts schema is still in flux. `accepted_at`
is distinct from `created_at` to preserve the draft-then-approve
two-step (the alert's queued_action may sit pending for hours before
the user approves it).

Schema
------
  id              INTEGER PRIMARY KEY
  user_id         TEXT NOT NULL DEFAULT 'bhanu'
  ticker          TEXT NOT NULL
  entry_kind      TEXT NOT NULL  — 'thesis_update' | 'bear_append'
                                  | 'earnings_prep_note'
  body            TEXT NOT NULL
  source_alert_id INTEGER        — soft pointer at alerts.id; no FK
  created_at      TEXT NOT NULL  — ISO 8601 UTC
  accepted_at     TEXT NOT NULL  — ISO 8601 UTC

Index on (user_id, ticker, created_at DESC) — the dashboard's "show me
the ledger newest first" query is the hot path.

Revision ID: 0062_thesis_ledger_entries
Revises: 0061_position_sizing_intent
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0062_thesis_ledger_entries"
down_revision: str | Sequence[str] | None = "0061_position_sizing_intent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "thesis_ledger_entries" in inspector.get_table_names():
        return  # idempotent

    op.create_table(
        "thesis_ledger_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'bhanu'"),
        ),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("entry_kind", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("source_alert_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("accepted_at", sa.Text(), nullable=False),
    )
    # SQLite ignores per-column DESC inside CREATE INDEX in older versions but
    # honors it in 3.7.4+; alembic emits the directive verbatim, so the
    # planner can still pick a descending scan when 'ORDER BY created_at DESC'
    # appears in the read query.
    op.create_index(
        "ix_thesis_ledger_user_ticker_created",
        "thesis_ledger_entries",
        ["user_id", "ticker", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "thesis_ledger_entries" not in inspector.get_table_names():
        return
    existing = {idx["name"] for idx in inspector.get_indexes("thesis_ledger_entries")}
    if "ix_thesis_ledger_user_ticker_created" in existing:
        op.drop_index(
            "ix_thesis_ledger_user_ticker_created",
            table_name="thesis_ledger_entries",
        )
    op.drop_table("thesis_ledger_entries")
