"""Retain field-specific ETF profile capture evidence; legacy rows remain unverified."""

from __future__ import annotations

from alembic import op

revision = "0043_etf_profile_field_evidence"
down_revision = "0042_governed_ir_event_revisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE etf_profile ADD COLUMN field_evidence_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(field_evidence_json) AND json_type(field_evidence_json) = 'object')"
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .exec_driver_sql(
            "SELECT 1 FROM etf_profile WHERE json(field_evidence_json) <> '{}' LIMIT 1"
        )
        .fetchone()
    ):
        raise RuntimeError("retained ETF field evidence prevents destructive downgrade")
    op.execute("ALTER TABLE etf_profile DROP COLUMN field_evidence_json")
