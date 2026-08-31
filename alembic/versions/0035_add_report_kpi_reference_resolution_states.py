"""Allow evidence-bound resolved and reviewed-retired report KPI references.

Revision ID: 0035_add_report_kpi_reference_resolution_states
Revises: 0034_add_investment_profile_label_reviews
"""

from __future__ import annotations

from alembic import op

revision = "0035_add_report_kpi_reference_resolution_states"
down_revision = "0034_add_investment_profile_label_reviews"
branch_labels = None
depends_on = None


_CREATE_V2 = """
CREATE TABLE report_kpi_reference_resolution_revisions_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL CHECK(length(trim(user_id)) > 0),
    ticker TEXT NOT NULL CHECK(length(trim(ticker)) > 0),
    source_path TEXT NOT NULL CHECK(length(trim(source_path)) > 0),
    json_pointer TEXT NOT NULL CHECK(length(trim(json_pointer)) > 0),
    reference_kind TEXT NOT NULL CHECK(reference_kind IN
        ('chart_priority','tier_1_kpi','tier_2_kpi','tier_3_kpi','break_rule',
         'business_model_rule','soft_rule_kpi')),
    requested_label TEXT NOT NULL CHECK(length(trim(requested_label)) > 0),
    reference_content_sha256 TEXT NOT NULL
        CHECK(length(reference_content_sha256)=64
          AND reference_content_sha256 NOT GLOB '*[^0-9a-f]*'),
    status TEXT NOT NULL CHECK(status IN ('resolved','unresolved','retired')),
    kpi_definition_id INTEGER REFERENCES kpi_definitions(id),
    definition_identity_sha256 TEXT
        CHECK(definition_identity_sha256 IS NULL OR
          (length(definition_identity_sha256)=64
           AND definition_identity_sha256 NOT GLOB '*[^0-9a-f]*')),
    evidence_fact_id INTEGER REFERENCES kpi_facts(id),
    evidence_context_id INTEGER REFERENCES kpi_fact_semantic_contexts(id),
    evidence_sha256 TEXT
        CHECK(evidence_sha256 IS NULL OR
          (length(evidence_sha256)=64 AND evidence_sha256 NOT GLOB '*[^0-9a-f]*')),
    resolution_method TEXT CHECK(resolution_method IS NULL OR resolution_method IN
        ('exact_definition_identity','unit_surface_alias')),
    policy_name TEXT CHECK(policy_name IS NULL OR length(trim(policy_name)) > 0),
    policy_version TEXT CHECK(policy_version IS NULL OR length(trim(policy_version)) > 0),
    policy_config_sha256 TEXT
        CHECK(policy_config_sha256 IS NULL OR
          (length(policy_config_sha256)=64
           AND policy_config_sha256 NOT GLOB '*[^0-9a-f]*')),
    reason_code TEXT NOT NULL CHECK(length(trim(reason_code)) > 0),
    revision INTEGER NOT NULL CHECK(revision > 0),
    supersedes_resolution_id INTEGER UNIQUE
        REFERENCES report_kpi_reference_resolution_revisions_v2(id),
    reviewed_by TEXT NOT NULL CHECK(length(trim(reviewed_by)) > 0),
    knowledge_at TEXT NOT NULL CHECK(length(trim(knowledge_at)) > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(user_id,ticker,source_path,json_pointer,revision),
    CHECK((revision=1 AND supersedes_resolution_id IS NULL)
       OR (revision>1 AND supersedes_resolution_id IS NOT NULL)),
    CHECK(
      (status='resolved'
       AND kpi_definition_id IS NOT NULL
       AND definition_identity_sha256 IS NOT NULL
       AND evidence_fact_id IS NOT NULL
       AND evidence_context_id IS NOT NULL
       AND evidence_sha256 IS NOT NULL
       AND resolution_method IS NOT NULL
       AND policy_name IS NOT NULL
       AND policy_version IS NOT NULL
       AND policy_config_sha256 IS NOT NULL)
      OR
      (status IN ('unresolved','retired')
       AND kpi_definition_id IS NULL
       AND definition_identity_sha256 IS NULL
       AND evidence_fact_id IS NULL
       AND evidence_context_id IS NULL
       AND evidence_sha256 IS NULL
       AND resolution_method IS NULL
       AND policy_name IS NULL
       AND policy_version IS NULL
       AND policy_config_sha256 IS NULL)
    )
)
"""


