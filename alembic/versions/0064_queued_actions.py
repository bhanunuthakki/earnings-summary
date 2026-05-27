"""queued_actions — drafted actions awaiting one-click user approval.

Why this exists
---------------
Every fired alert (rows in `alerts`) needs an attached drafted action
the user can approve or cancel from the dashboard: a thesis update
sentence, a bear-case append paragraph, a sizing intent change, an
earnings-prep note. The action drafter (a future PR) reads the alert's
evidence and writes one or more queued_actions rows, each carrying the
proposed change as JSON in `payload_json`. The dashboard renders the
payload as a diff/draft; on approve, the applier reads `action_kind` +
`payload_json` and writes to the right destination table (
`thesis_ledger_entries` / `position_sizing_intent` / ...).

Why a separate table from `alerts`
----------------------------------
- One alert can fan out into multiple queued actions (e.g. an
  earnings-tone alert generates BOTH a thesis_update AND an
  earnings_prep_append draft).
- The status of the alert and the status of any individual draft are
  independent: a user can approve the thesis_update half and cancel
  the earnings_prep_append half without changing the parent alert's
  status to anything other than 'approved'.
- Keeps the alerts row immutable from the drafter side; drafters only
  insert into this table.

FK to alerts.id is hard (NOT NULL) because a queued action without a
parent alert has no audit trail. `alert_id` is also indexed so the
dashboard's "show me all drafts for this alert" query is fast.

Schema
------
  id           INTEGER PRIMARY KEY
  alert_id     INTEGER NOT NULL  — FK alerts.id
  action_kind  TEXT NOT NULL     — 'thesis_update' | 'bear_append'
                                  | 'sizing_update' | 'earnings_prep_append'
  payload_json TEXT NOT NULL     — JSON; shape owned per action_kind
  status       TEXT NOT NULL DEFAULT 'pending'
                                  — 'pending' | 'approved' | 'applied'
                                  | 'cancelled'
  created_at   TEXT NOT NULL     — ISO 8601 UTC
  applied_at   TEXT              — set when applier successfully wrote
                                   the downstream row
  cancelled_at TEXT

Indices: alert_id, status.

Revision ID: 0064_queued_actions
Revises: 0063_alerts
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0064_queued_actions"
down_revision: str | Sequence[str] | None = "0063_alerts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "queued_actions" in inspector.get_table_names():
        return  # idempotent

    op.create_table(
        "queued_actions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "alert_id",
            sa.Integer(),
            sa.ForeignKey("alerts.id"),
            nullable=False,
        ),
        sa.Column("action_kind", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("applied_at", sa.Text(), nullable=True),
        sa.Column("cancelled_at", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_queued_actions_alert",
        "queued_actions",
        ["alert_id"],
    )
    op.create_index(
        "ix_queued_actions_status",
        "queued_actions",
        ["status"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "queued_actions" not in inspector.get_table_names():
        return
    existing = {idx["name"] for idx in inspector.get_indexes("queued_actions")}
    for name in ("ix_queued_actions_status", "ix_queued_actions_alert"):
        if name in existing:
            op.drop_index(name, table_name="queued_actions")
    op.drop_table("queued_actions")
