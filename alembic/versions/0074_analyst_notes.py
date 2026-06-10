"""analyst_notes — durable, object-anchored analyst memory.

Why this exists
---------------
Every thought the analyst records today lives in a per-report container:
inline comments and chat threads persist under
``data/report_comments/<T>/<YYYY-MM-DD>.json`` and die with that report
build — the next build starts blank, so questions, watch-items, and
decisions never resurface and no LLM call can consult them. The thesis
ledger (0062) is durable but only records *accepted* changes, not the
thinking around them.

``analyst_notes`` is the cross-build memory: one row per thought, anchored
to an object (ticker, report section, KPI, fact, alert) via the same
``anchor_type``/``anchor_key`` vocabulary the comment system already emits,
classified by *semantics* (question / decision / watch / assumption /
observation) rather than by action route, and never hard-deleted —
correction is a supersede chain (``supersedes_id``), exactly like the
restatement chains on the fact tables.

Rows arrive three ways (the ``source`` column): mirrored from report
comments (``comment`` — written by the reconciler in
``src/user_state/notes.py``, keyed idempotently on ``source_ref``),
captured from chat / alerts (follow-on wiring), or typed directly
(``manual``).

Schema
------
  id              INTEGER PRIMARY KEY
  user_id         TEXT NOT NULL DEFAULT 'bhanu'  — FK tenants.id (0073)
  ticker          TEXT            — NULL = portfolio-level note
  kind            TEXT NOT NULL   — question | decision | watch
                                  | assumption | observation
  status          TEXT NOT NULL   — open | resolved | superseded | archived
  body            TEXT NOT NULL
  anchor_type     TEXT            — comments.AnchorType vocabulary
  anchor_key      TEXT            — semantic key within the anchor type
  source          TEXT NOT NULL   — comment | chat | alert | manual
  source_ref      TEXT            — e.g. 'NU/2026-05-18/cmt_..' (unique per
                                    user+source when present → idempotent sync)
  supersedes_id   INTEGER         — FK analyst_notes.id (correction chain)
  resolution_note TEXT
  context_json    TEXT            — JSON extras (report_date, tab,
                                    selected_text, follow-up thread, ...)
  created_at      TEXT NOT NULL   — ISO 8601 UTC
  updated_at      TEXT NOT NULL   — ISO 8601 UTC
  resolved_at     TEXT

Indexes: (user_id, ticker, status) is the priors-anchor hot path ("open
notes for this ticker"); (user_id, status, kind) powers cross-ticker
queries ("all my open questions"); the partial-unique on
(user_id, source, source_ref) makes the comment reconciler an upsert.

The tenants guard mirrors 0073: in the linear chain tenants always exists
by now, but test fixtures stamp partial schemas, so the table+seed are
recreated defensively before the FK lands.

Revision ID: 0074_analyst_notes
Revises: 0073_tenant_identity
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0074_analyst_notes"
down_revision: str | Sequence[str] | None = "0073_tenant_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept in lockstep with 0073's canonical tenant and identity.DEFAULT_USER_ID.
CANONICAL_TENANT = "bhanu"

# Canonical enum vocabularies — keep in lockstep with src/user_state/notes.py.
_KINDS = "('question', 'decision', 'watch', 'assumption', 'observation')"
_STATUSES = "('open', 'resolved', 'superseded', 'archived')"
_SOURCES = "('comment', 'chat', 'alert', 'manual')"


def _has_table(inspector: sa.Inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # Defensive tenants guard (see docstring) — same shape as 0073 step 1+2.
    if not _has_table(insp, "tenants"):
        op.create_table(
            "tenants",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("created_at", sa.Text(), nullable=False),
        )
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0).isoformat()
    op.execute(
        f"INSERT OR IGNORE INTO tenants (id, created_at) VALUES ('{CANONICAL_TENANT}', '{now}')"
    )

    if _has_table(insp, "analyst_notes"):
        return  # idempotent

    op.create_table(
        "analyst_notes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text(f"'{CANONICAL_TENANT}'"),
        ),
        sa.Column("ticker", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("anchor_type", sa.Text(), nullable=True),
        sa.Column("anchor_key", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("supersedes_id", sa.Integer(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("context_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("resolved_at", sa.Text(), nullable=True),
        sa.CheckConstraint(f"kind IN {_KINDS}", name="ck_analyst_notes_kind"),
        sa.CheckConstraint(f"status IN {_STATUSES}", name="ck_analyst_notes_status"),
        sa.CheckConstraint(f"source IN {_SOURCES}", name="ck_analyst_notes_source"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["tenants.id"], name="fk_analyst_notes_user_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"], ["analyst_notes.id"], name="fk_analyst_notes_supersedes"
        ),
    )
    op.create_index(
        "ix_analyst_notes_user_ticker_status",
        "analyst_notes",
        ["user_id", "ticker", "status"],
    )
    op.create_index(
        "ix_analyst_notes_user_status_kind",
        "analyst_notes",
        ["user_id", "status", "kind"],
    )
    op.create_index(
        "uq_analyst_notes_source_ref",
        "analyst_notes",
        ["user_id", "source", "source_ref"],
        unique=True,
        sqlite_where=sa.text("source_ref IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "analyst_notes"):
        return
    existing = {idx["name"] for idx in insp.get_indexes("analyst_notes")}
    for name in (
        "uq_analyst_notes_source_ref",
        "ix_analyst_notes_user_status_kind",
        "ix_analyst_notes_user_ticker_status",
    ):
        if name in existing:
            op.drop_index(name, table_name="analyst_notes")
    op.drop_table("analyst_notes")
