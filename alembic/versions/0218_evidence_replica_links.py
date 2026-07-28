"""Record immutable evidence replicas and document retrieval links.

0213 stored one location and one primary observation directly on each immutable
blob/document row.  Those fields remain valid legacy anchors; this migration
adds the history needed when the same bytes are mirrored, disappear, are
quarantined, or are retrieved again from a distinct authoritative source.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0218_evidence_replica_links"
down_revision: str | Sequence[str] | None = "0217_fact_selection_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("evidence_blob_location_observations", "evidence_document_observation_links")


def _append_only(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'evidence replica ledger is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'evidence replica ledger is append-only'); END"
    )


def _create_document_current_view(existing: set[str]) -> None:
    op.execute("DROP VIEW IF EXISTS v_evidence_document_current")
    if "evidence_document_versions" not in existing:
        op.execute(
            "CREATE VIEW v_evidence_document_current AS "
            "SELECT CAST(NULL AS TEXT) AS document_version_id, CAST(NULL AS TEXT) AS document_key, "
            "CAST(NULL AS INTEGER) AS version_sequence WHERE 0"
        )
        return
    op.execute(
        "CREATE VIEW v_evidence_document_current AS "
        "SELECT document_version_id, document_key, version_sequence, observation_id, blob_sha256, "
        "issuer_id, ticker, document_type, form_type, accession_number, exhibit_id, period_start, "
        "period_end, as_of_at, language, replaces_document_version_id, legacy_document_id, recorded_at "
        "FROM evidence_document_versions AS document_version WHERE NOT EXISTS "
        "(SELECT 1 FROM evidence_document_versions AS newer "
        "WHERE newer.document_key = document_version.document_key "
        "AND newer.version_sequence > document_version.version_sequence)"
    )


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "evidence_blob_location_observations" not in existing:
        op.create_table(
            "evidence_blob_location_observations",
            sa.Column("location_observation_id", sa.String(128), primary_key=True),
            sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
            sa.Column(
                "blob_sha256",
                sa.String(64),
                sa.ForeignKey("evidence_content_blobs.sha256"),
                nullable=False,
            ),
            sa.Column("storage_uri", sa.Text(), nullable=False),
            sa.Column("location_kind", sa.String(16), nullable=False),
            sa.Column("availability_state", sa.String(16), nullable=False),
            sa.Column("location_sequence", sa.Integer(), nullable=False),
            sa.Column("verified_at", sa.DateTime(), nullable=False),
            sa.Column("verified_byte_size", sa.Integer(), nullable=True),
            sa.Column("verified_sha256", sa.String(64), nullable=True),
            sa.Column(
                "supersedes_location_observation_id",
                sa.String(128),
                sa.ForeignKey("evidence_blob_location_observations.location_observation_id"),
                nullable=True,
            ),
            sa.Column("recorded_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "blob_sha256", "storage_uri", "location_sequence", name="uq_evidence_location_revision"
            ),
            sa.CheckConstraint("location_sequence > 0", name="ck_evidence_location_revision"),
            sa.CheckConstraint(
                "location_kind IN ('local', 'object', 'archive', 'mirror')",
                name="ck_evidence_location_kind",
            ),
            sa.CheckConstraint(
                "availability_state IN ('present', 'missing', 'quarantined')",
                name="ck_evidence_location_availability",
            ),
            sa.CheckConstraint(
                "verified_byte_size IS NULL OR verified_byte_size >= 0",
                name="ck_evidence_location_verified_size",
            ),
            sa.CheckConstraint(
                "verified_sha256 IS NULL OR length(verified_sha256) = 64",
                name="ck_evidence_location_verified_hash",
            ),
            sa.CheckConstraint(
                "recorded_at >= verified_at", name="ck_evidence_location_clock_order"
            ),
        )
        op.create_index(
            "ix_evidence_location_current_lookup",
            "evidence_blob_location_observations",
            ["blob_sha256", "storage_uri", "location_sequence"],
        )
    if "evidence_document_observation_links" not in existing:
        op.create_table(
            "evidence_document_observation_links",
            sa.Column("link_id", sa.String(128), primary_key=True),
            sa.Column(
                "document_version_id",
                sa.String(128),
                sa.ForeignKey("evidence_document_versions.document_version_id"),
                nullable=False,
            ),
            sa.Column(
                "observation_id",
                sa.String(128),
                sa.ForeignKey("evidence_source_observations.observation_id"),
                nullable=False,
            ),
            sa.Column("link_kind", sa.String(16), nullable=False),
            sa.Column("linked_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "document_version_id", "observation_id", name="uq_evidence_document_observation_link"
            ),
            sa.CheckConstraint(
                "link_kind IN ('primary', 'retrieval', 'mirror')",
                name="ck_evidence_document_observation_link_kind",
            ),
        )
        op.create_index(
            "ix_evidence_document_observation_links_observation",
            "evidence_document_observation_links",
            ["observation_id", "document_version_id"],
        )
        op.execute(
            "CREATE UNIQUE INDEX uq_evidence_document_primary_link "
            "ON evidence_document_observation_links(document_version_id) WHERE link_kind = 'primary'"
        )

    op.execute(
        "CREATE TRIGGER trg_evidence_location_verified_hash BEFORE INSERT ON evidence_blob_location_observations "
        "WHEN NEW.verified_sha256 IS NOT NULL AND NEW.verified_sha256 <> NEW.blob_sha256 "
        "BEGIN SELECT RAISE(ABORT, 'location verification hash must match blob'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_evidence_location_verified_size BEFORE INSERT ON evidence_blob_location_observations "
        "WHEN NEW.verified_byte_size IS NOT NULL AND (SELECT byte_size FROM evidence_content_blobs "
        "WHERE sha256 = NEW.blob_sha256) <> NEW.verified_byte_size "
        "BEGIN SELECT RAISE(ABORT, 'location verification size must match blob'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_evidence_location_revision_chain BEFORE INSERT ON evidence_blob_location_observations "
        "WHEN (NEW.location_sequence = 1 AND NEW.supersedes_location_observation_id IS NOT NULL) "
        "OR (NEW.location_sequence > 1 AND NOT EXISTS (SELECT 1 FROM evidence_blob_location_observations "
        "WHERE location_observation_id = NEW.supersedes_location_observation_id "
        "AND blob_sha256 = NEW.blob_sha256 AND storage_uri = NEW.storage_uri "
        "AND location_sequence = NEW.location_sequence - 1)) "
        "BEGIN SELECT RAISE(ABORT, 'location revision must supersede the prior location revision'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_evidence_document_link_same_blob BEFORE INSERT ON evidence_document_observation_links "
        "WHEN (SELECT blob_sha256 FROM evidence_document_versions "
        "WHERE document_version_id = NEW.document_version_id) <> "
        "(SELECT blob_sha256 FROM evidence_source_observations WHERE observation_id = NEW.observation_id) "
        "BEGIN SELECT RAISE(ABORT, 'document observation link must use the same blob'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_evidence_document_link_primary BEFORE INSERT ON evidence_document_observation_links "
        "WHEN NEW.link_kind = 'primary' AND (SELECT observation_id FROM evidence_document_versions "
        "WHERE document_version_id = NEW.document_version_id) <> NEW.observation_id "
        "BEGIN SELECT RAISE(ABORT, 'primary link must match document primary observation'); END"
    )
    for table in _TABLES:
        _append_only(table)

    existing = set(sa.inspect(bind).get_table_names())
    if {"evidence_content_blobs", "evidence_blob_location_observations"} <= existing:
        op.execute(
            "INSERT OR IGNORE INTO evidence_blob_location_observations "
            "(location_observation_id, idempotency_key, blob_sha256, storage_uri, location_kind, "
            "availability_state, location_sequence, verified_at, verified_byte_size, verified_sha256, "
            "supersedes_location_observation_id, recorded_at) "
            "SELECT 'legacy-location:' || sha256, 'legacy-location:' || sha256, sha256, storage_uri, "
            "'local', 'present', 1, recorded_at, byte_size, sha256, NULL, recorded_at "
            "FROM evidence_content_blobs"
        )
    if {
        "evidence_document_versions",
        "evidence_source_observations",
        "evidence_document_observation_links",
    } <= existing:
        op.execute(
            "INSERT OR IGNORE INTO evidence_document_observation_links "
            "(link_id, document_version_id, observation_id, link_kind, linked_at) "
            "SELECT 'legacy-primary-link:' || document_version_id, document_version_id, "
            "document_version.observation_id, 'primary', source_observation.retrieved_at "
            "FROM evidence_document_versions AS document_version "
            "JOIN evidence_source_observations AS source_observation "
            "ON source_observation.observation_id = document_version.observation_id"
        )
    _create_document_current_view(existing)
    op.execute("DROP VIEW IF EXISTS v_evidence_blob_locations_current")
    op.execute(
        "CREATE VIEW v_evidence_blob_locations_current AS "
        "SELECT location_observation_id, idempotency_key, blob_sha256, storage_uri, location_kind, "
        "availability_state, location_sequence, verified_at, verified_byte_size, verified_sha256, "
        "supersedes_location_observation_id, recorded_at "
        "FROM evidence_blob_location_observations AS location_observation WHERE NOT EXISTS "
        "(SELECT 1 FROM evidence_blob_location_observations AS newer "
        "WHERE newer.blob_sha256 = location_observation.blob_sha256 "
        "AND newer.storage_uri = location_observation.storage_uri "
        "AND newer.location_sequence > location_observation.location_sequence)"
    )


def downgrade() -> None:
    for view in ("v_evidence_blob_locations_current", "v_evidence_document_current"):
        op.execute(f"DROP VIEW IF EXISTS {view}")
    for trigger in (
        "trg_evidence_location_verified_hash",
        "trg_evidence_location_verified_size",
        "trg_evidence_location_revision_chain",
        "trg_evidence_document_link_same_blob",
        "trg_evidence_document_link_primary",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in _TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "evidence_document_observation_links" in existing:
        op.execute("DROP INDEX IF EXISTS uq_evidence_document_primary_link")
        op.drop_index(
            "ix_evidence_document_observation_links_observation",
            table_name="evidence_document_observation_links",
        )
        op.drop_table("evidence_document_observation_links")
    if "evidence_blob_location_observations" in existing:
        op.drop_index(
            "ix_evidence_location_current_lookup",
            table_name="evidence_blob_location_observations",
        )
        op.drop_table("evidence_blob_location_observations")
