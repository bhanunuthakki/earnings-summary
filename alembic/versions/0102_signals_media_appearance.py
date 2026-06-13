"""signals — add the `media_appearance` diet signal type (S11).

The information-diet second leg (directive interaction_paradigm_2026_06 §4 S11):
a curated podcast-RSS feed writes a DIET-lane signal when a tracked company exec
or a rostered investor appears on a show (BG2, Invest Like the Best, Acquired,
Logan Bartlett, In Good Company, Odd Lots, Stratechery/Sharp Tech). It is a
DIET-ONLY type — written DIRECTLY to `signals` (never mirrored from `news`), so
it can never become a `material_news` alert candidate (the diet/alert lanes stay
disjoint; tests/test_signals_diet_guard.py).

This widens the `signal_type` CHECK from the 0095 five-type vocab to six. SQLite
can't ALTER a CHECK in place, so the table is rebuilt (rename → recreate →
copy → drop), preserving the 0095 columns + indexes and adding one dedup index:

  ux_signals_media_url  UNIQUE (ticker, url)
      WHERE signal_type = 'media_appearance' AND url IS NOT NULL — one media
      row per (ticker, episode url), so re-polling the RSS allowlist is a no-op
      (record_media_appearance INSERT OR IGNOREs against it). The 0095 events
      dedup index keys on event_date, which media rows leave NULL, so media
      needs its own key.

The CHECK literals stay in lockstep with src/signals/store.py.SIGNAL_TYPES
(test_signals_store::test_check_constraints_match_store_constants).

Note (S11 taxonomy reconciliation): the directive also names a `model_revision`
lane. That is the SAME concept as the already-shipped `estimate_revision`
disclosed scaffold (0095) — "sell-side estimate / model revisions", FMP-Ultimate
gated, no free path. Adding a second synonym would fragment the taxonomy (the §6
arbiter rule), so it is NOT added; `estimate_revision` remains canonical.

Revision ID: 0102_signals_media_appearance
Revises: 0101_analyst_notes_fact_ref
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0102_signals_media_appearance"
down_revision: str | Sequence[str] | None = "0101_analyst_notes_fact_ref"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Inlined vocab (migrations don't import from src). Kept in lockstep with
# src/signals/store.py — test_signals_store asserts the DB CHECK == the store
# constants. The cadence vocab is unchanged from 0095.
_SIGNAL_TYPES_NEW: tuple[str, ...] = (
    "buyside_rating",
    "consensus_rating",
    "estimate_revision",
    "general_news",
    "investor_day",
    "media_appearance",
)
_SIGNAL_TYPES_OLD: tuple[str, ...] = (
    "buyside_rating",
    "consensus_rating",
    "estimate_revision",
    "general_news",
    "investor_day",
)
_CADENCES: tuple[str, ...] = ("event", "quarterly", "scheduled")

# The exact 0095 column list (order matters for the copy).
_COLS = (
    "id, ticker, signal_type, title, body, url, firm, event_date, "
    "published_at, weight, cadence, source_feed, news_id, created_at"
)
_BASE_INDEXES = (
    "ux_signals_event",
    "ux_signals_news_id",
    "ix_signals_event_date",
    "ix_signals_ticker_type_date",
)


def _in_clause(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _create_signals(signal_types: tuple[str, ...], *, with_media_index: bool) -> None:
    """Recreate `signals` with the 0095 shape and the given signal_type CHECK,
    plus the 0095 indexes (and, when widened, the media dedup index)."""
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
            f"signal_type IN {_in_clause(signal_types)}",
            name="ck_signals_signal_type",
        ),
        sa.CheckConstraint(
            f"cadence IN {_in_clause(_CADENCES)}",
            name="ck_signals_cadence",
        ),
    )
    op.create_index(
        "ix_signals_ticker_type_date",
        "signals",
        ["ticker", "signal_type", sa.text("published_at DESC")],
    )
    op.create_index(
        "ix_signals_event_date",
        "signals",
        ["event_date"],
        sqlite_where=sa.text("event_date IS NOT NULL"),
    )
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
    if with_media_index:
        op.create_index(
            "ux_signals_media_url",
            "signals",
            ["ticker", "url"],
            unique=True,
            sqlite_where=sa.text("signal_type = 'media_appearance' AND url IS NOT NULL"),
        )


def _rebuild(signal_types: tuple[str, ...], *, with_media_index: bool) -> None:
    """Rename → recreate → copy → drop. The only safe way to change a SQLite
    CHECK. Re-run-safe via the caller's idempotency guard."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {idx["name"] for idx in inspector.get_indexes("signals")}
    for name in (*_BASE_INDEXES, "ux_signals_media_url"):
        if name in existing:
            op.drop_index(name, table_name="signals")
    op.rename_table("signals", "_signals_rebuild")
    _create_signals(signal_types, with_media_index=with_media_index)
    op.execute(f"INSERT INTO signals ({_COLS}) SELECT {_COLS} FROM _signals_rebuild")
    op.drop_table("_signals_rebuild")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "signals" not in inspector.get_table_names():
        return  # pre-0095 fixture schema — nothing to widen
    schema = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type='table' AND name='signals'")
    ).scalar()
    if schema and "media_appearance" in str(schema):
        return  # already widened — idempotent
    _rebuild(_SIGNAL_TYPES_NEW, with_media_index=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "signals" not in inspector.get_table_names():
        return
    schema = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type='table' AND name='signals'")
    ).scalar()
    if schema and "media_appearance" not in str(schema):
        return  # already narrowed — idempotent
    # The narrow CHECK can't hold media rows — drop them before the rebuild.
    op.execute("DELETE FROM signals WHERE signal_type = 'media_appearance'")
    _rebuild(_SIGNAL_TYPES_OLD, with_media_index=False)
