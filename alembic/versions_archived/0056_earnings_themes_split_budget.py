"""earnings_themes_split — per-purpose budget seed for §5 themes extractor.

Phase 2c §5 adds an LLM call that rolls themes across the last 4 transcripts
split into prepared-remarks vs Q&A buckets. The Phase 0 budget enforcer
(migration 0052) needs a cap row for this purpose; without one it falls back
to the '__default__' budget which is shared with every other unregistered
purpose and gives no visibility into themes-split spend.

Idempotent: INSERT ... ON CONFLICT DO NOTHING, so re-running on a DB that
already has the row is a no-op. Skips silently when llm_budgets doesn't
exist yet (extremely old branches that haven't picked up 0052 yet).

Revision ID: 0056_earnings_themes_split_budget
Revises: 0055_segment_junction
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0056_earnings_themes_split_budget"
down_revision: str | Sequence[str] | None = "0055_segment_junction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PURPOSE = "earnings_themes_split"
# Cross-quarter 4-transcript prompt, run once per ticker per refresh. 11
# tracked tickers x ~4 refreshes/month ~= 40 calls/month. Sized in line with
# pairwise_analysis ($30) given similar input shape and per-call cost.
_MONTHLY_CAP_USD = 30.00


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "llm_budgets" not in existing:
        return
    now = datetime.now(UTC).isoformat()
    bind.execute(
        sa.text(
            """
            INSERT INTO llm_budgets
                (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                 created_at, updated_at, notes)
            VALUES (:purpose, :cap, 0.80, 0, :now, :now, :notes)
            ON CONFLICT(purpose) DO NOTHING
            """
        ),
        {
            "purpose": _PURPOSE,
            "cap": _MONTHLY_CAP_USD,
            "now": now,
            "notes": "seeded by migration 0056 — Phase 2c §5 themes split",
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "llm_budgets" not in existing:
        return
    bind.execute(
        sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"),
        {"purpose": _PURPOSE},
    )
