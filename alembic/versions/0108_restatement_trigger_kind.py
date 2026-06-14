"""Widen ck_alerts_trigger_kind to admit 'restatement' (Close-the-Loops L10 PR2).

A supersede — a later filing materially correcting a number you hold — was the
most decision-relevant provenance event the platform never pushed: the owner
only learned a held-name figure changed by opening the passive Restatements
panel. ``src/triggers/restatement.py`` turns it into a fired alert, so this
widens the ``alerts.trigger_kind`` CHECK to accept the new kind (mirroring how
0086 admitted 'decision_condition'). Keep in lockstep with
``alerts.store.TRIGGER_KINDS``.

No new table — purely the constraint widening, batch-recreated per 0077/0086.

Revision ID: 0108_restatement_trigger_kind
Revises: 0107_standup_messages
Create Date: 2026-06-14
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0108_restatement_trigger_kind"
down_revision: str | Sequence[str] | None = "0107_standup_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirror of alerts.store.TRIGGER_KINDS after this migration.
_TRIGGER_KIND_WIDENED = (
    "('kpi_inflection', 'earnings_tone', 'saydo_due', 'thesis_drift', "
    "'material_news', 'decision_condition', 'restatement')"
)
_TRIGGER_KIND_PRIOR = (
    "('kpi_inflection', 'earnings_tone', 'saydo_due', 'thesis_drift', "
    "'material_news', 'decision_condition')"
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
    # Restore the narrower CHECK only when no restatement alerts exist — otherwise
    # the recreate would fail row validation; the widened constraint stays
    # (downgrade is never destructive).
    existing = bind.execute(
        sa.text("SELECT COUNT(*) FROM alerts WHERE trigger_kind = 'restatement'")
    ).scalar()
    if existing:
        return
    with op.batch_alter_table("alerts") as batch:
        with contextlib.suppress(ValueError, sa.exc.OperationalError):
            batch.drop_constraint("ck_alerts_trigger_kind", type_="check")
        batch.create_check_constraint(
            "ck_alerts_trigger_kind", f"trigger_kind IN {_TRIGGER_KIND_PRIOR}"
        )
