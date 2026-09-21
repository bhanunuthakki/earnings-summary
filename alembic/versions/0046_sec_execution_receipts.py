"""Preserve actual SEC execution attempts without inventing historical work."""

from __future__ import annotations

from alembic import op

revision = "0046_sec_execution_receipts"
down_revision = "0045_fmp_recovery_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE sec_execution_receipts (
        attempt_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK(sequence BETWEEN 0 AND 2),
        state TEXT NOT NULL CHECK(state IN ('requested','running','succeeded','partial','deferred','failed')),
        recorded_at TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
        PRIMARY KEY(attempt_id,sequence)
    )""")
    op.execute(
        "CREATE INDEX ix_sec_execution_request ON sec_execution_receipts(request_id,sequence)"
    )
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_sec_execution_no_{action.lower()} BEFORE {action} ON sec_execution_receipts BEGIN SELECT RAISE(ABORT,'SEC execution receipts are immutable'); END"
        )


def downgrade() -> None:
    if op.get_bind().exec_driver_sql("SELECT 1 FROM sec_execution_receipts LIMIT 1").fetchone():
        raise RuntimeError("retained SEC execution receipts prevent destructive downgrade")
    op.execute("DROP TABLE sec_execution_receipts")
