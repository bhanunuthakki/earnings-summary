"""Preserve expected documents across authority withdrawals and supersessions."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0224_expected_document_lifecycle"
down_revision: str | Sequence[str] | None = "0223_embedding_model_promotions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "expected_document_lifecycle_revisions",
        sa.Column("lifecycle_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("inventory_key", sa.String(256), nullable=False),
        sa.Column("expected_document_key", sa.String(256), nullable=False),
        sa.Column(
            "source_inventory_snapshot_id",
            sa.String(128),
            sa.ForeignKey("source_inventory_snapshots.snapshot_id"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "expected_document_id",
            sa.String(128),
            sa.ForeignKey("expected_documents.expected_document_id"),
            nullable=True,
        ),
        sa.Column(
            "authority_observation_id",
            sa.String(128),
            sa.ForeignKey("evidence_source_observations.observation_id"),
            nullable=False,
        ),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason_details_json", sa.Text(), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column(
            "supersedes_lifecycle_id",
            sa.String(128),
            sa.ForeignKey("expected_document_lifecycle_revisions.lifecycle_id"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "inventory_key",
            "expected_document_key",
            "revision",
            name="uq_expected_document_lifecycle_revision",
        ),
        sa.UniqueConstraint(
            "source_inventory_snapshot_id",
            "expected_document_key",
            name="uq_expected_document_lifecycle_snapshot_key",
        ),
        sa.CheckConstraint("revision > 0", name="ck_expected_document_lifecycle_revision"),
        sa.CheckConstraint(
            "status IN ('expected', 'withdrawn_by_authority', "
            "'superseded_by_authority')",
            name="ck_expected_document_lifecycle_status",
        ),
        sa.CheckConstraint(
            "(status = 'expected' AND expected_document_id IS NOT NULL) OR "
            "(status <> 'expected' AND expected_document_id IS NULL)",
            name="ck_expected_document_lifecycle_anchor",
        ),
    )
    op.create_index(
        "ix_expected_document_lifecycle_current",
        "expected_document_lifecycle_revisions",
        ["inventory_key", "expected_document_key", "revision"],
    )
    op.execute(
        "CREATE TRIGGER trg_expected_document_lifecycle_revision_chain "
        "BEFORE INSERT ON expected_document_lifecycle_revisions "
        "WHEN (NEW.revision = 1 AND NEW.supersedes_lifecycle_id IS NOT NULL) "
        "OR (NEW.revision > 1 AND NOT EXISTS "
        "(SELECT 1 FROM expected_document_lifecycle_revisions AS prior "
        "WHERE prior.lifecycle_id = NEW.supersedes_lifecycle_id "
        "AND prior.inventory_key = NEW.inventory_key "
        "AND prior.expected_document_key = NEW.expected_document_key "
        "AND prior.revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, 'expected document lifecycle chain is invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_expected_document_lifecycle_revisions_append_only "
        "BEFORE UPDATE ON expected_document_lifecycle_revisions "
        "BEGIN SELECT RAISE(ABORT, 'expected document lifecycle is append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_expected_document_lifecycle_revisions_append_only_delete "
        "BEFORE DELETE ON expected_document_lifecycle_revisions "
        "BEGIN SELECT RAISE(ABORT, 'expected document lifecycle is append-only'); END"
    )
    op.execute(
        "CREATE VIEW v_expected_document_lifecycle_current AS "
        "SELECT lifecycle.* FROM expected_document_lifecycle_revisions AS lifecycle "
        "WHERE NOT EXISTS (SELECT 1 FROM expected_document_lifecycle_revisions AS newer "
        "WHERE newer.inventory_key = lifecycle.inventory_key "
        "AND newer.expected_document_key = lifecycle.expected_document_key "
        "AND newer.revision > lifecycle.revision)"
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_expected_document_lifecycle_current")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_expected_document_lifecycle_revisions_append_only_delete"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_expected_document_lifecycle_revisions_append_only")
    op.execute("DROP TRIGGER IF EXISTS trg_expected_document_lifecycle_revision_chain")
    op.drop_index(
        "ix_expected_document_lifecycle_current",
        table_name="expected_document_lifecycle_revisions",
    )
    op.drop_table("expected_document_lifecycle_revisions")
