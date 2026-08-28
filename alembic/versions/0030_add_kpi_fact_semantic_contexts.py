"""Preserve source-bound KPI meaning before canonical projection.

Revision ID: 0030_add_kpi_fact_semantic_contexts
Revises: 0029_retire_podcast_prototype
"""

from __future__ import annotations

from alembic import op

revision = "0030_add_kpi_fact_semantic_contexts"
down_revision = "0029_retire_podcast_prototype"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE kpi_fact_semantic_contexts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kpi_fact_id INTEGER NOT NULL REFERENCES kpi_facts(id),
            revision INTEGER NOT NULL CHECK(revision > 0),
            supersedes_context_id INTEGER UNIQUE REFERENCES kpi_fact_semantic_contexts(id),
            metric_name_as_reported TEXT NOT NULL CHECK(length(trim(metric_name_as_reported)) > 0),
            reported_period_end TEXT CHECK(reported_period_end IS NULL OR date(reported_period_end) IS NOT NULL),
            period_role TEXT NOT NULL CHECK(period_role IN ('current','prior_period_comparator','prior_year_comparator','guidance','unknown')),
            publication_lane TEXT NOT NULL CHECK(publication_lane IN ('current_actual','comparator','guidance_target','management_explanation','analyst_question','unclassified')),
            accounting_basis TEXT NOT NULL CHECK(accounting_basis IN ('gaap','non_gaap','management','unknown')),
            consolidation_scope TEXT NOT NULL CHECK(consolidation_scope IN ('consolidated','geography','segment','product','other','unknown')),
            dimensions_json TEXT NOT NULL CHECK(json_valid(dimensions_json) AND json_type(dimensions_json)='object'),
            unit_scale TEXT NOT NULL CHECK(unit_scale IN ('none','thousands','millions','billions','unknown')),
            source_row_label TEXT,
            source_column_header TEXT,
            source_value_text TEXT CHECK(source_value_text IS NULL OR length(trim(source_value_text)) > 0),
            status TEXT NOT NULL CHECK(status IN ('admitted','quarantined','legacy_unknown')),
            reason_code TEXT,
            reviewed_by TEXT NOT NULL CHECK(length(trim(reviewed_by)) > 0),
            knowledge_at TEXT NOT NULL CHECK(length(trim(knowledge_at)) > 0),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE(kpi_fact_id, revision),
            CHECK((revision=1 AND supersedes_context_id IS NULL)
               OR (revision>1 AND supersedes_context_id IS NOT NULL)),
            CHECK((status='admitted' AND reported_period_end IS NOT NULL
                   AND publication_lane<>'unclassified'
                   AND ((period_role='current' AND publication_lane='current_actual')
                     OR (period_role IN ('prior_period_comparator','prior_year_comparator')
                         AND publication_lane='comparator')
                     OR (period_role='guidance' AND publication_lane='guidance_target')
                     OR period_role='unknown')
                   AND accounting_basis<>'unknown' AND consolidation_scope<>'unknown'
                   AND unit_scale<>'unknown'
                   AND (consolidation_scope NOT IN ('geography','segment','product')
                        OR dimensions_json<>'{}')
                   AND reason_code IS NULL)
               OR (status<>'admitted' AND reason_code IS NOT NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_kpi_fact_semantic_status ON "
        "kpi_fact_semantic_contexts(status,publication_lane,period_role,kpi_fact_id,revision)"
    )
    op.execute(
        "CREATE TRIGGER trg_kpi_fact_semantic_contexts_predecessor BEFORE INSERT ON "
        "kpi_fact_semantic_contexts WHEN NEW.revision > 1 BEGIN "
        "SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM kpi_fact_semantic_contexts prior "
        "WHERE prior.id=NEW.supersedes_context_id "
        "AND prior.kpi_fact_id=NEW.kpi_fact_id "
        "AND prior.revision=NEW.revision-1) THEN RAISE(ABORT, "
        "'KPI semantic revision predecessor mismatch') END; END"
    )
    op.execute(
        "CREATE TRIGGER trg_kpi_fact_semantic_contexts_no_update BEFORE UPDATE ON "
        "kpi_fact_semantic_contexts BEGIN SELECT RAISE(ABORT, "
        "'KPI semantic contexts are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_kpi_fact_semantic_contexts_no_delete BEFORE DELETE ON "
        "kpi_fact_semantic_contexts BEGIN SELECT RAISE(ABORT, "
        "'KPI semantic contexts are append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_fact_semantic_contexts_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_fact_semantic_contexts_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_fact_semantic_contexts_predecessor")
    op.execute("DROP INDEX IF EXISTS ix_kpi_fact_semantic_status")
    op.execute("DROP TABLE IF EXISTS kpi_fact_semantic_contexts")
