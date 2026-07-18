"""segment_10q_period_disambiguate -- per-purpose budget seed (docs/design/
segment_quarterly_framework.md §2.4).

The 10-Q segment quarterly extractor's LLM Stage B fallback
(``compute.segment_quarterly_10q``) resolves through ``LLM_MODELS`` (fast-
classifier tier, same as ``canonicalize_segments``). Without its own
``llm_budgets`` row it would fall back to ``__default__`` -- wrong
attribution and the wrong cap-exceeded mode. Seeded ``on_exceed='skip'``:
this is a batch pipeline (on-demand CLI in Phase 1, no cron leg yet) that
must never hard-block the whole extraction run over one ambiguous column --
a blown cap should defer just that item (per-item degrade, directives/
llm_quota_scheduling.md rule 3), not stop the CLI. $10/month bounds a
runaway (a small, cheap classifier call, not a bulk narrative generation).

Idempotent (ON CONFLICT DO NOTHING); skips when ``llm_budgets`` is absent.
Mirrors 0138/0132.

Revision ID: 0167_segment_10q_disambiguate_budget
Revises: 0166_segment_periods_provenance
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0167_segment_10q_disambiguate_budget"
down_revision: str | Sequence[str] | None = "0166_segment_periods_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURPOSE = "segment_10q_period_disambiguate"
_CAP = 10.00


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "llm_budgets" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("llm_budgets")}
    now = datetime.now(UTC).isoformat()
    notes = (
        "seeded by migration 0167 -- segment_10q_period_disambiguate (10-Q segment "
        "quarterly period-axis LLM Stage B fallback); skip mode (batch pipeline, "
        "never hard-block the whole extraction run over one ambiguous column)"
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
