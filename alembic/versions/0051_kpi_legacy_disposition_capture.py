"""Seal noncanonical KPI disposition inputs without admitting observations."""

from __future__ import annotations

from alembic import op

revision = "0051_kpi_legacy_disposition_capture"
down_revision = "0050_kpi_fact_supersedes_lookup_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE kpi_legacy_disposition_captures (
            kpi_fact_id INTEGER PRIMARY KEY REFERENCES kpi_facts(id),
            quarantine_context_id INTEGER NOT NULL UNIQUE REFERENCES kpi_fact_semantic_contexts(id),
            user_id TEXT NOT NULL CHECK(length(trim(user_id)) BETWEEN 1 AND 128),
            payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'),
            payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
            manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
            recorded_at TEXT NOT NULL CHECK(datetime(recorded_at) IS NOT NULL)
        )
    """)
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_kpi_legacy_disposition_captures_no_{action.lower()} "
            f"BEFORE {action} ON kpi_legacy_disposition_captures BEGIN "
            "SELECT RAISE(ABORT,'legacy KPI disposition captures are append-only'); END"
        )


def downgrade() -> None:
    op.drop_table("kpi_legacy_disposition_captures")
