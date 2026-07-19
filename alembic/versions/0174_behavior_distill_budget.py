"""behavior_distill -- per-purpose budget seed (tenet-2 Phase 4, docs/design/
tenet2_advisory_program.md §3.2 Tier C / §7 ruling 3).

The behavioral-rules distiller (``synthesis.behavior_distill``) resolves
through ``LLM_MODELS`` (FAST/cheap classifier tier -- see the per-purpose
comment in ``src/llm/cli.py``). Without its own ``llm_budgets`` row it would
fall back to ``__default__`` -- wrong attribution and the wrong cap-exceeded
mode. Seeded ``on_exceed='skip'`` (``hard_block=0``), mirroring 0132
(tenet_distill) exactly: this is a batch distill run on a cadence (post-grading,
weekly) or on demand, gated by the deterministic "any newly-graded decisions?"
triage -- a blown monthly cap must SKIP the run (the standing behavioral facts
stand, nothing regresses to the frozen seed defaults), never hard-block.
$10/month bounds a runaway; the real corpus is small (a few dozen graded
decisions), so a normal run costs a small fraction of that.

Idempotent (ON CONFLICT DO NOTHING); skips when ``llm_budgets`` is absent.
Mirrors 0132/0167.

Note (alembic renumbering-at-rebase, docs/design/tenet2_advisory_program.md
§6.4): this program (tenet-2 Phase 4, wave D) claims 0174; the parallel wave-D
tenet-1 track (comparable sets P2 / metrics engine P3) is expected to claim
0172-0173. At authoring time neither had landed yet, so ``down_revision``
below repoints to the actual head (0171) -- repoint again at rebase if 0172/
0173 landed in the meantime, per the standing gotcha.

Revision ID: 0174_behavior_distill_budget
Revises: 0171_owner_capacity_breach_trigger_kind
Create Date: 2026-07-18
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0174_behavior_distill_budget"
down_revision: str | Sequence[str] | None = "0171_owner_capacity_breach_trigger_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURPOSE = "behavior_distill"
_CAP = 10.00


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "llm_budgets" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("llm_budgets")}
    now = datetime.now(UTC).isoformat()
    notes = (
        "seeded by migration 0174 -- tenet-2 Phase 4 behavioral-rules distiller "
        "(behavior_distill); skip mode (batch/cadence pass gated by a deterministic "
        "triage: cap hit skips the run, standing behavioral facts stand)"
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
