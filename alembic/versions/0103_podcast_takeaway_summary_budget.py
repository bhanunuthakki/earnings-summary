"""podcast_takeaway_summary — per-purpose budget seed (S11 DIET leg).

The daily podcast-RSS poller (execution/fetch_podcast_rss.py) writes
``media_appearance`` signals with whatever copy is in the RSS description.
A nightly Sonnet pass (execution/summarize_podcast_episodes.py →
signals.takeaway.summarize_pending) replaces short/absent bodies with a
2-4 sentence investment-relevant briefing. Without its own ``llm_budgets``
row the purpose would fall back to '__default__' — wrong attribution and
the wrong cap-exceeded mode.

Seeded ``on_exceed='skip'`` so a blown monthly cap leaves existing bodies
as-is (the next calendar month the summarizer resumes). $5/month: one
Sonnet call per episode at ~$0.01 each covers ~500 summarizations/month,
well above the expected ~30-90 (7 shows × weekly cadence).

Idempotent: INSERT ... ON CONFLICT DO NOTHING. Silently skips when
``llm_budgets`` is absent (fixture DBs that stamp before 0052).

Revision ID: 0103_podcast_takeaway_summary_budget
Revises: 0102_signals_media_appearance
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0103_podcast_takeaway_summary_budget"
down_revision: str | Sequence[str] | None = "0102_signals_media_appearance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PURPOSE = "podcast_takeaway_summary"
# Sonnet call per episode (~500 tokens prompt, ~200 token output) ≈ $0.01;
# $5/month covers ~500 episodes while bounding a runaway.
_MONTHLY_CAP_USD = 5.00


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "llm_budgets" not in existing:
        return
    cols = {c["name"] for c in sa.inspect(bind).get_columns("llm_budgets")}
    now = datetime.now(UTC).isoformat()
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
    bind.execute(
        sa.text(sql),
        {
            "purpose": _PURPOSE,
            "cap": _MONTHLY_CAP_USD,
            "now": now,
            "notes": "seeded by migration 0103 — S11 podcast takeaway summarizer (skip "
            "mode: cap hit leaves RSS body as-is; summarizer resumes next month)",
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
