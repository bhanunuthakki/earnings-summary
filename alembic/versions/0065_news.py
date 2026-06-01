"""news — structured per-story news table feeding the material_news trigger.

Why this exists
---------------
The material-news sensor (``src/triggers/material_news.py``) reads a ``news``
table that no migration created until now, so its ``_has_table`` guard
short-circuited and it returned ``[]`` forever — fully built but dormant. This
migration creates that table so the sensor begins classifying real stories the
moment an ingester populates it.

The six trigger-read columns (id, ticker, headline, url, published_at, snippet)
match ``src/triggers/material_news.py`` (the ``_NEWS_*`` constants) EXACTLY. The
remaining columns are ingestion bookkeeping; ``source_feed`` records WHICH
ingester wrote the row ('fmp_stock_news' | 'websearch_opus') so the two feeds
coexist and are auditable. All bookkeeping columns are invisible to the trigger
because its SELECT names columns explicitly (never ``SELECT *``), so adding
columns is safe — no edit to material_news.py is required.

``published_at`` is stored as 'YYYY-MM-DD HH:MM:SS' in UTC (naive) so the
sensor's lexical ``published_at >= ?`` recency compare (``_format_threshold``)
is chronological. An ISO-8601 'T' value would sort after a same-instant space
value and skew the 24h window. The TEXT column cannot enforce that shape; the
persistence layer (``src/news/store.NewsRow``) is the code-side gate.

Dedup: UNIQUE (ticker, url). One row per (ticker, article); a syndicated URL may
legitimately appear under two tickers, so url alone is NOT unique. Both feeds
rely on this for INSERT OR IGNORE idempotency.

Schema
------
  id           INTEGER PRIMARY KEY
  ticker       TEXT NOT NULL  — per-ticker association column
  headline     TEXT NOT NULL
  url          TEXT NOT NULL
  published_at TEXT NOT NULL  — 'YYYY-MM-DD HH:MM:SS' UTC (naive)
  snippet      TEXT           — nullable
  source       TEXT           — publication, e.g. 'Reuters'
  source_feed  TEXT NOT NULL DEFAULT 'fmp_stock_news'
                              — 'fmp_stock_news' | 'websearch_opus'
  fetched_at   TEXT NOT NULL  — 'YYYY-MM-DD HH:MM:SS' UTC (write time)

Indices: (ticker, published_at DESC) — the sensor's recency query.
Constraints: UNIQUE (ticker, url) — cross-feed dedup.

Revision ID: 0065_news
Revises: 0064_queued_actions
Create Date: 2026-05-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0065_news"
down_revision: str | Sequence[str] | None = "0064_queued_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "news" in inspector.get_table_names():
        return  # idempotent

    op.create_table(
        "news",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("published_at", sa.Text(), nullable=False),  # 'YYYY-MM-DD HH:MM:SS' UTC
        sa.Column("snippet", sa.Text(), nullable=True),
        # --- ingestion bookkeeping (invisible to the trigger) ---
        sa.Column("source", sa.Text(), nullable=True),  # publication, e.g. 'Reuters'
        sa.Column(
            "source_feed",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'fmp_stock_news'"),  # 'fmp_stock_news' | 'websearch_opus'
        ),
        sa.Column("fetched_at", sa.Text(), nullable=False),  # 'YYYY-MM-DD HH:MM:SS' UTC
        sa.UniqueConstraint("ticker", "url", name="uq_news_ticker_url"),
    )
    op.create_index(
        "ix_news_ticker_published",
        "news",
        ["ticker", sa.text("published_at DESC")],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "news" not in inspector.get_table_names():
        return
    existing = {idx["name"] for idx in inspector.get_indexes("news")}
    if "ix_news_ticker_published" in existing:
        op.drop_index("ix_news_ticker_published", table_name="news")
    op.drop_table("news")
