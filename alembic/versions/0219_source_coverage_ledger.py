"""Add immutable source inventories and expected-document coverage assessments."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0219_source_coverage_ledger"
down_revision: str | Sequence[str] | None = "0218_evidence_replica_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "source_inventory_snapshots",
    "expected_documents",
    "source_coverage_assessments",
)


def _append_only(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'source coverage ledger is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'source coverage ledger is append-only'); END"
    )


def upgrade() -> None:
    op.create_table(
        "source_inventory_snapshots",
        sa.Column("snapshot_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("inventory_key", sa.String(256), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("issuer_id", sa.String(128), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=True),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column(
            "source_observation_id",
            sa.String(128),
            sa.ForeignKey("evidence_source_observations.observation_id"),
            nullable=True,
        ),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("authoritative", sa.Boolean(), nullable=False),
        sa.Column("retrieval_config_sha256", sa.String(64), nullable=False),
        sa.Column("collector_code_version", sa.String(255), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column(
            "supersedes_snapshot_id",
            sa.String(128),
            sa.ForeignKey("source_inventory_snapshots.snapshot_id"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "inventory_key", "revision", name="uq_source_inventory_snapshot_revision"
        ),
        sa.CheckConstraint("revision > 0", name="ck_source_inventory_revision"),
        sa.CheckConstraint(
            "source_kind IN ('sec_submissions', 'ir_crawl', 'earnings_events')",
            name="ck_source_inventory_kind",
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'partial', 'failed')",
            name="ck_source_inventory_outcome",
        ),
        sa.CheckConstraint(
            "(outcome IN ('succeeded', 'partial') AND source_observation_id IS NOT NULL) "
            "OR (outcome = 'failed' AND source_observation_id IS NULL)",
            name="ck_source_inventory_observation",
        ),
        sa.CheckConstraint(
            "length(retrieval_config_sha256) = 64",
            name="ck_source_inventory_config_hash",
        ),
        sa.CheckConstraint(
            "completed_at >= started_at AND recorded_at >= completed_at",
            name="ck_source_inventory_clocks",
        ),
    )
    op.create_index(
        "ix_source_inventory_scope",
        "source_inventory_snapshots",
        ["issuer_id", "source_kind", "revision"],
    )
    op.create_table(
        "expected_documents",
        sa.Column("expected_document_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "snapshot_id",
            sa.String(128),
            sa.ForeignKey("source_inventory_snapshots.snapshot_id"),
            nullable=False,
        ),
        sa.Column("expected_document_key", sa.String(256), nullable=False),
        sa.Column("issuer_id", sa.String(128), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=True),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("document_type", sa.String(64), nullable=False),
        sa.Column("form_type", sa.String(64), nullable=True),
        sa.Column("accession_number", sa.String(64), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("primary_document", sa.Text(), nullable=True),
        sa.Column("period_start", sa.DateTime(), nullable=True),
        sa.Column("period_end", sa.DateTime(), nullable=True),
        sa.Column("filing_at", sa.DateTime(), nullable=True),
        sa.Column("expected_at", sa.DateTime(), nullable=True),
        sa.Column("expectation_basis", sa.String(32), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "snapshot_id",
            "expected_document_key",
            name="uq_expected_document_snapshot_key",
        ),
        sa.CheckConstraint(
            "source_kind IN ('sec_filing', 'ir_document', 'earnings_call')",
            name="ck_expected_document_source_kind",
        ),
        sa.CheckConstraint(
            "expectation_basis IN ('authoritative', 'publisher_candidate', 'policy_inferred')",
            name="ck_expected_document_basis",
        ),
        sa.CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_end >= period_start",
            name="ck_expected_document_period",
        ),
    )
    op.create_index(
        "ix_expected_document_logical_key",
        "expected_documents",
        ["expected_document_key", "snapshot_id"],
    )
    op.create_table(
        "source_coverage_assessments",
        sa.Column("assessment_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "expected_document_id",
            sa.String(128),
            sa.ForeignKey("expected_documents.expected_document_id"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("coverage_status", sa.String(32), nullable=False),
        sa.Column(
            "document_version_id",
            sa.String(128),
            sa.ForeignKey("evidence_document_versions.document_version_id"),
            nullable=True,
        ),
        sa.Column(
            "extraction_run_id",
            sa.String(128),
            sa.ForeignKey("evidence_extraction_runs.extraction_run_id"),
            nullable=True,
        ),
        sa.Column(
            "manifest_id",
            sa.String(128),
            sa.ForeignKey("search_corpus_manifests.manifest_id"),
            nullable=True,
        ),
        sa.Column(
            "index_run_id",
            sa.String(128),
            sa.ForeignKey("search_index_runs.index_run_id"),
            nullable=True,
        ),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason_details_json", sa.Text(), nullable=False),
        sa.Column("decision_kind", sa.String(16), nullable=False),
        sa.Column("policy_name", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("policy_config_sha256", sa.String(64), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column(
            "supersedes_assessment_id",
            sa.String(128),
            sa.ForeignKey("source_coverage_assessments.assessment_id"),
            nullable=True,
        ),
        sa.Column("material_dissent", sa.Boolean(), nullable=False),
        sa.UniqueConstraint(
            "expected_document_id",
            "revision",
            name="uq_source_coverage_assessment_revision",
        ),
        sa.CheckConstraint("revision > 0", name="ck_source_coverage_revision"),
        sa.CheckConstraint(
            "coverage_status IN ('available', 'not_published', 'not_discovered', "
            "'fetch_failed', 'quarantined', 'captured', 'extracted', 'indexed', "
            "'unsupported', 'authority_unavailable')",
            name="ck_source_coverage_status",
        ),
        sa.CheckConstraint(
            "(coverage_status NOT IN ('captured', 'extracted', 'indexed') "
            "OR document_version_id IS NOT NULL) "
            "AND (coverage_status NOT IN ('extracted', 'indexed') "
            "OR extraction_run_id IS NOT NULL) "
            "AND (coverage_status <> 'indexed' "
            "OR (manifest_id IS NOT NULL AND index_run_id IS NOT NULL))",
            name="ck_source_coverage_lineage",
        ),
        sa.CheckConstraint(
            "decision_kind IN ('deterministic', 'manual', 'imported')",
            name="ck_source_coverage_decision_kind",
        ),
        sa.CheckConstraint(
            "length(policy_config_sha256) = 64",
            name="ck_source_coverage_policy_hash",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_source_coverage_clocks",
        ),
    )
    op.create_index(
        "ix_source_coverage_expected_revision",
        "source_coverage_assessments",
        ["expected_document_id", "revision"],
    )
    op.execute(
        "CREATE TRIGGER trg_source_inventory_revision_chain "
        "BEFORE INSERT ON source_inventory_snapshots "
        "WHEN NEW.revision = 1 AND NEW.supersedes_snapshot_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'first source inventory cannot supersede'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_source_inventory_revision_parent "
        "BEFORE INSERT ON source_inventory_snapshots "
        "WHEN NEW.revision > 1 AND (NEW.supersedes_snapshot_id IS NULL OR NOT EXISTS "
        "(SELECT 1 FROM source_inventory_snapshots "
        "WHERE snapshot_id = NEW.supersedes_snapshot_id "
        "AND inventory_key = NEW.inventory_key AND revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, 'source inventory must supersede prior revision'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_expected_document_inventory_outcome "
        "BEFORE INSERT ON expected_documents "
        "WHEN NOT EXISTS (SELECT 1 FROM source_inventory_snapshots "
        "WHERE snapshot_id = NEW.snapshot_id AND outcome IN ('succeeded', 'partial')) "
        "BEGIN SELECT RAISE(ABORT, 'expected document requires successful or partial inventory'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_expected_document_inventory_scope "
        "BEFORE INSERT ON expected_documents "
        "WHEN NOT EXISTS (SELECT 1 FROM source_inventory_snapshots "
        "WHERE snapshot_id = NEW.snapshot_id AND issuer_id = NEW.issuer_id "
        "AND (NEW.ticker IS NULL OR ticker IS NULL OR ticker = NEW.ticker) "
        "AND (NEW.expectation_basis <> 'authoritative' OR authoritative = 1)) "
        "BEGIN SELECT RAISE(ABORT, 'expected document must match inventory scope and authority'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_source_coverage_revision_chain "
        "BEFORE INSERT ON source_coverage_assessments "
        "WHEN NEW.revision = 1 AND NEW.supersedes_assessment_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'first coverage assessment cannot supersede'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_source_coverage_revision_parent "
        "BEFORE INSERT ON source_coverage_assessments "
        "WHEN NEW.revision > 1 AND (NEW.supersedes_assessment_id IS NULL OR NOT EXISTS "
        "(SELECT 1 FROM source_coverage_assessments "
        "WHERE assessment_id = NEW.supersedes_assessment_id "
        "AND expected_document_id = NEW.expected_document_id "
        "AND revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, 'coverage assessment must supersede prior revision'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_source_coverage_document_scope "
        "BEFORE INSERT ON source_coverage_assessments "
        "WHEN NEW.document_version_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM evidence_document_versions AS document "
        "JOIN expected_documents AS expected "
        "ON expected.expected_document_id = NEW.expected_document_id "
        "WHERE document.document_version_id = NEW.document_version_id "
        "AND document.issuer_id = expected.issuer_id) "
        "BEGIN SELECT RAISE(ABORT, 'coverage document must match expected issuer'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_source_coverage_extraction_document "
        "BEFORE INSERT ON source_coverage_assessments "
        "WHEN NEW.extraction_run_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM evidence_extraction_runs WHERE extraction_run_id = NEW.extraction_run_id "
        "AND document_version_id = NEW.document_version_id) "
        "BEGIN SELECT RAISE(ABORT, 'coverage extraction must use assessed document'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_source_coverage_index_manifest "
        "BEFORE INSERT ON source_coverage_assessments "
        "WHEN NEW.index_run_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM search_index_runs WHERE index_run_id = NEW.index_run_id "
        "AND manifest_id = NEW.manifest_id AND outcome = 'succeeded') "
        "BEGIN SELECT RAISE(ABORT, 'indexed coverage requires successful manifest index'); END"
    )
    for table in _TABLES:
        _append_only(table)
    op.execute(
        "CREATE VIEW v_source_inventory_current AS "
        "SELECT * FROM source_inventory_snapshots AS snapshot WHERE NOT EXISTS "
        "(SELECT 1 FROM source_inventory_snapshots AS newer "
        "WHERE newer.inventory_key = snapshot.inventory_key "
        "AND newer.revision > snapshot.revision)"
    )
    op.execute(
        "CREATE VIEW v_expected_documents_current AS "
        "SELECT expected.* FROM expected_documents AS expected "
        "JOIN v_source_inventory_current AS inventory "
        "ON inventory.snapshot_id = expected.snapshot_id"
    )
    op.execute(
        "CREATE VIEW v_source_coverage_current AS "
        "SELECT assessment.* FROM source_coverage_assessments AS assessment "
        "WHERE NOT EXISTS (SELECT 1 FROM source_coverage_assessments AS newer "
        "WHERE newer.expected_document_id = assessment.expected_document_id "
        "AND newer.revision > assessment.revision)"
    )


def downgrade() -> None:
    for view in (
        "v_source_coverage_current",
        "v_expected_documents_current",
        "v_source_inventory_current",
    ):
        op.execute(f"DROP VIEW IF EXISTS {view}")
    for trigger in (
        "trg_source_inventory_revision_chain",
        "trg_source_inventory_revision_parent",
        "trg_expected_document_inventory_outcome",
        "trg_expected_document_inventory_scope",
        "trg_source_coverage_revision_chain",
        "trg_source_coverage_revision_parent",
        "trg_source_coverage_document_scope",
        "trg_source_coverage_extraction_document",
        "trg_source_coverage_index_manifest",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in _TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
    op.drop_index(
        "ix_source_coverage_expected_revision",
        table_name="source_coverage_assessments",
    )
    op.drop_table("source_coverage_assessments")
    op.drop_index("ix_expected_document_logical_key", table_name="expected_documents")
    op.drop_table("expected_documents")
    op.drop_index("ix_source_inventory_scope", table_name="source_inventory_snapshots")
    op.drop_table("source_inventory_snapshots")
