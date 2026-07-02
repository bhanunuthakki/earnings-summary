"""tenet_distill — per-purpose budget seed (Worldview P2).

The Worldview distiller (``synthesis.tenet_distill``) resolves through
``LLM_MODELS`` (Sonnet pin). Without its own ``llm_budgets`` row it would fall back
to ``__default__`` — wrong attribution and the wrong cap-exceeded mode. Seeded
``on_exceed='skip'`` (``hard_block=0``): distillation is an OWNER-TAPPED, OPTIONAL
pass gated by a deterministic $0 triage, so a blown monthly cap must SKIP the run
(the standing Tenets stand), never block. $10/month bounds a runaway while covering
incremental distillation (only owner-flagged, not-yet-distilled musings call out).

Idempotent (ON CONFLICT DO NOTHING); skips when ``llm_budgets`` is absent. Mirrors
0119.

Revision ID: 0132_tenet_distill_budget
Revises: 0131_coach_pings
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0132_tenet_distill_budget"
down_revision: str | Sequence[str] | None = "0131_coach_pings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURPOSE = "tenet_distill"
_CAP = 10.00


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "llm_budgets" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("llm_budgets")}
    now = datetime.now(UTC).isoformat()
    notes = (
        "seeded by migration 0132 — Worldview distiller (tenet_distill); skip mode "
        "(owner-tapped/optional: cap hit skips the run, standing Tenets stand)"
    )
    if "on_exceed" in cols:
        sql = """
            INSERT INTO llm_budgets
                (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                 on_exceed, created_at, updated_at, notes)
            VALUES (:purpose, :cap, 0.80, 0, 'skip', :now, :now, :notes)
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
