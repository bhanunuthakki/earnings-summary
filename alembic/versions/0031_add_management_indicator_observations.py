"""Stage novel management indicators outside canonical KPI series.

Revision ID: 0031_add_management_indicator_observations
Revises: 0030_add_kpi_fact_semantic_contexts
"""

from __future__ import annotations

from alembic import op

revision = "0031_add_management_indicator_observations"
down_revision = "0030_add_kpi_fact_semantic_contexts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE management_indicator_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL UNIQUE,
            ticker TEXT NOT NULL,
            transcript_segment_id INTEGER NOT NULL REFERENCES transcript_segments(id),
            source_doc_id INTEGER NOT NULL REFERENCES documents(id),
            raw_label TEXT NOT NULL CHECK(length(trim(raw_label)) > 0),
            value TEXT NOT NULL,
            unit TEXT NOT NULL CHECK(unit IN ('actual','thousands','millions','billions','percent','ratio','bps','count')),
            scope TEXT NOT NULL CHECK(scope IN ('consolidated','segment','product','geography','unspecified')),
            speaker TEXT,
            source_excerpt TEXT NOT NULL CHECK(length(trim(source_excerpt)) > 0),
            source_locator_json TEXT NOT NULL CHECK(json_valid(source_locator_json) AND json_type(source_locator_json)='object'),
            recurrence TEXT NOT NULL CHECK(recurrence IN ('recurring','one_off','unknown')),
            promotion_status TEXT NOT NULL DEFAULT 'pending_review'
                CHECK(promotion_status IN ('pending_review','promoted','rejected')),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            reviewed_at TEXT,
            reviewed_by TEXT,
            CHECK((promotion_status='pending_review' AND reviewed_at IS NULL AND reviewed_by IS NULL)
               OR (promotion_status IN ('promoted','rejected') AND reviewed_at IS NOT NULL
                   AND reviewed_by IS NOT NULL AND length(trim(reviewed_by)) > 0))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_management_indicator_review ON management_indicator_observations "
        "(ticker,promotion_status,recurrence,source_doc_id)"
    )
    op.execute(
        "CREATE TRIGGER trg_management_indicator_source_immutable BEFORE UPDATE ON "
        "management_indicator_observations WHEN "
        "NEW.idempotency_key IS NOT OLD.idempotency_key OR NEW.ticker IS NOT OLD.ticker OR "
        "NEW.transcript_segment_id IS NOT OLD.transcript_segment_id OR "
        "NEW.source_doc_id IS NOT OLD.source_doc_id OR NEW.raw_label IS NOT OLD.raw_label OR "
        "NEW.value IS NOT OLD.value OR NEW.unit IS NOT OLD.unit OR NEW.scope IS NOT OLD.scope OR "
        "NEW.speaker IS NOT OLD.speaker OR NEW.source_excerpt IS NOT OLD.source_excerpt OR "
        "NEW.source_locator_json IS NOT OLD.source_locator_json OR "
        "NEW.recurrence IS NOT OLD.recurrence OR NEW.created_at IS NOT OLD.created_at "
        "BEGIN SELECT RAISE(ABORT, 'Management indicator source observations are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_management_indicator_review_once BEFORE UPDATE ON "
        "management_indicator_observations WHEN OLD.promotion_status<>'pending_review' "
        "BEGIN SELECT RAISE(ABORT, 'Management indicator review is already final'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_management_indicator_no_delete BEFORE DELETE ON "
        "management_indicator_observations BEGIN SELECT RAISE(ABORT, "
        "'Management indicator observations are append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_management_indicator_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_management_indicator_review_once")
    op.execute("DROP TRIGGER IF EXISTS trg_management_indicator_source_immutable")
    op.execute("DROP INDEX IF EXISTS ix_management_indicator_review")
    op.execute("DROP TABLE IF EXISTS management_indicator_observations")
