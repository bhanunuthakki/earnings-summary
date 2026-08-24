"""Persist idempotent, auditable state for the dark price-action sensor.

Revision ID: 0023_add_price_action_sensor_state
Revises: 0022_add_governed_alert_action_receipts
"""

from __future__ import annotations

from alembic import op

revision = "0023_add_price_action_sensor_state"
down_revision = "0022_add_governed_alert_action_receipts"
branch_labels = None
depends_on = None

_TRIGGER_KINDS = (
    "'kpi_inflection','earnings_tone','saydo_due','thesis_drift','material_news',"
    "'decision_condition','restatement','owner_capacity_breach','data_feed_stale',"
    "'risk_drift','model_pin_switch','price_action'"
)


def upgrade() -> None:
    # SQLite needs a table rebuild to widen a CHECK constraint.
    with op.batch_alter_table("alerts", recreate="always") as batch:
        batch.drop_constraint("ck_alerts_trigger_kind", type_="check")
        batch.create_check_constraint(
            "ck_alerts_trigger_kind", f"trigger_kind IN ({_TRIGGER_KINDS})"
        )
    op.execute(
        """
        CREATE TABLE price_action_sensor_state (
            user_id TEXT NOT NULL REFERENCES tenants(id),
            ticker TEXT NOT NULL,
            ladder_id TEXT NOT NULL,
            ladder_revision_sha256 TEXT NOT NULL,
            rung_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('add','trim','sell')),
            trigger_side TEXT NOT NULL CHECK(trigger_side IN ('at_or_below','at_or_above')),
            phase TEXT NOT NULL CHECK(phase IN ('clear','approaching','breached')),
            generation INTEGER NOT NULL CHECK(generation >= 0),
            last_price REAL NOT NULL CHECK(last_price > 0),
            last_observed_at TEXT NOT NULL,
            last_source_ref TEXT NOT NULL,
            last_source_sha256 TEXT NOT NULL CHECK(length(last_source_sha256)=64),
            phase_entered_at TEXT NOT NULL,
            last_approaching_alert_id INTEGER REFERENCES alerts(id),
            last_breached_alert_id INTEGER REFERENCES alerts(id),
            rearmed_at TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(user_id,ticker,ladder_id,ladder_revision_sha256,rung_id),
            CHECK(length(trim(ticker)) > 0),
            CHECK(length(trim(ladder_id)) > 0),
            CHECK(length(ladder_revision_sha256)=64),
            CHECK(length(trim(rung_id)) > 0)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_price_action_sensor_state_ticker ON "
        "price_action_sensor_state(user_id,ticker,updated_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE price_action_sensor_events (
            event_key TEXT PRIMARY KEY NOT NULL,
            user_id TEXT NOT NULL REFERENCES tenants(id),
            ticker TEXT NOT NULL,
            ladder_id TEXT NOT NULL,
            ladder_revision_sha256 TEXT NOT NULL,
            rung_id TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK(generation >= 0),
            transition TEXT NOT NULL CHECK(transition IN ('approaching','breached','rearmed')),
            observed_at TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
            alert_id INTEGER REFERENCES alerts(id),
            evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
            created_at TEXT NOT NULL,
            UNIQUE(user_id,ticker,ladder_id,ladder_revision_sha256,rung_id,generation,transition),
            CHECK(length(event_key)=64),
            CHECK(length(ladder_revision_sha256)=64)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_price_action_sensor_events_rung ON "
        "price_action_sensor_events(user_id,ticker,rung_id,created_at DESC)"
    )
    op.execute(
        "CREATE TRIGGER trg_price_action_sensor_events_no_update BEFORE UPDATE ON "
        "price_action_sensor_events BEGIN SELECT RAISE(ABORT, 'price action sensor events append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_price_action_sensor_events_no_delete BEFORE DELETE ON "
        "price_action_sensor_events BEGIN SELECT RAISE(ABORT, 'price action sensor events append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_price_action_sensor_events_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_price_action_sensor_events_no_update")
    op.execute("DROP INDEX IF EXISTS ix_price_action_sensor_events_rung")
    op.execute("DROP TABLE IF EXISTS price_action_sensor_events")
    op.execute("DROP INDEX IF EXISTS ix_price_action_sensor_state_ticker")
    op.execute("DROP TABLE IF EXISTS price_action_sensor_state")
    with op.batch_alter_table("alerts", recreate="always") as batch:
        batch.drop_constraint("ck_alerts_trigger_kind", type_="check")
        batch.create_check_constraint(
            "ck_alerts_trigger_kind",
            "trigger_kind IN (" + _TRIGGER_KINDS.replace(",'price_action'", "") + ")",
        )
