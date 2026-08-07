"""sector_benchmark_proposal -- per-purpose budget seed (comparable sets
Phase 3, docs/design/comparable_sets_bottoms_up.md §4 ratification flow).

The sector-benchmark-ETF proposal generator (``compute.sector_benchmark_proposal``,
``execution/propose_sector_benchmarks.py``) resolves through ``LLM_MODELS``
(FAST/cheap classifier tier -- see the per-purpose comment in
``src/llm/cli.py``). Without its own ``llm_budgets`` row it would fall back to
``__default__`` -- wrong attribution and the wrong cap-exceeded mode. Seeded
``on_exceed='skip'`` (``hard_block=0``): this is an on-demand, owner-triggered
CLI (never a standing pipeline stage per §4's explicit ruling), so a blown cap
should just skip/defer the remaining proposals for that run, not hard-block.
$5/month bounds a runaway; the real corpus is small (~50-150 total industries,
one-time-per-industry), so a normal run costs a small fraction of that.

Idempotent (ON CONFLICT DO NOTHING); skips when ``llm_budgets`` is absent.
Mirrors 0132/0167/0174.

Note (alembic renumbering-at-rebase): this repo's head was 0178
(comp_set_metrics_locator) at authoring time; 0179 was reserved by the
parallel tenet-2 program and landed (as ``0179_v_decision_journal``,
tenet-2 Phase 5) before this PR was rebased onto main. This revision keeps
its 0180 id but repoints ``down_revision`` to 0179 per the standing
renumbering-at-rebase convention, restoring a single alembic head.

Revision ID: 0180_sector_benchmark_proposal_budget
Revises: 0179_v_decision_journal
Create Date: 2026-07-19
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0180_sector_benchmark_proposal_budget"
down_revision: str | Sequence[str] | None = "0179_v_decision_journal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURPOSE = "sector_benchmark_proposal"
_CAP = 5.00


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "llm_budgets" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("llm_budgets")}
    now = datetime.now(UTC).isoformat()
    notes = (
        "seeded by migration 0180 -- comparable sets Phase 3 sector-benchmark-ETF "
        "proposal (sector_benchmark_proposal); skip mode (on-demand owner-run CLI, "
        "never a standing pipeline stage — a blown cap defers, never hard-blocks)"
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
