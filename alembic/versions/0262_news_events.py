"""Add the news_events store — the material-news lane's pull-only product.

Revision ID: 0262_news_events
Revises: 0261_latest_governed_state

Owner ruling 2026-07-31: material-news alerts are retired entirely — no news
story, however material, earns an interrupt ("no filter can do a good enough
job"; the catch-up happens in the pre-earnings brief). The trigger keeps its
daily batched classification but now persists qualifying primary events here
instead of emitting alert candidates. Consumers are pull surfaces only: the
earnings-prep peek's "Since last call" section and the ticker peek's events
strip. One row per news story (UNIQUE news_id); cross-outlet duplicates are
suppressed at write time via event_key, so the table reads as one row per
real-world event per outlet-window.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0262_news_events"
down_revision: str | None = "0261_latest_governed_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "news_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("news_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        # Canonical UTC 'YYYY-MM-DD HH:MM:SS' (the news table's shape) so
        # lexical range compares are chronological.
        sa.Column("published_at", sa.Text(), nullable=False),
        sa.Column("event_key", sa.Text(), nullable=False, server_default=""),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("relevance", sa.Float(), nullable=False),
        sa.Column("why_material", sa.Text(), nullable=False, server_default=""),
        sa.Column("classified_at", sa.Text(), nullable=False),
    )
    op.create_index("ix_news_events_ticker_published", "news_events", ["ticker", "published_at"])


def downgrade() -> None:
    op.drop_index("ix_news_events_ticker_published", table_name="news_events")
    op.drop_table("news_events")
