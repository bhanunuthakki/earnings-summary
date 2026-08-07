"""Widen ck_alerts_trigger_kind to admit 'owner_capacity_breach' (tenet-2 Phase 3).

The governor's new ``capacity_breach`` moment class (human-capital cap or
cash-floor policy crossed, read from AFFIRMED ``owner_profile_facts`` rows)
writes its own ``alerts`` row so the SAME tier-1 severity/ranking path
(``dashboard.inbox_rank.decisive_alert_reason``) treats an owner-policy
breach exactly like an owner-falsifier breach — mirroring how 0086 admitted
'decision_condition' and 0108 admitted 'restatement'. Keep in lockstep with
``alerts.store.TRIGGER_KINDS``.

No new table — purely the constraint widening, batch-recreated per 0077/0086.

Revision ID: 0171_owner_capacity_breach_trigger_kind
Revises: 0170_comparable_sets
Create Date: 2026-07-17
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0171_owner_capacity_breach_trigger_kind"
down_revision: str | Sequence[str] | None = "0170_comparable_sets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirror of alerts.store.TRIGGER_KINDS after this migration.
_TRIGGER_KIND_WIDENED = (
    "('kpi_inflection', 'earnings_tone', 'saydo_due', 'thesis_drift', "
    "'material_news', 'decision_condition', 'restatement', 'owner_capacity_breach')"
)
_TRIGGER_KIND_PRIOR = (
    "('kpi_inflection', 'earnings_tone', 'saydo_due', 'thesis_drift', "
    "'material_news', 'decision_condition', 'restatement')"
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
    # Restore the narrower CHECK only when no owner_capacity_breach alerts
    # exist — otherwise the recreate would fail row validation; the widened
    # constraint stays (downgrade is never destructive).
    existing = bind.execute(
        sa.text("SELECT COUNT(*) FROM alerts WHERE trigger_kind = 'owner_capacity_breach'")
    ).scalar()
    if existing:
        return
    with op.batch_alter_table("alerts") as batch:
        with contextlib.suppress(ValueError, sa.exc.OperationalError):
            batch.drop_constraint("ck_alerts_trigger_kind", type_="check")
        batch.create_check_constraint(
            "ck_alerts_trigger_kind", f"trigger_kind IN {_TRIGGER_KIND_PRIOR}"
        )
