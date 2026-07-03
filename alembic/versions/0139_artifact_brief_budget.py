"""artifact_brief — per-purpose budget seed (Ledger artifact brief).

The artifact brief (``research.brief``) resolves through ``LLM_MODELS`` (Sonnet pin).
Without its own ``llm_budgets`` row it would fall back to ``__default__`` — wrong
attribution and the wrong cap-exceeded mode. Seeded ``on_exceed='warn'``
(``hard_block=0``): it runs auto on capture (the owner chose auto-on-capture), so a
blown cap should log + proceed rather than silently drop briefs; at single-user
volume a Sonnet pass over one fetched article is ~cents, so a soft overspend is
bounded while the warn surfaces any runaway (the ``LEDGER_ARTIFACT_BRIEF=0`` kill
switch is the hard stop). $15/month bounds it.

Idempotent (ON CONFLICT DO NOTHING); skips when ``llm_budgets`` is absent. Mirrors
0138.

Revision ID: 0139_artifact_brief_budget
Revises: 0138_capture_intent_budget
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0139_artifact_brief_budget"
down_revision: str | Sequence[str] | None = "0138_capture_intent_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURPOSE = "artifact_brief"
_CAP = 15.00


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "llm_budgets" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("llm_budgets")}
    now = datetime.now(UTC).isoformat()
    notes = (
        "seeded by migration 0139 — Ledger artifact brief (artifact_brief); warn mode "
        "(auto on capture: a blown cap logs + proceeds; LEDGER_ARTIFACT_BRIEF=0 is the kill switch)"
    )
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
    bind.execute(sa.text(sql), {"purpose": _PURPOSE, "cap": _CAP, "now": now, "notes": notes})


def downgrade() -> None:
    bind = op.get_bind()
    if "llm_budgets" not in set(sa.inspect(bind).get_table_names()):
        return
    bind.execute(sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"), {"purpose": _PURPOSE})
