"""weekly_packet_runs/items + decision_nudges + pending_telegram_replies —
PR2 of the behavioral-flagship program (navigation_ia.md §3).

Four small tables backing the three interruption mechanisms:

  * ``weekly_packet_runs`` / ``weekly_packet_items`` — the Sunday packet's
    state. One run per ISO (year, week) — the idempotency key — so a re-run
    for the same week UPDATES (adds newly-surfaced items) rather than
    duplicating; each item records its own verdict + acted_at so the packet
    is resumable across a day of Telegram taps.
  * ``decision_nudges`` — the point-of-intent nudge ledger. ``UNIQUE
    (decision_id)`` enforces "a decision is nudged AT MOST once ever" at the
    schema level, not just in application logic.
  * ``pending_telegram_replies`` — the shared awaited-free-text-reply stash
    used by BOTH the decision-nudge fill-in flow and the packet's Rewrite
    action (one mechanism, two producers) — the poller's text-capture path
    checks this table before falling through to the default musing route.

Mirrors the 0141/0148 stamped-DB guard (``sa.inspect`` before CREATE) and the
0146 budget-seed shape (idempotent ON CONFLICT DO NOTHING, guarded on
``llm_budgets`` existing).

Revision ID: 0149_weekly_packet_and_nudges
Revises: 0148_panel_activation_counts
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0149_weekly_packet_and_nudges"
down_revision: str | Sequence[str] | None = "0148_panel_activation_counts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNS_DDL = """
CREATE TABLE weekly_packet_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iso_year INTEGER NOT NULL,
    iso_week INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    total_items INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (iso_year, iso_week)
)
"""

_ITEMS_DDL = """
CREATE TABLE weekly_packet_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES weekly_packet_runs(id),
    item_kind TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    ticker TEXT,
    title TEXT NOT NULL,
    predraft TEXT,
    message_id INTEGER,
    sent_at TEXT,
    verdict TEXT,
    acted_at TEXT,
    order_index INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, item_kind, ref_id)
)
"""

_NUDGES_DDL = """
CREATE TABLE decision_nudges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL UNIQUE,
    chat_id INTEGER,
    status TEXT NOT NULL DEFAULT 'sent',
    sent_at TEXT NOT NULL,
    awaiting_until TEXT,
    filled_at TEXT
)
"""

_PENDING_REPLIES_DDL = """
CREATE TABLE pending_telegram_replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT
)
"""

_TABLES: tuple[tuple[str, str], ...] = (
    ("weekly_packet_runs", _RUNS_DDL),
    ("weekly_packet_items", _ITEMS_DDL),
    ("decision_nudges", _NUDGES_DDL),
    ("pending_telegram_replies", _PENDING_REPLIES_DDL),
)

_BUDGET_SEED = (
    "weekly_packet_predraft",
    5.00,
    "seeded by migration 0149 — the Sunday packet's per-item LLM pre-draft "
    "(Haiku-tier, short); warn mode (a blown cap must not silently stop the "
    "packet — items degrade to buttons-without-a-draft per item, never abort)",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    for name, ddl in _TABLES:
        if name not in existing:
            op.execute(sa.text(ddl))

    if "llm_budgets" not in existing:
        return
    cols = {c["name"] for c in inspector.get_columns("llm_budgets")}
    now = datetime.now(UTC).isoformat()
    purpose, cap, notes = _BUDGET_SEED
    if "on_exceed" in cols:
        sql = """
            INSERT INTO llm_budgets
                (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                 on_exceed, created_at, updated_at, notes)
            VALUES (:purpose, :cap, 0.80, 0, 'warn', :now, :now, :notes)
            ON CONFLICT(purpose) DO NOTHING
            """
    else:  # pre-0066 shape (hand-built fixture DBs)
        sql = """
            INSERT INTO llm_budgets
                (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                 created_at, updated_at, notes)
            VALUES (:purpose, :cap, 0.80, 0, :now, :now, :notes)
            ON CONFLICT(purpose) DO NOTHING
            """
    bind.execute(sa.text(sql), {"purpose": purpose, "cap": cap, "now": now, "notes": notes})


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "llm_budgets" in existing:
        bind.execute(
            sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"),
            {"purpose": _BUDGET_SEED[0]},
        )
    for name, _ddl in reversed(_TABLES):
        if name in existing:
            op.execute(sa.text(f"DROP TABLE {name}"))
