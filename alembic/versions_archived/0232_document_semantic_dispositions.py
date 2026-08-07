"""Add revisioned semantic-content dispositions for captured documents."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0232_document_semantic_dispositions"
down_revision: str | Sequence[str] | None = "0231_legacy_document_evidence_bindings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "document_semantic_disposition_revisions"
_VIEW = "v_document_semantic_dispositions_current"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("assessment_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "document_version_id",
            sa.String(128),
            sa.ForeignKey("evidence_document_versions.document_version_id"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("semantic_status", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason_details_json", sa.Text(), nullable=False),
        sa.Column("decision_kind", sa.String(32), nullable=False),
        sa.Column("reviewer_identity", sa.String(255), nullable=True),
        sa.Column("policy_name", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("policy_config_sha256", sa.String(64), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column(
            "supersedes_assessment_id",
            sa.String(128),
            sa.ForeignKey(f"{_TABLE}.assessment_id"),
            nullable=True,
        ),
        sa.Column("material_dissent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint(
            "document_version_id",
            "revision",
            name="uq_document_semantic_disposition_revision",
        ),
        sa.CheckConstraint("revision > 0", name="ck_document_semantic_disposition_revision"),
        sa.CheckConstraint(
            "semantic_status IN "
            "('required', 'not_required', 'review_required', 'quarantined')",
            name="ck_document_semantic_disposition_status",
        ),
        sa.CheckConstraint(
            "decision_kind IN ('deterministic', 'human', 'model_assisted')",
            name="ck_document_semantic_disposition_decision",
        ),
        sa.CheckConstraint(
            "(decision_kind = 'human' AND reviewer_identity IS NOT NULL) OR "
            "(decision_kind <> 'human' AND reviewer_identity IS NULL)",
            name="ck_document_semantic_disposition_reviewer",
        ),
        sa.CheckConstraint(
            "semantic_status <> 'not_required' OR decision_kind = 'human'",
            name="ck_document_semantic_disposition_exclusion_authority",
        ),
        sa.CheckConstraint(
            "length(policy_config_sha256) = 64",
            name="ck_document_semantic_disposition_policy_hash",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_document_semantic_disposition_clocks",
        ),
    )
    op.create_index(
        "ix_document_semantic_disposition_current",
        _TABLE,
        ["document_version_id", "revision"],
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_revision_chain BEFORE INSERT ON {_TABLE} "
        "WHEN (NEW.revision = 1 AND NEW.supersedes_assessment_id IS NOT NULL) "
        "OR (NEW.revision > 1 AND NOT EXISTS "
        f"(SELECT 1 FROM {_TABLE} AS prior "
        "WHERE prior.assessment_id = NEW.supersedes_assessment_id "
        "AND prior.document_version_id = NEW.document_version_id "
        "AND prior.revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, 'semantic disposition revision chain is invalid'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_append_only BEFORE UPDATE ON {_TABLE} "
        "BEGIN SELECT RAISE(ABORT, 'semantic disposition ledger is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_append_only_delete BEFORE DELETE ON {_TABLE} "
        "BEGIN SELECT RAISE(ABORT, 'semantic disposition ledger is append-only'); END"
    )
    op.execute(
        f"CREATE VIEW {_VIEW} AS SELECT disposition.* FROM {_TABLE} AS disposition "
        "WHERE NOT EXISTS "
        f"(SELECT 1 FROM {_TABLE} AS newer "
        "WHERE newer.document_version_id = disposition.document_version_id "
        "AND newer.revision > disposition.revision)"
    )


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {_VIEW}")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_append_only_delete")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_append_only")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_revision_chain")
    op.drop_index("ix_document_semantic_disposition_current", table_name=_TABLE)
    op.drop_table(_TABLE)
