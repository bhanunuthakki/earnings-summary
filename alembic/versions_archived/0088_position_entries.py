"""position_entries — the position-lifecycle ledger (fund-grade S5, PR 2).

The process-capture gap (fund_grade_build §1): no durable record of entry/exit
thesis snapshots — what the analyst believed AT entry (thesis excerpt,
conviction, the falsifiable conditions of #468), what they paid, why they
exited, and what the exit taught. One row per position lifecycle:

  entry_*  — snapshot at open. ``entry_date``/``entry_price`` are nullable:
      a backfilled row for a position opened before this ledger existed
      honestly records "held, opening unknown" rather than inventing a date.
      ``entry_conditions`` snapshots the open falsifiable conditions
      (decisions.decision_conditions, 0086) at entry time.
  exit_*   — stamped by the reconciler when the ticker leaves the portfolio
      (tracked_companies.list_type transition and/or tracker sell
      transactions). ``exit_reason`` / ``lessons`` / ``outcome_vs_thesis``
      are the analyst's post-exit grading, written from the holding page.
  source   — 'reconciler' (live transition detection) | 'backfill' (seeded
      for an already-held position) | 'manual'.

One OPEN row per (user_id, ticker) — partial UNIQUE; closed history
accumulates. Population is state-based (src/position_lifecycle.py compares
current list_type + tracker holdings against open rows) so every transition
path — scripts, manual SQL, future UI — is caught on the next morning run;
there is no single list_type write chokepoint to hook.

New-table CHECKs follow 0071's policy (named constraints, guarded ops).

Revision ID: 0088_position_entries
Revises: 0087_kpi_computed_from
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0088_position_entries"
down_revision: str | Sequence[str] | None = "0087_kpi_computed_from"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OUTCOMES = "('played_out', 'broke', 'mixed', 'unrelated')"
_SOURCES = "('reconciler', 'backfill', 'manual')"
_CONVICTIONS = "('low', 'medium', 'high')"


def upgrade() -> None:
    bind = op.get_bind()
    if "position_entries" in set(sa.inspect(bind).get_table_names()):
        return

    op.create_table(
        "position_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Text(), nullable=False, server_default=sa.text("'bhanu'")),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("entry_date", sa.Text(), nullable=True),  # ISO date; NULL = unknown (backfill)
        sa.Column("entry_price", sa.Float(), nullable=True),  # per-share avg cost
        sa.Column("entry_conviction", sa.Text(), nullable=True),
        sa.Column("entry_thesis_excerpt", sa.Text(), nullable=True),
        sa.Column("entry_conditions", sa.Text(), nullable=True),  # JSON, 0086 condition schema
        sa.Column("exit_date", sa.Text(), nullable=True),  # NULL = position open
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("exit_reason", sa.Text(), nullable=True),
        sa.Column("lessons", sa.Text(), nullable=True),
        sa.Column("outcome_vs_thesis", sa.Text(), nullable=True),
        sa.Column(
            "source", sa.Text(), nullable=False, server_default=sa.text("'reconciler'")
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            f"outcome_vs_thesis IS NULL OR outcome_vs_thesis IN {_OUTCOMES}",
            name="ck_position_entries_outcome",
        ),
        sa.CheckConstraint(f"source IN {_SOURCES}", name="ck_position_entries_source"),
        sa.CheckConstraint(
            f"entry_conviction IS NULL OR entry_conviction IN {_CONVICTIONS}",
            name="ck_position_entries_conviction",
        ),
    )
    op.create_index(
        "ix_position_entries_user_ticker",
        "position_entries",
        ["user_id", "ticker", "entry_date"],
    )
    # One OPEN lifecycle row per name: the reconciler's idempotency anchor.
    op.create_index(
        "uq_position_entries_open",
        "position_entries",
        ["user_id", "ticker"],
        unique=True,
        sqlite_where=sa.text("exit_date IS NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "position_entries" not in set(sa.inspect(bind).get_table_names()):
        return
    op.drop_index("uq_position_entries_open", table_name="position_entries")
    op.drop_index("ix_position_entries_user_ticker", table_name="position_entries")
    op.drop_table("position_entries")
