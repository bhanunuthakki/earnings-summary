"""Seal exact lexical and vector search projections before they become current."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0233_search_projection_seals"
down_revision: str | Sequence[str] | None = "0232_document_semantic_dispositions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "search_projection_seals"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("projection_seal_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "index_run_id",
            sa.String(128),
            sa.ForeignKey("search_index_runs.index_run_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "manifest_id",
            sa.String(128),
            sa.ForeignKey("search_corpus_manifests.manifest_id"),
            nullable=False,
        ),
        sa.Column("index_kind", sa.String(16), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("chunk_set_sha256", sa.String(64), nullable=False),
        sa.Column("projection_records_sha256", sa.String(64), nullable=False),
        sa.Column("artifact_set_sha256", sa.String(64), nullable=True),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("dimensions", sa.Integer(), nullable=True),
        sa.Column("config_sha256", sa.String(64), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column("sealed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("index_kind IN ('lexical', 'vector')", name="ck_search_projection_kind"),
        sa.CheckConstraint(
            "(index_kind = 'vector' AND chunk_count > 0) "
            "OR (index_kind = 'lexical' AND chunk_count >= 0)",
            name="ck_search_projection_chunk_count",
        ),
        sa.CheckConstraint(
            "length(chunk_set_sha256) = 64 "
            "AND length(projection_records_sha256) = 64 "
            "AND length(config_sha256) = 64",
            name="ck_search_projection_hashes",
        ),
        sa.CheckConstraint(
            "(index_kind = 'vector' AND length(artifact_set_sha256) = 64 "
            "AND provider IS NOT NULL AND model IS NOT NULL AND dimensions > 0) "
            "OR (index_kind = 'lexical' AND artifact_set_sha256 IS NULL "
            "AND provider IS NULL AND model IS NULL AND dimensions IS NULL)",
            name="ck_search_projection_backend_contract",
        ),
    )
    op.create_index(
        "ix_search_projection_manifest_kind",
        _TABLE,
        ["manifest_id", "index_kind", "sealed_at"],
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_run_contract BEFORE INSERT ON {_TABLE} "
        "WHEN NOT EXISTS (SELECT 1 FROM search_index_runs AS run "
        "JOIN search_corpus_manifest_seals AS corpus_seal "
        "ON corpus_seal.manifest_id = run.manifest_id "
        "WHERE run.index_run_id = NEW.index_run_id "
        "AND run.manifest_id = NEW.manifest_id "
        "AND run.index_kind = NEW.index_kind "
        "AND run.config_sha256 = NEW.config_sha256 "
        "AND run.outcome = 'succeeded' "
        "AND corpus_seal.completion_status = 'complete') "
        "BEGIN SELECT RAISE(ABORT, 'projection seal requires matching complete successful run'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_chunk_count BEFORE INSERT ON {_TABLE} "
        "WHEN NEW.chunk_count <> "
        "(SELECT COUNT(*) FROM search_chunks WHERE manifest_id = NEW.manifest_id) "
        "BEGIN SELECT RAISE(ABORT, 'projection seal chunk count does not match manifest'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_vector_coverage BEFORE INSERT ON {_TABLE} "
        "WHEN NEW.index_kind = 'vector' AND ("
        "(SELECT COUNT(*) FROM search_index_memberships "
        "WHERE index_run_id = NEW.index_run_id AND membership_status = 'included') "
        "<> NEW.chunk_count OR "
        "(SELECT COUNT(*) FROM search_embedding_artifacts "
        "WHERE index_run_id = NEW.index_run_id AND outcome = 'succeeded' "
        "AND provider = NEW.provider AND model = NEW.model AND dimensions = NEW.dimensions) "
        "<> NEW.chunk_count OR EXISTS (SELECT 1 FROM search_chunks AS chunk "
        "LEFT JOIN search_index_memberships AS membership "
        "ON membership.index_run_id = NEW.index_run_id "
        "AND membership.chunk_id = chunk.chunk_id "
        "LEFT JOIN search_embedding_artifacts AS artifact "
        "ON artifact.index_run_id = NEW.index_run_id "
        "AND artifact.chunk_id = chunk.chunk_id "
        "AND artifact.outcome = 'succeeded' "
        "AND artifact.provider = NEW.provider AND artifact.model = NEW.model "
        "AND artifact.dimensions = NEW.dimensions "
        "WHERE chunk.manifest_id = NEW.manifest_id "
        "AND (membership.membership_status IS NULL "
        "OR membership.membership_status <> 'included' OR artifact.chunk_id IS NULL))) "
        "BEGIN SELECT RAISE(ABORT, 'vector projection seal requires exact artifact membership'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_lexical_coverage BEFORE INSERT ON {_TABLE} "
        "WHEN NEW.index_kind = 'lexical' AND EXISTS ("
        "SELECT 1 FROM search_chunks AS chunk WHERE chunk.manifest_id = NEW.manifest_id "
        "AND ((SELECT COUNT(*) FROM search_lexical_chunks AS lexical "
        "WHERE lexical.chunk_id = chunk.chunk_id) <> 1 "
        "OR NOT EXISTS (SELECT 1 FROM search_lexical_chunks AS lexical "
        "WHERE lexical.chunk_id = chunk.chunk_id AND lexical.text = chunk.text))) "
        "BEGIN SELECT RAISE(ABORT, 'lexical projection seal requires exact FTS rows'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_append_only BEFORE UPDATE ON {_TABLE} "
        "BEGIN SELECT RAISE(ABORT, 'search projection seals are append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_append_only_delete BEFORE DELETE ON {_TABLE} "
        "BEGIN SELECT RAISE(ABORT, 'search projection seals are append-only'); END"
    )
    op.execute("DROP VIEW IF EXISTS v_search_index_successful")
    op.execute(
        "CREATE VIEW v_search_index_successful AS "
        "SELECT run.* FROM search_index_runs AS run "
        f"JOIN {_TABLE} AS seal ON seal.index_run_id = run.index_run_id "
        "WHERE run.outcome = 'succeeded' "
        "AND NOT EXISTS (SELECT 1 FROM search_index_runs AS newer "
        "WHERE newer.index_key = run.index_key AND newer.revision > run.revision)"
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_search_index_successful")
    op.execute(
        "CREATE VIEW v_search_index_successful AS SELECT * FROM search_index_runs AS run "
        "WHERE outcome = 'succeeded' AND NOT EXISTS (SELECT 1 FROM search_index_runs AS newer "
        "WHERE newer.index_key = run.index_key AND newer.revision > run.revision)"
    )
    for trigger in (
        f"trg_{_TABLE}_append_only_delete",
        f"trg_{_TABLE}_append_only",
        f"trg_{_TABLE}_lexical_coverage",
        f"trg_{_TABLE}_vector_coverage",
        f"trg_{_TABLE}_chunk_count",
        f"trg_{_TABLE}_run_contract",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.drop_index("ix_search_projection_manifest_kind", table_name=_TABLE)
    op.drop_table(_TABLE)
