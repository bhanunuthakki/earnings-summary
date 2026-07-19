"""Widen ck_alerts_trigger_kind to admit 'data_feed_stale' (program review 2026-07-19).

The review found two data feeds silently dead for weeks with no owner-visible
signal: the news pipeline (stage_0_news killed daily at its budget, news table
frozen at 2026-07-03 while ~$14/day of Opus output was discarded) and the macro
series fetch (every FMP series 429-rate-limited, "populated": 0 logged daily,
betas recomputed off frozen series). 'data_feed_stale' is the dead-man switch
for that class: a feed job that produced nothing (or whose freshest row is
older than its cadence tolerates) fires ONE book-level alert row (sentinel
ticker 'PORTFOLIO', the 0171 capacity-breach convention) through the same
sensor contract as every other trigger, so it surfaces in the alert feed the
owner actually reads instead of a cron log nobody opens.

No new table — purely the constraint widening, batch-recreated per 0077/0086/
0108/0171. Keep in lockstep with ``alerts.store.TRIGGER_KINDS``.

Revision ID: 0183_data_feed_stale_trigger_kind
Revises: 0182_dcf_runs_sanity_flag
Create Date: 2026-07-19
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0183_data_feed_stale_trigger_kind"
down_revision: str | Sequence[str] | None = "0182_dcf_runs_sanity_flag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirror of alerts.store.TRIGGER_KINDS after this migration.
_TRIGGER_KIND_WIDENED = (
    "('kpi_inflection', 'earnings_tone', 'saydo_due', 'thesis_drift', "
    "'material_news', 'decision_condition', 'restatement', 'owner_capacity_breach', "
    "'data_feed_stale')"
)
_TRIGGER_KIND_PRIOR = (
    "('kpi_inflection', 'earnings_tone', 'saydo_due', 'thesis_drift', "
    "'material_news', 'decision_condition', 'restatement', 'owner_capacity_breach')"
)


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in insp.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "alerts"):
        return
    with op.batch_alter_table("alerts") as batch:
        with contextlib.suppress(ValueError, sa.exc.OperationalError):  # already absent
            batch.drop_constraint("ck_alerts_trigger_kind", type_="check")
        batch.create_check_constraint(
            "ck_alerts_trigger_kind", f"trigger_kind IN {_TRIGGER_KIND_WIDENED}"
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "alerts"):
        return
    # Restore the narrower CHECK only when no data_feed_stale alerts exist —
    # otherwise the recreate would fail row validation; the widened constraint
    # stays (downgrade is never destructive).
    existing = bind.execute(
        sa.text("SELECT COUNT(*) FROM alerts WHERE trigger_kind = 'data_feed_stale'")
    ).scalar()
    if existing:
        return
    with op.batch_alter_table("alerts") as batch:
        with contextlib.suppress(ValueError, sa.exc.OperationalError):
            batch.drop_constraint("ck_alerts_trigger_kind", type_="check")
        batch.create_check_constraint(
            "ck_alerts_trigger_kind", f"trigger_kind IN {_TRIGGER_KIND_PRIOR}"
        )
