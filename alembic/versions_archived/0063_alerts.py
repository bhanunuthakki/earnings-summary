"""alerts — fired-alert event log for the Personal CIO trigger framework.

Why this exists
---------------
The trigger framework (KPI inflection, earnings-tone shift, SayDo verdict
coming due, thesis-anchor drift, material news) needs a single durable
log of every fired alert. Each row is the system's record of: *something
worth your attention happened, here's the evidence, here's the status*.

The row is immutable from the trigger side once written. The status
column transitions left-to-right ('pending' → 'approved' | 'dismissed'
| 'expired') as the user acts on the alert through the dashboard; the
matching timestamp column (`approved_at` / `dismissed_at`) is set when
the transition fires.

`evidence_json` carries the exact numeric / textual evidence that
fired the rule — never reconstructed downstream. Storing it as TEXT
(JSON-encoded) keeps the schema flat and forward-compatible with new
trigger kinds; the trigger sensors enforce the shape per `trigger_kind`
when writing.

`signature_sha` is a content hash of (trigger_kind, ticker, evidence-
keying-fields) used for re-fire dedup: if the same KPI breach would
fire again next refresh, the sensor sees a prior alert with the same
signature already 'pending' or 'approved' and suppresses the duplicate.
Indexed for fast lookup. Signature scheme is owned by the sensors, not
by this schema.

`memo_artifact_id` is a soft pointer at llm_artifacts.id (where the
drafted memo body lives once an action drafter has run); kept nullable
and FK-less so an alert can fire before its memo is drafted.

Schema
------
  id               INTEGER PRIMARY KEY
  user_id          TEXT NOT NULL DEFAULT 'bhanu'
  ticker           TEXT NOT NULL
  trigger_kind     TEXT NOT NULL  — 'kpi_inflection' | 'earnings_tone'
                                   | 'saydo_due' | 'thesis_drift'
                                   | 'material_news'
  fired_at         TEXT NOT NULL  — ISO 8601 UTC
  status           TEXT NOT NULL DEFAULT 'pending'
                                  — 'pending' | 'approved' | 'dismissed'
                                  | 'expired'
  memo_artifact_id INTEGER        — soft pointer at llm_artifacts.id
  evidence_json    TEXT NOT NULL  — JSON; shape owned per trigger_kind
  signature_sha    TEXT NOT NULL  — content hash for re-fire dedup
  dismissed_at     TEXT
  approved_at      TEXT

Indices:
  (user_id, ticker, fired_at DESC) — per-ticker history, newest first
  (user_id, status)                — dashboard's "pending queue" filter
  (user_id, signature_sha)         — dedup lookup before insert

Revision ID: 0063_alerts
Revises: 0062_thesis_ledger_entries
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0063_alerts"
down_revision: str | Sequence[str] | None = "0062_thesis_ledger_entries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "alerts" in inspector.get_table_names():
        return  # idempotent

    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'bhanu'"),
        ),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("trigger_kind", sa.Text(), nullable=False),
        sa.Column("fired_at", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("memo_artifact_id", sa.Integer(), nullable=True),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("signature_sha", sa.Text(), nullable=False),
        sa.Column("dismissed_at", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_alerts_user_ticker_fired",
        "alerts",
        ["user_id", "ticker", sa.text("fired_at DESC")],
    )
    op.create_index(
        "ix_alerts_user_status",
        "alerts",
        ["user_id", "status"],
    )
    op.create_index(
        "ix_alerts_user_signature",
        "alerts",
        ["user_id", "signature_sha"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "alerts" not in inspector.get_table_names():
        return
    existing = {idx["name"] for idx in inspector.get_indexes("alerts")}
    for name in (
        "ix_alerts_user_signature",
        "ix_alerts_user_status",
        "ix_alerts_user_ticker_fired",
    ):
        if name in existing:
            op.drop_index(name, table_name="alerts")
    op.drop_table("alerts")
