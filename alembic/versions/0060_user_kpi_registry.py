"""user_kpi_registry — per-user, per-ticker load-bearing KPI registry.

Why this exists
---------------
The Personal CIO alerting layer (PR-series introducing trigger sensors,
the alert log, and queued actions) needs to know, for a given ticker,
which KPIs the user has chosen to *load-bear* the thesis: the metrics
whose direction-of-travel or threshold breach should fire an alert.

The existing `kpi_definitions` table (0007) is the pipeline-side registry
of which KPI is sourced from where for the deterministic extractors; it
makes no statement about thesis weight. This table is the orthogonal,
user-side registry — "what should wake me up?" — and the trigger
framework joins against it to decide whether an observed inflection on
`kpi_facts` is alert-worthy.

Carries `user_id` as a forward-looking design for eventual multi-tenant
retrofit; defaults to 'bhanu' so single-user inserts don't have to know
about the column.

Schema
------
  id                   INTEGER PRIMARY KEY
  user_id              TEXT NOT NULL DEFAULT 'bhanu'
  ticker               TEXT NOT NULL
  kpi_name             TEXT NOT NULL
  threshold_direction  TEXT       — 'above' | 'below' | NULL (informational KPI)
  threshold_value      REAL
  is_thesis_breaker    INTEGER NOT NULL DEFAULT 0   — 0/1 boolean
  scaffold_source      TEXT       — 'manual' | 'seeded_from_10k' | ...
  notes                TEXT
  created_at           TEXT NOT NULL                — ISO 8601 UTC
  updated_at           TEXT NOT NULL                — ISO 8601 UTC

Unique index on (user_id, ticker, kpi_name) — one registry row per
user/ticker/kpi tuple.

Revision ID: 0060_user_kpi_registry
Revises: 0059_kpi_facts_restatement
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0060_user_kpi_registry"
down_revision: str | Sequence[str] | None = "0059_kpi_facts_restatement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user_kpi_registry" in inspector.get_table_names():
        return  # idempotent

    op.create_table(
        "user_kpi_registry",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'bhanu'"),
        ),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("kpi_name", sa.Text(), nullable=False),
        sa.Column("threshold_direction", sa.Text(), nullable=True),
        sa.Column("threshold_value", sa.Float(), nullable=True),
        sa.Column(
            "is_thesis_breaker",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("scaffold_source", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_index(
        "uq_user_kpi_registry_user_ticker_kpi",
        "user_kpi_registry",
        ["user_id", "ticker", "kpi_name"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user_kpi_registry" not in inspector.get_table_names():
        return
    existing = {idx["name"] for idx in inspector.get_indexes("user_kpi_registry")}
    if "uq_user_kpi_registry_user_ticker_kpi" in existing:
        op.drop_index(
            "uq_user_kpi_registry_user_ticker_kpi",
            table_name="user_kpi_registry",
        )
    op.drop_table("user_kpi_registry")
