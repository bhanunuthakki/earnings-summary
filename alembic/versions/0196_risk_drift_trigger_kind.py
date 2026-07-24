"""Widen ck_alerts_trigger_kind to admit 'risk_drift' (Workstream C8).

Program plan (2026-07-19, Workstream C8): ``portfolio_risk_snapshot_history``
(0185/0186) is append-only, but nothing read it back for "did my book's risk
posture drift" until now. This migration does two additive things:

1. Widens the ``alerts.trigger_kind`` CHECK the same way 0183 did for
   'data_feed_stale' — batch-recreated per 0077/0086/0108/0171/0183 — so
   ``src/triggers/risk_drift.py`` can fire book-level 'risk_drift' alerts
   (sentinel ticker 'PORTFOLIO', the same convention 0171/0183 established;
   ``alerts.ticker`` is NOT NULL, so a book-level alert can never literally use
   ``ticker=NULL``).
2. Adds ONE additive, nullable ``factor_vector_json`` column to
   ``portfolio_risk_snapshot_history`` so the authoritative risk writer
   (``execution/refresh_portfolio_risk_snapshot.py``) can stamp the current C3
   book-level business-factor vector (``risk_factors.book_factor_vector``)
   onto each capture — the drift sensor's baseline then covers the factor
   legs alongside spy_beta/growth_tilt/concentration. No existing column
   changes shape or name; a pre-0197 read (``portfolio_risk_snapshot_store.
   read_history``, which explicitly SELECTs its own column list) is
   unaffected by the new column's presence.

Keep the trigger_kind set in lockstep with ``alerts.store.TRIGGER_KINDS``.

Revision ID: 0196_risk_drift_trigger_kind
Revises: 0195_business_factor_exposures
Create Date: 2026-07-24


# orchestrator renumbers both this file's revision id AND down_revision at
# push time to slot in after the true current alembic head (other Workstream
# C branches may claim 0196+ numbers first). Do not treat 0196 as a verified
# parent.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0196_risk_drift_trigger_kind"

down_revision: str | Sequence[str] | None = "0195_business_factor_exposures"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALERTS_TABLE = "alerts"
_HISTORY_TABLE = "portfolio_risk_snapshot_history"
_FACTOR_COL = "factor_vector_json"

# Mirror of alerts.store.TRIGGER_KINDS after this migration.
_TRIGGER_KIND_WIDENED = (
    "('kpi_inflection', 'earnings_tone', 'saydo_due', 'thesis_drift', "
    "'material_news', 'decision_condition', 'restatement', 'owner_capacity_breach', "
    "'data_feed_stale', 'risk_drift')"
)
_TRIGGER_KIND_PRIOR = (
    "('kpi_inflection', 'earnings_tone', 'saydo_due', 'thesis_drift', "
    "'material_news', 'decision_condition', 'restatement', 'owner_capacity_breach', "
    "'data_feed_stale')"
)


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in insp.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if _has_table(insp, _ALERTS_TABLE):
        with op.batch_alter_table(_ALERTS_TABLE) as batch:
            with contextlib.suppress(ValueError, sa.exc.OperationalError):  # already absent
                batch.drop_constraint("ck_alerts_trigger_kind", type_="check")
            batch.create_check_constraint(
                "ck_alerts_trigger_kind", f"trigger_kind IN {_TRIGGER_KIND_WIDENED}"
            )

    if _has_table(insp, _HISTORY_TABLE):
        cols = {c["name"] for c in insp.get_columns(_HISTORY_TABLE)}
        if _FACTOR_COL not in cols:
            op.add_column(_HISTORY_TABLE, sa.Column(_FACTOR_COL, sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if _has_table(insp, _ALERTS_TABLE):
        # Restore the narrower CHECK only when no risk_drift alerts exist —
        # otherwise the recreate would fail row validation; the widened
        # constraint stays (downgrade is never destructive), matching 0183.
        existing = bind.execute(
            sa.text("SELECT COUNT(*) FROM alerts WHERE trigger_kind = 'risk_drift'")
        ).scalar()
        if not existing:
            with op.batch_alter_table(_ALERTS_TABLE) as batch:
                with contextlib.suppress(ValueError, sa.exc.OperationalError):
                    batch.drop_constraint("ck_alerts_trigger_kind", type_="check")
                batch.create_check_constraint(
                    "ck_alerts_trigger_kind", f"trigger_kind IN {_TRIGGER_KIND_PRIOR}"
                )

    if _has_table(insp, _HISTORY_TABLE):
        cols = {c["name"] for c in insp.get_columns(_HISTORY_TABLE)}
        if _FACTOR_COL in cols:
            op.drop_column(_HISTORY_TABLE, _FACTOR_COL)
