"""CHECK constraints on the Personal-CIO substrate enums + an active-alert
dedup uniqueness guarantee.

Closes two data-reliability gaps from directives/regrade_memo_post_wedge.md:

  * alerts.status / alerts.trigger_kind / queued_actions.status /
    queued_actions.action_kind / user_kpi_registry.threshold_direction were
    free TEXT — the enum vocabularies were enforced only in Python, so a raw
    write or a careless future caller could persist an out-of-vocabulary value
    that the dashboard "pending" filter or the trigger dispatch silently
    mishandles. We add DB-level CHECK constraints matching the canonical
    vocabularies (the ALERT_STATUS_* / ACTION_STATUS_* constants in
    src/alerts/store.py, the dashboard's five recognized trigger kinds in
    src/dashboard/evidence_drawer.py, and kpi_polarity.adverse_direction).

  * Alert re-fire dedup was an ADVISORY index (ix_alerts_user_signature) read by
    a race-prone SELECT-before-INSERT. We add a PARTIAL UNIQUE index so the DB
    itself guarantees at most one ACTIVE (non-expired) alert per
    (user_id, signature_sha) — matching find_by_signature's "most recent
    non-expired" dedup contract (an expired alert may legitimately re-fire, so
    the index is scoped to status <> 'expired').

threshold_direction stays nullable (a tracked KPI may carry no threshold), so
its CHECK is "NULL OR ('above','below')".

SQLite cannot ALTER TABLE ADD CONSTRAINT, so the CHECKs ride on a
batch_alter_table recreation (rename → create-with-constraint → copy → drop).
The migration is a no-op on a DB that predates the substrate tables.

Revision ID: 0068_substrate_constraints
Revises: 0067_ticker_settings
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0068_substrate_constraints"
down_revision: str | Sequence[str] | None = "0067_ticker_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Canonical enum vocabularies — keep in lockstep with src/alerts/store.py
# (ALERT_STATUS_* / ACTION_STATUS_*), the trigger `kind` classvars, and
# kpi_polarity.adverse_direction.
_ALERT_STATUS = "('pending', 'approved', 'dismissed', 'expired')"
# Five conceived trigger kinds (the dashboard's own vocabulary —
# src/dashboard/evidence_drawer.py). 'thesis_drift' was folded into the
# kpi_inflection sensor rather than shipped as its own trigger, but it remains a
# recognized alert kind the feed/drawer render, so the constraint admits it.
_TRIGGER_KIND = "('kpi_inflection', 'earnings_tone', 'saydo_due', 'thesis_drift', 'material_news')"
_ACTION_STATUS = "('pending', 'applied', 'cancelled')"
_ACTION_KIND = "('thesis_update', 'bear_append', 'sizing_update', 'earnings_prep_append')"


def _has_table(inspector: sa.Inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _has_index(inspector: sa.Inspector, table: str, name: str) -> bool:
    return any(ix["name"] == name for ix in inspector.get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if _has_table(insp, "alerts"):
        with op.batch_alter_table("alerts") as batch:
            batch.create_check_constraint("ck_alerts_status", f"status IN {_ALERT_STATUS}")
            batch.create_check_constraint(
                "ck_alerts_trigger_kind", f"trigger_kind IN {_TRIGGER_KIND}"
            )
        if not _has_index(insp, "alerts", "uq_alerts_active_signature"):
            op.create_index(
                "uq_alerts_active_signature",
                "alerts",
                ["user_id", "signature_sha"],
                unique=True,
                sqlite_where=sa.text("status <> 'expired'"),
            )

    if _has_table(insp, "queued_actions"):
        with op.batch_alter_table("queued_actions") as batch:
            batch.create_check_constraint("ck_queued_actions_status", f"status IN {_ACTION_STATUS}")
            batch.create_check_constraint(
                "ck_queued_actions_action_kind", f"action_kind IN {_ACTION_KIND}"
            )

    if _has_table(insp, "user_kpi_registry"):
        with op.batch_alter_table("user_kpi_registry") as batch:
            batch.create_check_constraint(
                "ck_user_kpi_registry_direction",
                "threshold_direction IS NULL OR threshold_direction IN ('above', 'below')",
            )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if _has_table(insp, "user_kpi_registry"):
        with op.batch_alter_table("user_kpi_registry") as batch:
            batch.drop_constraint("ck_user_kpi_registry_direction", type_="check")

    if _has_table(insp, "queued_actions"):
        with op.batch_alter_table("queued_actions") as batch:
            batch.drop_constraint("ck_queued_actions_action_kind", type_="check")
            batch.drop_constraint("ck_queued_actions_status", type_="check")

    if _has_table(insp, "alerts"):
        if _has_index(insp, "alerts", "uq_alerts_active_signature"):
            op.drop_index("uq_alerts_active_signature", table_name="alerts")
        with op.batch_alter_table("alerts") as batch:
            batch.drop_constraint("ck_alerts_trigger_kind", type_="check")
            batch.drop_constraint("ck_alerts_status", type_="check")
