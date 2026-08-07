"""red_team_items — the First-Saturday adversarial review's persisted memory
(monthly_red_team.md Phase 2, PR5).

One row per attack the monthly red-team engine (src/redteam/) produced, either
per-name (rotating-lens LLM call over a held name) or cross-book (factor-block
detection / style drift / human-capital overlay). ``ticker`` is nullable —
cross-book items have none. ``status`` starts ``open`` and is driven to
``refuted``/``accepted``/``deferred``/``closed`` by the response loop (PR6);
this migration only creates the table, PR5 writes ``open`` rows and renders
them read-only (status chips, no action buttons yet).

No FKs by repo-wide invariant (FK-poisoning); naive-UTC TEXT stamps, matching
the sibling ``coach_pings`` (0131) table this migration mirrors structurally.

Revision ID: 0147_red_team_items
Revises: 0146_positioning_budgets
Create Date: 2026-07-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0147_red_team_items"
down_revision: str | Sequence[str] | None = "0146_positioning_budgets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "red_team_items"


def upgrade() -> None:
    bind = op.get_bind()
    names = set(sa.inspect(bind).get_table_names())
    if _TABLE not in names:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            # e.g. "red_team_2026_08" — the monthly idempotency key
            # (monthly_red_team.md: "Idempotency key: red_team_{YYYY_MM}").
            sa.Column("run_key", sa.String(32), nullable=False),
            # Nullable: cross-book items (factor_block / style_drift /
            # human_capital) have no single ticker.
            sa.Column("ticker", sa.String(16), nullable=True),
            # Per-name: one of the 5 rotating lenses (lenses.py). Cross-book:
            # one of factor_block / style_drift / human_capital.
            sa.Column("lens", sa.String(32), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("attack_md", sa.Text(), nullable=False),
            sa.Column("question_md", sa.Text(), nullable=False),
            sa.Column("proposed_change_md", sa.Text(), nullable=False),
            sa.Column("severity", sa.String(8), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="open"),
            sa.Column("defer_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("response_md", sa.Text(), nullable=True),
            sa.Column("responded_at", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
        )
        op.create_index("ix_red_team_items_run_key", _TABLE, ["run_key"])
        op.create_index("ix_red_team_items_status", _TABLE, ["status"])
        op.create_index("ix_red_team_items_ticker", _TABLE, ["ticker"])


def downgrade() -> None:
    bind = op.get_bind()
    names = set(sa.inspect(bind).get_table_names())
    if _TABLE in names:
        op.drop_table(_TABLE)
