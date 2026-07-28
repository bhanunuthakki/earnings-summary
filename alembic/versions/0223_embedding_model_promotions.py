"""Govern evidence-vector model promotion from closed evaluation artifacts."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0223_embedding_model_promotions"
down_revision: str | Sequence[str] | None = "0222_ocr_extraction_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "search_embedding_model_promotions",
        sa.Column("promotion_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("golden_sha256", sa.String(64), nullable=False),
        sa.Column("evaluation_artifact_sha256", sa.String(64), nullable=False),
        sa.Column("evaluation_metrics_json", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.String(128), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=False),
        sa.Column(
            "supersedes_promotion_id",
            sa.String(128),
            sa.ForeignKey("search_embedding_model_promotions.promotion_id"),
            nullable=True,
        ),
        sa.UniqueConstraint("purpose", "revision", name="uq_embedding_promotion_revision"),
        sa.CheckConstraint("revision > 0", name="ck_embedding_promotion_revision"),
        sa.CheckConstraint("dimensions > 0", name="ck_embedding_promotion_dimensions"),
        sa.CheckConstraint(
            "length(golden_sha256) = 64 AND length(evaluation_artifact_sha256) = 64",
            name="ck_embedding_promotion_hashes",
        ),
    )
    op.execute(
        "CREATE TRIGGER trg_search_embedding_model_promotions_revision "
        "BEFORE INSERT ON search_embedding_model_promotions "
        "WHEN (NEW.revision = 1 AND NEW.supersedes_promotion_id IS NOT NULL) "
        "OR (NEW.revision > 1 AND NOT EXISTS "
        "(SELECT 1 FROM search_embedding_model_promotions AS prior "
        "WHERE prior.promotion_id = NEW.supersedes_promotion_id "
        "AND prior.purpose = NEW.purpose AND prior.revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, 'embedding promotion revision chain is invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_embedding_model_promotions_append_only "
        "BEFORE UPDATE ON search_embedding_model_promotions "
        "BEGIN SELECT RAISE(ABORT, 'embedding promotions are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_embedding_model_promotions_append_only_delete "
        "BEFORE DELETE ON search_embedding_model_promotions "
        "BEGIN SELECT RAISE(ABORT, 'embedding promotions are append-only'); END"
    )
    op.execute(
        "CREATE VIEW v_search_embedding_model_promotion_current AS "
        "SELECT promotion.* FROM search_embedding_model_promotions AS promotion "
        "WHERE NOT EXISTS (SELECT 1 FROM search_embedding_model_promotions AS newer "
        "WHERE newer.purpose = promotion.purpose AND newer.revision > promotion.revision)"
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_search_embedding_model_promotion_current")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_search_embedding_model_promotions_append_only_delete"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_search_embedding_model_promotions_append_only"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_search_embedding_model_promotions_revision")
    op.drop_table("search_embedding_model_promotions")