_CREATE_V1 = """
CREATE TABLE report_kpi_reference_resolution_revisions_v1 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL CHECK(length(trim(user_id)) > 0),
    ticker TEXT NOT NULL CHECK(length(trim(ticker)) > 0),
    source_path TEXT NOT NULL CHECK(length(trim(source_path)) > 0),
    json_pointer TEXT NOT NULL CHECK(length(trim(json_pointer)) > 0),
    reference_kind TEXT NOT NULL CHECK(reference_kind IN
        ('chart_priority','tier_1_kpi','tier_2_kpi','tier_3_kpi','break_rule')),
    requested_label TEXT NOT NULL CHECK(length(trim(requested_label)) > 0),
    reference_content_sha256 TEXT NOT NULL
        CHECK(length(reference_content_sha256)=64
          AND reference_content_sha256 NOT GLOB '*[^0-9a-f]*'),
    status TEXT NOT NULL CHECK(status='unresolved'),
    kpi_definition_id INTEGER REFERENCES kpi_definitions(id) CHECK(kpi_definition_id IS NULL),
    reason_code TEXT NOT NULL CHECK(length(trim(reason_code)) > 0),
    revision INTEGER NOT NULL CHECK(revision > 0),
    supersedes_resolution_id INTEGER UNIQUE
        REFERENCES report_kpi_reference_resolution_revisions_v1(id),
    reviewed_by TEXT NOT NULL CHECK(length(trim(reviewed_by)) > 0),
    knowledge_at TEXT NOT NULL CHECK(length(trim(knowledge_at)) > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(user_id,ticker,source_path,json_pointer,revision),
    CHECK((revision=1 AND supersedes_resolution_id IS NULL)
       OR (revision>1 AND supersedes_resolution_id IS NOT NULL)),
    CHECK(status='unresolved' AND kpi_definition_id IS NULL)
)
"""


def _drop_guards() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_report_kpi_reference_resolution_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_report_kpi_reference_resolution_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_report_kpi_reference_resolution_predecessor")
    op.execute("DROP INDEX IF EXISTS ix_report_kpi_reference_resolution_current")


def _create_guards() -> None:
    op.execute(
        "CREATE INDEX ix_report_kpi_reference_resolution_current ON "
        "report_kpi_reference_resolution_revisions(user_id,ticker,source_path,json_pointer,revision)"
    )
    op.execute(
        "CREATE TRIGGER trg_report_kpi_reference_resolution_predecessor BEFORE INSERT ON "
        "report_kpi_reference_resolution_revisions WHEN NEW.revision > 1 BEGIN "
        "SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM report_kpi_reference_resolution_revisions prior "
        "WHERE prior.id=NEW.supersedes_resolution_id AND prior.user_id=NEW.user_id "
        "AND prior.ticker=NEW.ticker AND prior.source_path=NEW.source_path "
        "AND prior.json_pointer=NEW.json_pointer AND prior.revision=NEW.revision-1) "
        "THEN RAISE(ABORT,'report KPI reference predecessor mismatch') END; END"
    )
    op.execute(
        "CREATE TRIGGER trg_report_kpi_reference_resolution_no_update BEFORE UPDATE ON "
        "report_kpi_reference_resolution_revisions BEGIN SELECT RAISE(ABORT,"
        "'report KPI reference resolutions are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_report_kpi_reference_resolution_no_delete BEFORE DELETE ON "
        "report_kpi_reference_resolution_revisions BEGIN SELECT RAISE(ABORT,"
        "'report KPI reference resolutions are append-only'); END"
    )


def upgrade() -> None:
    _drop_guards()
    op.execute(_CREATE_V2)
    op.execute(
        """
        INSERT INTO report_kpi_reference_resolution_revisions_v2 (
            id,user_id,ticker,source_path,json_pointer,reference_kind,requested_label,
            reference_content_sha256,status,kpi_definition_id,reason_code,revision,
            supersedes_resolution_id,reviewed_by,knowledge_at,created_at
        )
        SELECT id,user_id,ticker,source_path,json_pointer,reference_kind,requested_label,
               reference_content_sha256,status,kpi_definition_id,reason_code,revision,
               supersedes_resolution_id,reviewed_by,knowledge_at,created_at
        FROM report_kpi_reference_resolution_revisions
        ORDER BY id
        """
    )
    op.execute("DROP TABLE report_kpi_reference_resolution_revisions")
    op.execute(
        "ALTER TABLE report_kpi_reference_resolution_revisions_v2 "
        "RENAME TO report_kpi_reference_resolution_revisions"
    )
    _create_guards()


def downgrade() -> None:
    bind = op.get_bind()
    incompatible = bind.exec_driver_sql(
        "SELECT COUNT(*) FROM report_kpi_reference_resolution_revisions "
        "WHERE status<>'unresolved' OR kpi_definition_id IS NOT NULL "
        "OR reference_kind NOT IN "
        "('chart_priority','tier_1_kpi','tier_2_kpi','tier_3_kpi','break_rule')"
    ).scalar_one()
    if int(incompatible) != 0:
        raise RuntimeError("cannot downgrade report KPI reference v2 rows without losing history")
    _drop_guards()
    op.execute(_CREATE_V1)
    op.execute(
        """
        INSERT INTO report_kpi_reference_resolution_revisions_v1 (
            id,user_id,ticker,source_path,json_pointer,reference_kind,requested_label,
            reference_content_sha256,status,kpi_definition_id,reason_code,revision,
            supersedes_resolution_id,reviewed_by,knowledge_at,created_at
        )
        SELECT id,user_id,ticker,source_path,json_pointer,reference_kind,requested_label,
               reference_content_sha256,status,kpi_definition_id,reason_code,revision,
               supersedes_resolution_id,reviewed_by,knowledge_at,created_at
        FROM report_kpi_reference_resolution_revisions
        ORDER BY id
        """
    )
    op.execute("DROP TABLE report_kpi_reference_resolution_revisions")
    op.execute(
        "ALTER TABLE report_kpi_reference_resolution_revisions_v1 "
        "RENAME TO report_kpi_reference_resolution_revisions"
    )
    _create_guards()
