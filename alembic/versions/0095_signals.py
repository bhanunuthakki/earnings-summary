"""signals — the typed information-diet substrate (tracked-name signals).

Why this exists
---------------
Before this table the repo had ONE `news` table, ONE decaying inbox scorer, and
a materiality veto — so a thesis-breach ALERTER and an information-diet CURATOR
(inverse products) were forced through one pipe: informative-but-not-breaching
signal was vetoed away, and there was nowhere to store a signal's TYPE, its
forward EVENT DATE, its curation WEIGHT, or its CADENCE.

`signals` is that store. Each row is a DIET row in a NON-decaying PULL lane
(the diet panel) — structurally separate from the decaying PUSH lane (the
inbox): a `signals` row is never converted into an InboxItem, so it never
reaches the urgency-decay scorer or the materiality veto (design_language
"Diet-vs-alert"; guard tests/test_signals_diet_guard.py). The taxonomy lives in
``src/signals/store.py``; the CHECK constraints below are kept in lockstep with
it (test_signals_store asserts the DB CHECK == the store constants).

Columns
-------
  id            INTEGER PRIMARY KEY
  ticker        TEXT NOT NULL
  signal_type   TEXT NOT NULL  — STORED at ingest (not a render-time regex);
                  CHECK ∈ {general_news, consensus_rating, investor_day,
                  buyside_rating, estimate_revision}
  title         TEXT NOT NULL  — display headline / summary
  body          TEXT           — nullable detail / snippet
  url           TEXT           — nullable source link
  firm          TEXT           — nullable free-text source ENTITY (sell-side
                  firm / investor / publisher). A `source_entity` table is a
                  deliberate fast-follow; a column is enough for v1.
  event_date    TEXT(10)       — nullable FORWARD date 'YYYY-MM-DD'. Makes an
                  investor day a queryable ROW, not LLM prose.
  published_at  TEXT NOT NULL  — 'YYYY-MM-DD HH:MM:SS' UTC naive (recency; same
                  shape as `news` so a mirrored row sorts chronologically)
  weight        REAL NOT NULL DEFAULT 1.0 — curation salience, DECOUPLED from
                  urgency decay (never a decay factor)
  cadence       TEXT NOT NULL DEFAULT 'event' — CHECK ∈ {quarterly, scheduled,
                  event}
  source_feed   TEXT           — provenance tag (yf_grades, fmp_stock_news, …)
  news_id       INTEGER        — nullable back-ref to news.id for the mirror.
                  PLAIN integer, NO REFERENCES clause: open_conn runs
                  PRAGMA foreign_keys=ON and an FK to a table absent in a
                  partial fixture schema poisons every insert (same posture as
                  0086/0093). The unique index below owns dedup instead.
  created_at    TEXT NOT NULL  — write stamp

Indices
-------
  ix_signals_ticker_type_date  (ticker, signal_type, published_at DESC) — the
      RECENCY lane (the ingest stream's "newest first" read).
  ix_signals_event_date        (event_date) WHERE event_date IS NOT NULL — the
      FORWARD agenda (ASC sort, the OPPOSITE order from the recency index; a
      separate index because the two lenses sort oppositely).
  ux_signals_news_id  UNIQUE (news_id) WHERE news_id IS NOT NULL — one signal
      per mirrored news story (makes sync_news_to_signals idempotent).
  ux_signals_event    UNIQUE (ticker, signal_type, event_date)
      WHERE event_date IS NOT NULL — one scheduled-event signal per
      (ticker, type, date) (makes record_investor_day idempotent).

Backfill
--------
Existing `news` rows are mirrored once here (INSERT OR IGNORE, so the migration
is re-run-safe): a `yf_grades` story → typed ``consensus_rating`` (routing the
free sell-side feed into the typed lane, replacing the headline regex); every
other story → ``general_news``. Non-destructive: `news` is untouched, so
material_news's recency query + the NewsRow gate are preserved verbatim.

Revision ID: 0095_signals
Revises: 0094_validation_issue_resolution
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0095_signals"
down_revision: str | Sequence[str] | None = "0094_validation_issue_resolution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Inlined vocab (migrations don't import from src). Kept in lockstep with
# src/signals/store.py — test_signals_store asserts the DB CHECK == the store
# constants, so a drift between the two fails CI.
_SIGNAL_TYPES: tuple[str, ...] = (
    "buyside_rating",
    "consensus_rating",
    "estimate_revision",
    "general_news",
    "investor_day",
)
_CADENCES: tuple[str, ...] = ("event", "quarterly", "scheduled")


def _in_clause(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "signals" in inspector.get_table_names():
        return  # idempotent

    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("signal_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("firm", sa.Text(), nullable=True),
        sa.Column("event_date", sa.Text(length=10), nullable=True),
        sa.Column("published_at", sa.Text(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("cadence", sa.Text(), nullable=False, server_default=sa.text("'event'")),
        sa.Column("source_feed", sa.Text(), nullable=True),
        sa.Column("news_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            f"signal_type IN {_in_clause(_SIGNAL_TYPES)}",
            name="ck_signals_signal_type",
        ),
        sa.CheckConstraint(
            f"cadence IN {_in_clause(_CADENCES)}",
            name="ck_signals_cadence",
        ),
    )
    # Recency lane: newest-first reads of the ingest stream.
    op.create_index(
        "ix_signals_ticker_type_date",
        "signals",
        ["ticker", "signal_type", sa.text("published_at DESC")],
    )
    # Forward agenda: soonest-first reads of forward-dated events (opposite sort).
    op.create_index(
        "ix_signals_event_date",
        "signals",
        ["event_date"],
        sqlite_where=sa.text("event_date IS NOT NULL"),
    )
    # Dedup keys (make the two reconcilers idempotent).
    op.create_index(
        "ux_signals_news_id",
        "signals",
        ["news_id"],
        unique=True,
        sqlite_where=sa.text("news_id IS NOT NULL"),
    )
    op.create_index(
        "ux_signals_event",
        "signals",
        ["ticker", "signal_type", "event_date"],
        unique=True,
        sqlite_where=sa.text("event_date IS NOT NULL"),
    )

    # One-time backfill of existing news (guarded — a partial fixture schema may
    # stamp ahead of 0065 without a `news` table). INSERT OR IGNORE keeps the
    # migration re-run-safe against ux_signals_news_id.
    if "news" in inspector.get_table_names():
        stamp = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        op.execute(
            sa.text(
                """
                INSERT OR IGNORE INTO signals
                    (ticker, signal_type, title, body, url, firm,
                     event_date, published_at, weight, cadence, source_feed,
                     news_id, created_at)
                SELECT
                    ticker,
                    CASE WHEN source_feed = 'yf_grades'
                         THEN 'consensus_rating' ELSE 'general_news' END,
                    headline, snippet, url, source,
                    NULL, published_at,
                    CASE WHEN source_feed = 'yf_grades' THEN 0.8 ELSE 0.5 END,
                    'event', source_feed, id, :stamp
                FROM news
                """
            ).bindparams(stamp=stamp)
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "signals" not in inspector.get_table_names():
        return
    existing = {idx["name"] for idx in inspector.get_indexes("signals")}
    for name in (
        "ux_signals_event",
        "ux_signals_news_id",
        "ix_signals_event_date",
        "ix_signals_ticker_type_date",
    ):
        if name in existing:
            op.drop_index(name, table_name="signals")
    op.drop_table("signals")
