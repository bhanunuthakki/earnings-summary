"""Bind recorded evidence subjects below the legal-issuer boundary.

Revision ID: 0230_evidence_subject_bindings
Revises: 0229_reporting_entities_and_obligations
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0230_evidence_subject_bindings"
down_revision = "0229_reporting_entities_and_obligations"
branch_labels = None
depends_on = None


def _append_only(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'evidence subject registry is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'evidence subject registry is append-only'); END"
    )


def _create_canonical_document_view(*, include_subject: bool) -> None:
    subject_join = ""
    canonical_issuer = (
        "COALESCE(subject.issuer_id, canonical.issuer_id, "
        "binding.canonical_issuer_id, document.issuer_id)"
        if include_subject
        else "COALESCE(canonical.issuer_id, binding.canonical_issuer_id, document.issuer_id)"
    )
    subject_columns = (
        ", subject.reporting_entity_id, subject.security_id" if include_subject else ""
    )
    if include_subject:
        subject_join = (
            " LEFT JOIN v_recorded_subject_bindings_current AS subject "
            "ON subject.recorded_issuer_id = document.issuer_id "
            "AND subject.outcome = 'selected'"
        )
    op.execute(
        "CREATE VIEW v_evidence_document_versions_canonical AS "
        "SELECT "
        "document.document_version_id, document.document_key, "
        "document.version_sequence, document.observation_id, document.blob_sha256, "
        f"{canonical_issuer} AS issuer_id, "
        "document.issuer_id AS recorded_issuer_id, document.ticker, "
        "document.document_type, document.form_type, document.accession_number, "
        "document.exhibit_id, document.period_start, document.period_end, "
        "document.as_of_at, document.language, document.replaces_document_version_id, "
        "document.legacy_document_id, document.recorded_at"
        f"{subject_columns} "
        "FROM evidence_document_versions AS document "
        "LEFT JOIN issuer_entities AS canonical "
        "ON canonical.issuer_id = document.issuer_id "
        "LEFT JOIN v_legacy_issuer_bindings_current AS binding "
        "ON binding.recorded_issuer_id = document.issuer_id "
        "AND binding.outcome = 'selected'"
        f"{subject_join}"
    )


def _create_coverage_scope_trigger() -> None:
    op.execute(
        "CREATE TRIGGER trg_source_coverage_document_scope "
        "BEFORE INSERT ON source_coverage_assessments "
        "WHEN NEW.document_version_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM v_evidence_document_versions_canonical AS document "
        "JOIN expected_documents AS expected "
        "ON expected.expected_document_id = NEW.expected_document_id "
        "WHERE document.document_version_id = NEW.document_version_id "
        "AND document.issuer_id = expected.issuer_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'coverage document must match canonical expected issuer'); END"
    )


def upgrade() -> None:
    op.create_table(
        "recorded_subject_binding_revisions",
        sa.Column("binding_revision_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("recorded_issuer_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "issuer_id",
            sa.String(128),
            sa.ForeignKey("issuer_entities.issuer_id"),
            nullable=True,
        ),
        sa.Column(
            "reporting_entity_id",
            sa.String(128),
            sa.ForeignKey("reporting_entities.reporting_entity_id"),
            nullable=True,
        ),
        sa.Column(
            "security_id",
            sa.String(128),
            sa.ForeignKey("securities.security_id"),
            nullable=True,
        ),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("decision_kind", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason_details_json", sa.Text(), nullable=False),
        sa.Column("material_dissent", sa.Boolean(), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column(
            "supersedes_binding_revision_id",
            sa.String(128),
            sa.ForeignKey("recorded_subject_binding_revisions.binding_revision_id"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "recorded_issuer_id",
            "revision",
            name="uq_recorded_subject_binding_revision",
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_recorded_subject_binding_revision",
        ),
        sa.CheckConstraint(
            "outcome IN ('selected', 'unresolved', 'retired')",
            name="ck_recorded_subject_binding_outcome",
        ),
        sa.CheckConstraint(
            "(outcome = 'selected' AND issuer_id IS NOT NULL) OR "
            "(outcome IN ('unresolved', 'retired') AND issuer_id IS NULL "
            "AND reporting_entity_id IS NULL AND security_id IS NULL)",
            name="ck_recorded_subject_binding_selection",
        ),
        sa.CheckConstraint(
            "decision_kind IN ('deterministic', 'manual', 'imported')",
            name="ck_recorded_subject_binding_decision_kind",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_recorded_subject_binding_clocks",
        ),
    )
    op.create_index(
        "ix_recorded_subject_binding_current",
        "recorded_subject_binding_revisions",
        ["recorded_issuer_id", "revision"],
    )
    op.create_index(
        "ix_recorded_subject_binding_targets",
        "recorded_subject_binding_revisions",
        ["issuer_id", "reporting_entity_id", "security_id", "revision"],
    )
    op.execute(
        "CREATE TRIGGER trg_recorded_subject_binding_revision_first "
        "BEFORE INSERT ON recorded_subject_binding_revisions "
        "WHEN NEW.revision = 1 AND NEW.supersedes_binding_revision_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'first subject binding cannot supersede'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_recorded_subject_binding_revision_parent "
        "BEFORE INSERT ON recorded_subject_binding_revisions "
        "WHEN NEW.revision > 1 AND (NEW.supersedes_binding_revision_id IS NULL OR "
        "NOT EXISTS (SELECT 1 FROM recorded_subject_binding_revisions AS prior "
        "WHERE prior.binding_revision_id = NEW.supersedes_binding_revision_id "
        "AND prior.recorded_issuer_id = NEW.recorded_issuer_id "
        "AND prior.revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, 'subject binding must supersede prior revision'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_recorded_subject_binding_reporting_scope "
        "BEFORE INSERT ON recorded_subject_binding_revisions "
        "WHEN NEW.reporting_entity_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM reporting_entities AS entity "
        "WHERE entity.reporting_entity_id = NEW.reporting_entity_id "
        "AND entity.issuer_id = NEW.issuer_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'subject reporting entity must belong to canonical issuer'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_recorded_subject_binding_security_scope "
        "BEFORE INSERT ON recorded_subject_binding_revisions "
        "WHEN NEW.security_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM securities AS security "
        "WHERE security.security_id = NEW.security_id "
        "AND security.issuer_id = NEW.issuer_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'subject security must belong to canonical issuer'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_recorded_subject_binding_relationship_scope "
        "BEFORE INSERT ON recorded_subject_binding_revisions "
        "WHEN NEW.reporting_entity_id IS NOT NULL AND NEW.security_id IS NOT NULL "
        "AND NOT EXISTS ("
        "SELECT 1 FROM security_reporting_entity_revisions AS relation "
        "WHERE relation.security_id = NEW.security_id "
        "AND relation.reporting_entity_id = NEW.reporting_entity_id "
        "AND NOT EXISTS ("
        "SELECT 1 FROM security_reporting_entity_revisions AS newer "
        "WHERE newer.relationship_key = relation.relationship_key "
        "AND newer.revision > relation.revision)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'subject security must map to selected reporting entity'); END"
    )
    _append_only("recorded_subject_binding_revisions")
    op.execute(
        "CREATE VIEW v_recorded_subject_bindings_current AS "
        "SELECT binding.* FROM recorded_subject_binding_revisions AS binding "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM recorded_subject_binding_revisions AS newer "
        "WHERE newer.recorded_issuer_id = binding.recorded_issuer_id "
        "AND newer.revision > binding.revision)"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_source_coverage_document_scope")
    op.execute("DROP VIEW IF EXISTS v_evidence_document_versions_canonical")
    _create_canonical_document_view(include_subject=True)
    _create_coverage_scope_trigger()


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_source_coverage_document_scope")
    op.execute("DROP VIEW IF EXISTS v_evidence_document_versions_canonical")
    op.execute("DROP VIEW IF EXISTS v_recorded_subject_bindings_current")
    for trigger in (
        "trg_recorded_subject_binding_revisions_append_only",
        "trg_recorded_subject_binding_revisions_append_only_delete",
        "trg_recorded_subject_binding_revision_first",
        "trg_recorded_subject_binding_revision_parent",
        "trg_recorded_subject_binding_reporting_scope",
        "trg_recorded_subject_binding_security_scope",
        "trg_recorded_subject_binding_relationship_scope",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.drop_index(
        "ix_recorded_subject_binding_targets",
        table_name="recorded_subject_binding_revisions",
    )
    op.drop_index(
        "ix_recorded_subject_binding_current",
        table_name="recorded_subject_binding_revisions",
    )
    op.drop_table("recorded_subject_binding_revisions")
    _create_canonical_document_view(include_subject=False)
    _create_coverage_scope_trigger()
