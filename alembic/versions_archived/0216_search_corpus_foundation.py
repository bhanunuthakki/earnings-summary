"""Add the append-only, evidence-anchored grounded-search foundation.

Search remains a projection over canonical evidence: corpus manifests make
coverage gaps explicit, chunks anchor every retrieval unit to an evidence node,
and index/embedding records preserve the exact software and artifacts used.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0216_search_corpus_foundation"
down_revision: str | Sequence[str] | None = "0215_observation_resolution_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "search_corpus_manifests",
    "search_corpus_document_memberships",
    "search_corpus_manifest_seals",
    "search_chunks",
    "search_embedding_artifacts",
    "search_index_runs",
    "search_index_memberships",
)


def _append_only(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'grounded search ledger is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'grounded search ledger is append-only'); END"
    )


def upgrade() -> None:
    op.create_table(
        "search_corpus_manifests",
        sa.Column("manifest_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("corpus_key", sa.String(256), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("selection_config_sha256", sa.String(64), nullable=False),
        sa.Column("selector_code_version", sa.String(255), nullable=False),
        sa.Column("knowledge_cutoff", sa.DateTime(), nullable=True),
        sa.Column(
            "supersedes_manifest_id",
            sa.String(128),
            sa.ForeignKey("search_corpus_manifests.manifest_id"),
        ),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("corpus_key", "revision", name="uq_search_corpus_manifest_revision"),
        sa.CheckConstraint("revision > 0", name="ck_search_corpus_manifest_revision"),
        sa.CheckConstraint(
            "length(selection_config_sha256) = 64", name="ck_search_corpus_manifest_config"
        ),
    )
    op.create_table(
        "search_corpus_document_memberships",
        sa.Column("membership_id", sa.String(128), primary_key=True),
        sa.Column(
            "manifest_id",
            sa.String(128),
            sa.ForeignKey("search_corpus_manifests.manifest_id"),
            nullable=False,
        ),
        sa.Column("expected_document_key", sa.String(256), nullable=False),
        sa.Column(
            "document_version_id",
            sa.String(128),
            sa.ForeignKey("evidence_document_versions.document_version_id"),
            nullable=True,
        ),
        sa.Column("membership_status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "manifest_id", "expected_document_key", name="uq_search_corpus_expected_document"
        ),
        sa.CheckConstraint(
            "membership_status IN ('included', 'missing', 'quarantined')",
            name="ck_search_corpus_membership_status",
        ),
        sa.CheckConstraint(
            "(membership_status = 'included' AND document_version_id IS NOT NULL) OR "
            "membership_status IN ('missing', 'quarantined')",
            name="ck_search_corpus_membership_document_contract",
        ),
    )
    op.create_index(
        "ix_search_corpus_membership_status",
        "search_corpus_document_memberships",
        ["manifest_id", "membership_status"],
    )
    op.create_table(
        "search_corpus_manifest_seals",
        sa.Column(
            "manifest_id",
            sa.String(128),
            sa.ForeignKey("search_corpus_manifests.manifest_id"),
            primary_key=True,
        ),
        sa.Column("expected_document_count", sa.Integer(), nullable=False),
        sa.Column("membership_digest_sha256", sa.String(64), nullable=False),
        sa.Column("completion_status", sa.String(16), nullable=False),
        sa.Column("sealed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("expected_document_count >= 0", name="ck_search_corpus_seal_count"),
        sa.CheckConstraint(
            "length(membership_digest_sha256) = 64", name="ck_search_corpus_seal_digest"
        ),
        sa.CheckConstraint(
            "completion_status IN ('complete', 'incomplete')", name="ck_search_corpus_seal_status"
        ),
    )
    op.create_table(
        "search_chunks",
        sa.Column("chunk_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "manifest_id",
            sa.String(128),
            sa.ForeignKey("search_corpus_manifests.manifest_id"),
            nullable=False,
        ),
        sa.Column(
            "evidence_node_id",
            sa.String(128),
            sa.ForeignKey("evidence_nodes.node_id"),
            nullable=False,
        ),
        sa.Column("chunk_key", sa.String(256), nullable=False),
        sa.Column("chunk_revision", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("chunker_config_sha256", sa.String(64), nullable=False),
        sa.Column("chunker_code_version", sa.String(255), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "manifest_id", "chunk_key", "chunk_revision", name="uq_search_chunk_revision"
        ),
        sa.CheckConstraint("chunk_revision > 0", name="ck_search_chunk_revision"),
        sa.CheckConstraint(
            "char_start >= 0 AND char_end >= char_start", name="ck_search_chunk_range"
        ),
        sa.CheckConstraint("length(content_sha256) = 64", name="ck_search_chunk_content"),
        sa.CheckConstraint("length(chunker_config_sha256) = 64", name="ck_search_chunk_config"),
    )
    op.create_index(
        "ix_search_chunks_manifest_node", "search_chunks", ["manifest_id", "evidence_node_id"]
    )
    op.create_table(
        "search_embedding_artifacts",
        sa.Column("embedding_artifact_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "index_run_id",
            sa.String(128),
            sa.ForeignKey("search_index_runs.index_run_id"),
            nullable=False,
        ),
        sa.Column(
            "chunk_id", sa.String(128), sa.ForeignKey("search_chunks.chunk_id"), nullable=False
        ),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("vector_sha256", sa.String(64), nullable=True),
        sa.Column("storage_uri", sa.Text(), nullable=True),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("request_config_sha256", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("dimensions > 0", name="ck_search_embedding_dimensions"),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'failed')", name="ck_search_embedding_outcome"
        ),
        sa.CheckConstraint(
            "length(input_sha256) = 64 AND length(request_config_sha256) = 64 "
            "AND ((outcome = 'succeeded' AND length(vector_sha256) = 64 AND storage_uri IS NOT NULL) "
            "OR (outcome = 'failed' AND vector_sha256 IS NULL AND storage_uri IS NULL))",
            name="ck_search_embedding_hashes",
        ),
        sa.CheckConstraint("cost_usd IS NULL OR cost_usd >= 0", name="ck_search_embedding_cost"),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0", name="ck_search_embedding_latency"
        ),
    )
    op.create_index(
        "ix_search_embedding_run_chunk_model",
        "search_embedding_artifacts",
        ["index_run_id", "chunk_id", "provider", "model"],
    )
    op.create_table(
        "search_index_runs",
        sa.Column("index_run_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("index_key", sa.String(256), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "manifest_id",
            sa.String(128),
            sa.ForeignKey("search_corpus_manifests.manifest_id"),
            nullable=False,
        ),
        sa.Column("index_kind", sa.String(16), nullable=False),
        sa.Column("config_sha256", sa.String(64), nullable=False),
        sa.Column("code_version", sa.String(255), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("index_key", "revision", name="uq_search_index_run_revision"),
        sa.CheckConstraint("revision > 0", name="ck_search_index_run_revision"),
        sa.CheckConstraint("index_kind IN ('lexical', 'vector')", name="ck_search_index_kind"),
        sa.CheckConstraint("outcome IN ('succeeded', 'failed')", name="ck_search_index_outcome"),
        sa.CheckConstraint("length(config_sha256) = 64", name="ck_search_index_config"),
    )
    op.create_table(
        "search_index_memberships",
        sa.Column(
            "index_run_id",
            sa.String(128),
            sa.ForeignKey("search_index_runs.index_run_id"),
            primary_key=True,
        ),
        sa.Column(
            "chunk_id", sa.String(128), sa.ForeignKey("search_chunks.chunk_id"), primary_key=True
        ),
        sa.Column("membership_status", sa.String(16), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "membership_status IN ('included', 'missing', 'quarantined', 'failed')",
            name="ck_search_index_membership_status",
        ),
    )
    op.create_index(
        "ix_search_index_membership_status",
        "search_index_memberships",
        ["index_run_id", "membership_status"],
    )

    op.execute(
        "CREATE TRIGGER trg_search_corpus_membership_unsealed BEFORE INSERT ON search_corpus_document_memberships "
        "WHEN EXISTS (SELECT 1 FROM search_corpus_manifest_seals WHERE manifest_id = NEW.manifest_id) "
        "BEGIN SELECT RAISE(ABORT, 'sealed corpus manifest cannot receive memberships'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_corpus_seal_counts BEFORE INSERT ON search_corpus_manifest_seals "
        "WHEN NEW.expected_document_count <> (SELECT COUNT(*) FROM search_corpus_document_memberships "
        "WHERE manifest_id = NEW.manifest_id) OR (NEW.completion_status = 'complete' AND EXISTS "
        "(SELECT 1 FROM search_corpus_document_memberships WHERE manifest_id = NEW.manifest_id "
        "AND membership_status <> 'included')) OR (NEW.completion_status = 'incomplete' AND NOT EXISTS "
        "(SELECT 1 FROM search_corpus_document_memberships WHERE manifest_id = NEW.manifest_id "
        "AND membership_status <> 'included')) BEGIN SELECT RAISE(ABORT, "
        "'corpus seal count or completion status does not match membership'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_chunk_node_text BEFORE INSERT ON search_chunks "
        "WHEN (SELECT text FROM evidence_nodes WHERE node_id = NEW.evidence_node_id) IS NULL "
        "OR substr((SELECT text FROM evidence_nodes WHERE node_id = NEW.evidence_node_id), NEW.char_start + 1, NEW.char_end - NEW.char_start) <> NEW.text "
        "BEGIN SELECT RAISE(ABORT, 'search chunk text must exactly anchor evidence node text'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_chunk_manifest_unsealed BEFORE INSERT ON search_chunks "
        "WHEN EXISTS (SELECT 1 FROM search_corpus_manifest_seals WHERE manifest_id = NEW.manifest_id) "
        "BEGIN SELECT RAISE(ABORT, 'sealed corpus manifest cannot receive chunks'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_chunk_manifest_membership BEFORE INSERT ON search_chunks "
        "WHEN NOT EXISTS (SELECT 1 FROM evidence_nodes AS node "
        "JOIN evidence_extraction_runs AS run ON run.extraction_run_id = node.extraction_run_id "
        "JOIN search_corpus_document_memberships AS membership "
        "ON membership.document_version_id = run.document_version_id "
        "WHERE node.node_id = NEW.evidence_node_id AND membership.manifest_id = NEW.manifest_id "
        "AND membership.membership_status = 'included') "
        "BEGIN SELECT RAISE(ABORT, 'search chunk requires included corpus document membership'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_embedding_input BEFORE INSERT ON search_embedding_artifacts "
        "WHEN (SELECT content_sha256 FROM search_chunks WHERE chunk_id = NEW.chunk_id) <> NEW.input_sha256 "
        "BEGIN SELECT RAISE(ABORT, 'embedding input hash must match search chunk'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_embedding_index_manifest BEFORE INSERT ON search_embedding_artifacts "
        "WHEN (SELECT manifest_id FROM search_index_runs WHERE index_run_id = NEW.index_run_id) <> "
        "(SELECT manifest_id FROM search_chunks WHERE chunk_id = NEW.chunk_id) "
        "BEGIN SELECT RAISE(ABORT, 'embedding artifact must use its index manifest'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_index_membership_manifest BEFORE INSERT ON search_index_memberships "
        "WHEN (SELECT manifest_id FROM search_index_runs WHERE index_run_id = NEW.index_run_id) <> "
        "(SELECT manifest_id FROM search_chunks WHERE chunk_id = NEW.chunk_id) "
        "BEGIN SELECT RAISE(ABORT, 'index membership must use the index manifest'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_manifest_revision_chain BEFORE INSERT ON search_corpus_manifests "
        "WHEN NEW.revision = 1 AND NEW.supersedes_manifest_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'first corpus manifest cannot supersede'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_manifest_revision_parent BEFORE INSERT ON search_corpus_manifests "
        "WHEN NEW.revision > 1 AND (NEW.supersedes_manifest_id IS NULL OR NOT EXISTS "
        "(SELECT 1 FROM search_corpus_manifests WHERE manifest_id = NEW.supersedes_manifest_id "
        "AND corpus_key = NEW.corpus_key AND revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, 'corpus manifest must supersede prior revision'); END"
    )
    for table in _TABLES:
        _append_only(table)
    try:
        op.execute(
            "CREATE VIRTUAL TABLE search_lexical_chunks USING fts5(chunk_id UNINDEXED, text)"
        )
    except Exception as exc:  # pragma: no cover - exercised only on SQLite builds without FTS5
        raise RuntimeError("SQLite FTS5 is required for grounded lexical search") from exc
    op.execute(
        "CREATE TRIGGER trg_search_chunks_fts_after_insert AFTER INSERT ON search_chunks "
        "BEGIN INSERT INTO search_lexical_chunks (chunk_id, text) VALUES (NEW.chunk_id, NEW.text); END"
    )
    op.execute(
        "CREATE VIEW v_search_corpus_coverage AS "
        "SELECT manifest_id, COUNT(*) AS expected_document_count, "
        "SUM(CASE WHEN membership_status = 'included' THEN 1 ELSE 0 END) AS included_document_count, "
        "SUM(CASE WHEN membership_status = 'missing' THEN 1 ELSE 0 END) AS missing_document_count, "
        "SUM(CASE WHEN membership_status = 'quarantined' THEN 1 ELSE 0 END) AS quarantined_document_count "
        "FROM search_corpus_document_memberships GROUP BY manifest_id"
    )
    op.execute(
        "CREATE VIEW v_search_corpus_current AS "
        "SELECT manifest_id, corpus_key, revision, selection_config_sha256, selector_code_version, "
        "knowledge_cutoff, supersedes_manifest_id, recorded_at FROM search_corpus_manifests AS manifest "
        "WHERE NOT EXISTS (SELECT 1 FROM search_corpus_manifests AS newer "
        "WHERE newer.corpus_key = manifest.corpus_key AND newer.revision > manifest.revision)"
    )
    op.execute(
        "CREATE VIEW v_search_index_current AS "
        "SELECT index_run_id, index_key, revision, manifest_id, index_kind, config_sha256, code_version, "
        "outcome, failure_reason, started_at, completed_at FROM search_index_runs AS run "
        "WHERE NOT EXISTS (SELECT 1 FROM search_index_runs AS newer "
        "WHERE newer.index_key = run.index_key AND newer.revision > run.revision)"
    )
    op.execute(
        "CREATE VIEW v_search_index_successful AS SELECT * FROM search_index_runs AS run "
        "WHERE outcome = 'succeeded' AND NOT EXISTS (SELECT 1 FROM search_index_runs AS newer "
        "WHERE newer.index_key = run.index_key AND newer.revision > run.revision)"
    )


def downgrade() -> None:
    for view in (
        "v_search_index_successful",
        "v_search_index_current",
        "v_search_corpus_current",
        "v_search_corpus_coverage",
    ):
        op.execute(f"DROP VIEW IF EXISTS {view}")
    for trigger in (
        "trg_search_chunks_fts_after_insert",
        "trg_search_corpus_membership_unsealed",
        "trg_search_corpus_seal_counts",
        "trg_search_chunk_node_text",
        "trg_search_chunk_manifest_unsealed",
        "trg_search_chunk_manifest_membership",
        "trg_search_embedding_input",
        "trg_search_embedding_index_manifest",
        "trg_search_index_membership_manifest",
        "trg_search_manifest_revision_chain",
        "trg_search_manifest_revision_parent",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.execute("DROP TABLE IF EXISTS search_lexical_chunks")
    for table in _TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table, index in (
        ("search_index_memberships", "ix_search_index_membership_status"),
        ("search_embedding_artifacts", "ix_search_embedding_run_chunk_model"),
        ("search_index_runs", None),
        ("search_chunks", "ix_search_chunks_manifest_node"),
        ("search_corpus_manifest_seals", None),
        ("search_corpus_document_memberships", "ix_search_corpus_membership_status"),
        ("search_corpus_manifests", None),
    ):
        if table in existing:
            if index is not None:
                op.drop_index(index, table_name=table)
            op.drop_table(table)
