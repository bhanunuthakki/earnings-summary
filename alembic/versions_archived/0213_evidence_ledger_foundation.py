"""Add the canonical, append-only evidence-ledger foundation.

The new relations deliberately coexist with the historical ``documents``
table.  This establishes immutable raw bytes, retrieval observations, logical
document versions, deterministic extraction runs, and revisioned evidence
nodes before any existing writer begins a dual write.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0213_evidence_ledger_foundation"
down_revision: str | Sequence[str] | None = "0213_decision_draft_provider_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "evidence_content_blobs",
    "evidence_source_observations",
    "evidence_document_versions",
    "evidence_extraction_runs",
    "evidence_nodes",
)
_IMMUTABLE_TRIGGERS = tuple(f"trg_{table}_append_only" for table in _TABLES)


def _append_only_triggers(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'evidence ledger is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'evidence ledger is append-only'); END"
    )


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "evidence_content_blobs" not in existing:
        op.create_table(
            "evidence_content_blobs",
            sa.Column("sha256", sa.String(length=64), primary_key=True),
            sa.Column("byte_size", sa.Integer(), nullable=False),
            sa.Column("media_type", sa.String(length=255), nullable=False),
            sa.Column("storage_uri", sa.Text(), nullable=False),
            sa.Column("recorded_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint("byte_size >= 0", name="ck_evidence_content_blobs_size"),
            sa.CheckConstraint("length(sha256) = 64", name="ck_evidence_content_blobs_sha256"),
        )
    if "evidence_source_observations" not in existing:
        op.create_table(
            "evidence_source_observations",
            sa.Column("observation_id", sa.String(length=128), primary_key=True),
            sa.Column("idempotency_key", sa.String(length=256), nullable=False, unique=True),
            sa.Column("source_kind", sa.String(length=64), nullable=False),
            sa.Column("source_url", sa.Text(), nullable=False),
            sa.Column(
                "blob_sha256",
                sa.String(length=64),
                sa.ForeignKey("evidence_content_blobs.sha256"),
                nullable=False,
            ),
            sa.Column("source_published_at", sa.DateTime(), nullable=True),
            sa.Column("filing_at", sa.DateTime(), nullable=True),
            sa.Column("accepted_at", sa.DateTime(), nullable=True),
            sa.Column("observed_at", sa.DateTime(), nullable=False),
            sa.Column("retrieved_at", sa.DateTime(), nullable=False),
            sa.Column("retrieval_config_sha256", sa.String(length=64), nullable=False),
            sa.Column("collector_code_version", sa.String(length=255), nullable=False),
            sa.CheckConstraint("length(blob_sha256) = 64", name="ck_evidence_observation_blob_sha"),
            sa.CheckConstraint(
                "length(retrieval_config_sha256) = 64", name="ck_evidence_observation_config_sha"
            ),
            sa.CheckConstraint(
                "retrieved_at >= observed_at", name="ck_evidence_observation_clock_order"
            ),
        )
        op.create_index(
            "ix_evidence_observations_blob_retrieved",
            "evidence_source_observations",
            ["blob_sha256", "retrieved_at"],
        )
    if "evidence_document_versions" not in existing:
        op.create_table(
            "evidence_document_versions",
            sa.Column("document_version_id", sa.String(length=128), primary_key=True),
            sa.Column("document_key", sa.String(length=256), nullable=False),
            sa.Column("version_sequence", sa.Integer(), nullable=False),
            sa.Column(
                "observation_id",
                sa.String(length=128),
                sa.ForeignKey("evidence_source_observations.observation_id"),
                nullable=False,
            ),
            sa.Column(
                "blob_sha256",
                sa.String(length=64),
                sa.ForeignKey("evidence_content_blobs.sha256"),
                nullable=False,
            ),
            sa.Column("issuer_id", sa.String(length=128), nullable=False),
            sa.Column("ticker", sa.String(length=16), nullable=True),
            sa.Column("document_type", sa.String(length=64), nullable=False),
            sa.Column("form_type", sa.String(length=64), nullable=False),
            sa.Column("accession_number", sa.String(length=64), nullable=True),
            sa.Column("exhibit_id", sa.String(length=128), nullable=True),
            sa.Column("period_start", sa.DateTime(), nullable=True),
            sa.Column("period_end", sa.DateTime(), nullable=True),
            sa.Column("as_of_at", sa.DateTime(), nullable=True),
            sa.Column("language", sa.String(length=32), nullable=False),
            sa.Column(
                "replaces_document_version_id",
                sa.String(length=128),
                sa.ForeignKey("evidence_document_versions.document_version_id"),
                nullable=True,
            ),
            # ``documents`` is absent in stamped synthetic schemas, so this is
            # a nullable indexed bridge validated by EvidenceLedger at write
            # time instead of a conditional FK.
            sa.Column("legacy_document_id", sa.Integer(), nullable=True),
            sa.Column("recorded_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("document_key", "version_sequence", name="uq_evidence_document_version"),
            sa.UniqueConstraint("document_key", "blob_sha256", name="uq_evidence_document_blob"),
            sa.UniqueConstraint("legacy_document_id", name="uq_evidence_document_legacy_document"),
            sa.CheckConstraint("version_sequence > 0", name="ck_evidence_document_version_positive"),
            sa.CheckConstraint(
                "period_start IS NULL OR period_end IS NULL OR period_end >= period_start",
                name="ck_evidence_document_period_order",
            ),
        )
        op.create_index(
            "ix_evidence_document_filter",
            "evidence_document_versions",
            ["issuer_id", "ticker", "form_type", "period_end", "as_of_at"],
        )
    if "evidence_extraction_runs" not in existing:
        op.create_table(
            "evidence_extraction_runs",
            sa.Column("extraction_run_id", sa.String(length=128), primary_key=True),
            sa.Column("idempotency_key", sa.String(length=256), nullable=False, unique=True),
            sa.Column(
                "document_version_id",
                sa.String(length=128),
                sa.ForeignKey("evidence_document_versions.document_version_id"),
                nullable=False,
            ),
            sa.Column("input_sha256", sa.String(length=64), nullable=False),
            sa.Column("extractor_name", sa.String(length=128), nullable=False),
            sa.Column("extractor_config_sha256", sa.String(length=64), nullable=False),
            sa.Column("extractor_code_version", sa.String(length=255), nullable=False),
            sa.Column("output_sha256", sa.String(length=64), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=False),
            sa.Column("outcome", sa.String(length=16), nullable=False),
            sa.UniqueConstraint(
                "document_version_id",
                "input_sha256",
                "extractor_name",
                "extractor_config_sha256",
                "extractor_code_version",
                name="uq_evidence_extraction_semantic_run",
            ),
            sa.CheckConstraint(
                "outcome IN ('succeeded', 'failed')", name="ck_evidence_extraction_outcome"
            ),
            sa.CheckConstraint(
                "completed_at >= started_at", name="ck_evidence_extraction_clock_order"
            ),
        )
    if "evidence_nodes" not in existing:
        op.create_table(
            "evidence_nodes",
            sa.Column("node_id", sa.String(length=128), primary_key=True),
            sa.Column("evidence_key", sa.String(length=256), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column(
                "extraction_run_id",
                sa.String(length=128),
                sa.ForeignKey("evidence_extraction_runs.extraction_run_id"),
                nullable=False,
            ),
            sa.Column(
                "parent_node_id",
                sa.String(length=128),
                sa.ForeignKey("evidence_nodes.node_id"),
                nullable=True,
            ),
            sa.Column(
                "supersedes_node_id",
                sa.String(length=128),
                sa.ForeignKey("evidence_nodes.node_id"),
                nullable=True,
            ),
            sa.Column("node_kind", sa.String(length=32), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("locator_json", sa.Text(), nullable=True),
            sa.Column("locator_sha256", sa.String(length=64), nullable=True),
            sa.Column("recorded_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("evidence_key", "revision", name="uq_evidence_node_revision"),
            sa.CheckConstraint("revision > 0", name="ck_evidence_node_revision_positive"),
            sa.CheckConstraint(
                "node_kind IN ('document', 'section', 'passage', 'table', 'table_row', "
                "'table_cell', 'pdf_page', 'transcript_turn', 'claim')",
                name="ck_evidence_node_kind",
            ),
        )
        op.create_index("ix_evidence_nodes_key_revision", "evidence_nodes", ["evidence_key", "revision"])

    # Cross-row identity is not expressible as a SQLite FK: a document version
    # and its extraction run must carry the exact bytes their parents identify.
    op.execute(
        "CREATE TRIGGER trg_evidence_document_observation_blob BEFORE INSERT ON evidence_document_versions "
        "WHEN (SELECT blob_sha256 FROM evidence_source_observations "
        "WHERE observation_id = NEW.observation_id) <> NEW.blob_sha256 "
        "BEGIN SELECT RAISE(ABORT, 'document version blob must match source observation'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_evidence_extraction_input_blob BEFORE INSERT ON evidence_extraction_runs "
        "WHEN (SELECT blob_sha256 FROM evidence_document_versions "
        "WHERE document_version_id = NEW.document_version_id) <> NEW.input_sha256 "
        "BEGIN SELECT RAISE(ABORT, 'extraction input hash must match document version blob'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_evidence_document_replacement_chain BEFORE INSERT ON evidence_document_versions "
        "WHEN NEW.replaces_document_version_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM evidence_document_versions WHERE document_version_id = NEW.replaces_document_version_id "
        "AND document_key = NEW.document_key AND version_sequence = NEW.version_sequence - 1) "
        "BEGIN SELECT RAISE(ABORT, 'document replacement must follow the prior version'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_evidence_node_revision_chain BEFORE INSERT ON evidence_nodes "
        "WHEN NEW.revision = 1 AND NEW.supersedes_node_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'first evidence revision cannot supersede'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_evidence_node_revision_parent BEFORE INSERT ON evidence_nodes "
        "WHEN NEW.revision > 1 AND (NEW.supersedes_node_id IS NULL OR NOT EXISTS "
        "(SELECT 1 FROM evidence_nodes WHERE node_id = NEW.supersedes_node_id "
        "AND evidence_key = NEW.evidence_key AND revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, 'evidence revision must supersede the previous revision'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_evidence_node_parent_run BEFORE INSERT ON evidence_nodes "
        "WHEN NEW.parent_node_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM evidence_nodes WHERE node_id = NEW.parent_node_id "
        "AND extraction_run_id = NEW.extraction_run_id) "
        "BEGIN SELECT RAISE(ABORT, 'evidence parent must share extraction run'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_evidence_node_succeeded_run BEFORE INSERT ON evidence_nodes "
        "WHEN (SELECT outcome FROM evidence_extraction_runs "
        "WHERE extraction_run_id = NEW.extraction_run_id) <> 'succeeded' "
        "BEGIN SELECT RAISE(ABORT, 'evidence nodes require a succeeded extraction'); END"
    )
    for table in _TABLES:
        _append_only_triggers(table)
    op.execute(
        "CREATE VIEW v_evidence_current AS "
        "SELECT node_id, evidence_key, revision, extraction_run_id, parent_node_id, supersedes_node_id, "
        "node_kind, text, locator_json, locator_sha256, recorded_at FROM evidence_nodes AS node "
        "WHERE NOT EXISTS (SELECT 1 FROM evidence_nodes AS newer "
        "WHERE newer.evidence_key = node.evidence_key AND newer.revision > node.revision)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    op.execute("DROP VIEW IF EXISTS v_evidence_current")
    for trigger in (
        "trg_evidence_node_succeeded_run",
        "trg_evidence_node_parent_run",
        "trg_evidence_node_revision_parent",
        "trg_evidence_node_revision_chain",
        "trg_evidence_extraction_input_blob",
        "trg_evidence_document_observation_blob",
        "trg_evidence_document_replacement_chain",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for trigger in _IMMUTABLE_TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}_delete")
    if "evidence_nodes" in existing:
        op.drop_index("ix_evidence_nodes_key_revision", table_name="evidence_nodes")
        op.drop_table("evidence_nodes")
    if "evidence_extraction_runs" in existing:
        op.drop_table("evidence_extraction_runs")
    if "evidence_document_versions" in existing:
        op.drop_index("ix_evidence_document_filter", table_name="evidence_document_versions")
        op.drop_table("evidence_document_versions")
    if "evidence_source_observations" in existing:
        op.drop_index("ix_evidence_observations_blob_retrieved", table_name="evidence_source_observations")
        op.drop_table("evidence_source_observations")
    if "evidence_content_blobs" in existing:
        op.drop_table("evidence_content_blobs")
