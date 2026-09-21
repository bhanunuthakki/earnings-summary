"""Persist immutable terminal FMP recovery receipts separately from events."""

from __future__ import annotations

from alembic import op

revision = "0045_fmp_recovery_receipts"
down_revision = "0044_position_entry_supersession"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE fmp_refresh_receipts (
        run_id TEXT PRIMARY KEY CHECK(length(run_id)>0),
        receipt_id TEXT NOT NULL UNIQUE CHECK(length(receipt_id)=64),
        recorded_at TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64)
    )""")
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_fmp_refresh_receipts_no_{action.lower()} BEFORE {action} ON fmp_refresh_receipts BEGIN SELECT RAISE(ABORT,'FMP final receipts are immutable'); END"
        )


def downgrade() -> None:
    if op.get_bind().exec_driver_sql("SELECT 1 FROM fmp_refresh_receipts LIMIT 1").fetchone():
        raise RuntimeError("retained FMP recovery receipts prevent destructive downgrade")
    op.execute("DROP TABLE fmp_refresh_receipts")
