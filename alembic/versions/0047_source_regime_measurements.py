"""Retain measured source attempts beside existing source_calls.

Revision ID: 0047_source_regime_measurements
Revises: 0046_sec_execution_receipts (isolated test parent; integrate after0046)
"""

from __future__ import annotations

from alembic import op

revision = "0047_source_regime_measurements"
down_revision = "0046_sec_execution_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE source_regime_measurements (
        measurement_id TEXT PRIMARY KEY,
        source_call_id INTEGER NOT NULL UNIQUE REFERENCES source_calls(id),
        run_id TEXT NOT NULL,
        regime TEXT CHECK(regime IN ('official_primary','normalized_vendor_only','combined')),
        provider TEXT NOT NULL,
        measured_at TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'),
        payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
        CHECK(json_extract(payload_json,'$.measurement_id') IS measurement_id),
        CHECK(json_extract(payload_json,'$.run_id') IS run_id),
        CHECK(json_extract(payload_json,'$.provider') IS provider),
        CHECK(json_extract(payload_json,'$.regime') IS regime)
    )""")
    op.execute(
        "CREATE INDEX ix_source_regime_measurement_run ON source_regime_measurements(run_id,measured_at)"
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER source_regime_measurements_no_{operation.lower()} BEFORE {operation} ON source_regime_measurements BEGIN SELECT RAISE(ABORT,'source measurements are append-only'); END"
        )


def downgrade() -> None:
    if op.get_bind().exec_driver_sql("SELECT 1 FROM source_regime_measurements LIMIT 1").fetchone():
        raise RuntimeError("refusing to discard retained source measurements")
    op.drop_table("source_regime_measurements")
