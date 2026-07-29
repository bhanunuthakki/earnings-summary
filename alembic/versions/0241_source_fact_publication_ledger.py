"""Seal source-fact publications as immutable ordered evidence graphs.

Revision ID: 0241_source_fact_publication_ledger
Revises: 0240_fact_plane_v2_hardening
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0241_source_fact_publication_ledger"
down_revision: str | Sequence[str] | None = "0240_fact_plane_v2_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "source_fact_publications",
    "source_fact_publication_members",
    "source_fact_publication_seals",
)
_KINDS = (
    "fact_cell",
    "fact_observation",
    "observation_relation",
    "derivation_seal",
    "extraction_seal",
    "resolution_revision",
)


def _hex_check(column: str) -> str:
    return (
        f"length({column}) = 64 AND "
        f"{column} NOT GLOB '*[^0-9a-f]*'"
    )


def _append_only(table: str, label: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{label} is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{label} is append-only'); END"
    )


def upgrade() -> None:
    bind = op.get_bind()
    required = {
        "fact_cells_v2",
        "fact_cell_identity_seals_v2",
        "fact_observations_v2",
        "fact_observation_payload_commitments_v2",
        "fact_observation_relations_v2",
        "fact_derivation_seals_v2",
        "fact_derivation_basis_commitments_v2",
        "fact_extraction_run_completeness_seals_v2",
        "fact_resolution_revisions_v2",
    }
    missing = sorted(required - set(sa.inspect(bind).get_table_names()))
    if missing:
        raise RuntimeError(
            "source-fact publication ledger requires the hardened fact plane: "
            + ", ".join(missing)
        )

    op.create_table(
        "source_fact_publications",
        sa.Column("publication_id", sa.String(128), primary_key=True),
        sa.Column(
            "idempotency_key",
            sa.String(256),
            nullable=False,
            unique=True,
        ),
        sa.Column("payload_version", sa.String(64), nullable=False),
        sa.Column("canonical_publication_payload_json", sa.Text(), nullable=False),
        sa.Column("publication_payload_sha256", sa.String(64), nullable=False),
        sa.Column("member_set_sha256", sa.String(64), nullable=False),
        sa.Column("cell_count", sa.Integer(), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("relation_count", sa.Integer(), nullable=False),
        sa.Column("derivation_seal_count", sa.Integer(), nullable=False),
        sa.Column("extraction_seal_count", sa.Integer(), nullable=False),
        sa.Column("resolution_revision_count", sa.Integer(), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "payload_version = 'source_fact_publication.v1'",
            name="ck_source_fact_publication_version",
        ),
        sa.CheckConstraint(
            "json_valid(canonical_publication_payload_json) "
            "AND json_type(canonical_publication_payload_json) = 'object'",
            name="ck_source_fact_publication_json",
        ),
        sa.CheckConstraint(
            _hex_check("publication_payload_sha256")
            + " AND "
            + _hex_check("member_set_sha256"),
            name="ck_source_fact_publication_hashes",
        ),
        sa.CheckConstraint(
            "cell_count >= 0 AND observation_count >= 0 "
            "AND relation_count >= 0 AND derivation_seal_count >= 0 "
            "AND extraction_seal_count >= 0 "
            "AND resolution_revision_count >= 0 "
            "AND member_count = cell_count + observation_count "
            "+ relation_count + derivation_seal_count "
            "+ extraction_seal_count + resolution_revision_count",
            name="ck_source_fact_publication_counts",
        ),
        sa.CheckConstraint(
            "recorded_at >= created_at",
            name="ck_source_fact_publication_clocks",
        ),
    )

    op.create_table(
        "source_fact_publication_members",
        sa.Column("publication_member_id", sa.String(128), primary_key=True),
        sa.Column(
            "idempotency_key",
            sa.String(256),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "publication_id",
            sa.String(128),
            sa.ForeignKey("source_fact_publications.publication_id"),
            nullable=False,
        ),
        sa.Column("member_ordinal", sa.Integer(), nullable=False),
        sa.Column("record_kind", sa.String(32), nullable=False),
        sa.Column("record_id", sa.String(128), nullable=False),
        sa.Column(
            "record_idempotency_key",
            sa.String(256),
            nullable=False,
        ),
        sa.Column("record_commitment_version", sa.String(64), nullable=False),
        sa.Column("record_commitment_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_member_json", sa.Text(), nullable=False),
        sa.Column("canonical_member_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "publication_id",
            "member_ordinal",
            name="uq_source_fact_publication_member_ordinal",
        ),
        sa.UniqueConstraint(
            "publication_id",
            "record_kind",
            "record_id",
            name="uq_source_fact_publication_member_record",
        ),
        sa.CheckConstraint(
            "member_ordinal >= 0",
            name="ck_source_fact_publication_member_ordinal",
        ),
        sa.CheckConstraint(
            "record_kind IN (" + ",".join(f"'{kind}'" for kind in _KINDS) + ")",
            name="ck_source_fact_publication_member_kind",
        ),
        sa.CheckConstraint(
            "record_commitment_version = 'source_fact_record_commitment.v1'",
            name="ck_source_fact_publication_member_commitment_version",
        ),
        sa.CheckConstraint(
            _hex_check("record_commitment_sha256")
            + " AND "
            + _hex_check("canonical_member_sha256"),
            name="ck_source_fact_publication_member_hashes",
        ),
        sa.CheckConstraint(
            "json_valid(canonical_member_json) "
            "AND json_type(canonical_member_json) = 'object'",
            name="ck_source_fact_publication_member_json",
        ),
    )
    op.create_index(
        "ix_source_fact_publication_members_order",
        "source_fact_publication_members",
        ["publication_id", "member_ordinal"],
        unique=True,
    )

    op.create_table(
        "source_fact_publication_seals",
        sa.Column("publication_seal_id", sa.String(128), primary_key=True),
        sa.Column(
            "idempotency_key",
            sa.String(256),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "publication_id",
            sa.String(128),
            sa.ForeignKey("source_fact_publications.publication_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("canonical_member_set_json", sa.Text(), nullable=False),
        sa.Column("member_set_sha256", sa.String(64), nullable=False),
        sa.Column("publication_payload_sha256", sa.String(64), nullable=False),
        sa.Column("sealed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "member_count >= 0",
            name="ck_source_fact_publication_seal_count",
        ),
        sa.CheckConstraint(
            "json_valid(canonical_member_set_json) "
            "AND json_type(canonical_member_set_json) = 'array'",
            name="ck_source_fact_publication_seal_json",
        ),
        sa.CheckConstraint(
            _hex_check("member_set_sha256")
            + " AND "
            + _hex_check("publication_payload_sha256"),
            name="ck_source_fact_publication_seal_hashes",
        ),
    )

    op.execute(
        "CREATE TRIGGER trg_source_fact_publications_exact "
        "BEFORE INSERT ON source_fact_publications WHEN "
        "NEW.canonical_publication_payload_json <> "
        "source_fact_publication_payload_v1("
        "NEW.publication_id, NEW.idempotency_key, NEW.member_set_sha256, "
        "NEW.cell_count, NEW.observation_count, NEW.relation_count, "
        "NEW.derivation_seal_count, NEW.extraction_seal_count, "
        "NEW.resolution_revision_count, NEW.member_count, "
        "NEW.created_at, NEW.recorded_at) "
        "OR NEW.publication_payload_sha256 <> "
        "fact_sha256(NEW.canonical_publication_payload_json) "
        "BEGIN SELECT RAISE(ABORT, "
        "'source-fact publication payload mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_source_fact_publication_members_unsealed "
        "BEFORE INSERT ON source_fact_publication_members WHEN EXISTS ("
        "SELECT 1 FROM source_fact_publication_seals AS seal "
        "WHERE seal.publication_id = NEW.publication_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'source-fact publication is already sealed'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_source_fact_publication_members_exact "
        "BEFORE INSERT ON source_fact_publication_members WHEN "
        "NEW.canonical_member_json <> json_object("
        "'member_ordinal', NEW.member_ordinal, "
        "'record_commitment_sha256', NEW.record_commitment_sha256, "
        "'record_commitment_version', NEW.record_commitment_version, "
        "'record_id', NEW.record_id, "
        "'record_idempotency_key', NEW.record_idempotency_key, "
        "'record_kind', NEW.record_kind) "
        "OR NEW.canonical_member_sha256 <> fact_sha256(NEW.canonical_member_json) "
        "OR NOT EXISTS (SELECT 1 FROM source_fact_publications AS publication "
        "WHERE publication.publication_id = NEW.publication_id "
        "AND NEW.recorded_at <= publication.recorded_at) "
        "BEGIN SELECT RAISE(ABORT, "
        "'source-fact publication member mismatch'); END"
    )
    existence_checks = (
        (
            "fact_cell",
            "fact_cells_v2",
            "fact_cell_id",
        ),
        (
            "fact_observation",
            "fact_observations_v2",
            "observation_id",
        ),
        (
            "observation_relation",
            "fact_observation_relations_v2",
            "relation_id",
        ),
        (
            "derivation_seal",
            "fact_derivation_seals_v2",
            "derivation_seal_id",
        ),
        (
            "extraction_seal",
            "fact_extraction_run_completeness_seals_v2",
            "extraction_seal_id",
        ),
        (
            "resolution_revision",
            "fact_resolution_revisions_v2",
            "resolution_revision_id",
        ),
    )
    missing_record = " OR ".join(
        "("
        f"NEW.record_kind = '{kind}' AND NOT EXISTS ("
        f"SELECT 1 FROM {table} AS record "
        f"WHERE record.{identifier} = NEW.record_id "
        "AND record.idempotency_key = NEW.record_idempotency_key)"
        ")"
        for kind, table, identifier in existence_checks
    )
    op.execute(
        "CREATE TRIGGER trg_source_fact_publication_members_record_exists "
        "BEFORE INSERT ON source_fact_publication_members WHEN "
        + missing_record
        + " BEGIN SELECT RAISE(ABORT, "
        "'source-fact publication member record does not exist'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_source_fact_publication_seals_exact "
        "BEFORE INSERT ON source_fact_publication_seals WHEN "
        "NOT EXISTS (SELECT 1 FROM source_fact_publications AS publication "
        "WHERE publication.publication_id = NEW.publication_id "
        "AND publication.member_count = NEW.member_count "
        "AND publication.member_set_sha256 = NEW.member_set_sha256 "
        "AND publication.publication_payload_sha256 = "
        "NEW.publication_payload_sha256 "
        "AND publication.recorded_at = NEW.sealed_at "
        "AND publication.cell_count = (SELECT COUNT(*) "
        "FROM source_fact_publication_members AS member "
        "WHERE member.publication_id = NEW.publication_id "
        "AND member.record_kind = 'fact_cell') "
        "AND publication.observation_count = (SELECT COUNT(*) "
        "FROM source_fact_publication_members AS member "
        "WHERE member.publication_id = NEW.publication_id "
        "AND member.record_kind = 'fact_observation') "
        "AND publication.relation_count = (SELECT COUNT(*) "
        "FROM source_fact_publication_members AS member "
        "WHERE member.publication_id = NEW.publication_id "
        "AND member.record_kind = 'observation_relation') "
        "AND publication.derivation_seal_count = (SELECT COUNT(*) "
        "FROM source_fact_publication_members AS member "
        "WHERE member.publication_id = NEW.publication_id "
        "AND member.record_kind = 'derivation_seal') "
        "AND publication.extraction_seal_count = (SELECT COUNT(*) "
        "FROM source_fact_publication_members AS member "
        "WHERE member.publication_id = NEW.publication_id "
        "AND member.record_kind = 'extraction_seal') "
        "AND publication.resolution_revision_count = (SELECT COUNT(*) "
        "FROM source_fact_publication_members AS member "
        "WHERE member.publication_id = NEW.publication_id "
        "AND member.record_kind = 'resolution_revision')) "
        "OR NEW.member_count <> (SELECT COUNT(*) "
        "FROM source_fact_publication_members AS member "
        "WHERE member.publication_id = NEW.publication_id) "
        "OR (NEW.member_count > 0 AND ("
        "(SELECT MIN(member_ordinal) FROM source_fact_publication_members "
        "WHERE publication_id = NEW.publication_id) <> 0 OR "
        "(SELECT MAX(member_ordinal) FROM source_fact_publication_members "
        "WHERE publication_id = NEW.publication_id) <> NEW.member_count - 1)) "
        "OR NEW.canonical_member_set_json <> COALESCE((SELECT "
        "json_group_array(json(ordered.canonical_member_json)) FROM ("
        "SELECT member.canonical_member_json "
        "FROM source_fact_publication_members AS member "
        "WHERE member.publication_id = NEW.publication_id "
        "ORDER BY member.member_ordinal) AS ordered), '[]') "
        "OR NEW.member_set_sha256 <> fact_sha256(NEW.canonical_member_set_json) "
        "BEGIN SELECT RAISE(ABORT, "
        "'source-fact publication seal mismatch'); END"
    )

    for table, label in (
        ("source_fact_publications", "source-fact publication"),
        ("source_fact_publication_members", "source-fact publication member"),
        ("source_fact_publication_seals", "source-fact publication seal"),
    ):
        _append_only(table, label)

    op.execute(
        "CREATE VIEW v_source_fact_publications_sealed AS "
        "SELECT publication.*, seal.publication_seal_id, "
        "seal.canonical_member_set_json, seal.sealed_at "
        "FROM source_fact_publications AS publication "
        "JOIN source_fact_publication_seals AS seal "
        "ON seal.publication_id = publication.publication_id"
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_source_fact_publications_sealed")
    for trigger in (
        "trg_source_fact_publication_seals_exact",
        "trg_source_fact_publication_members_record_exists",
        "trg_source_fact_publication_members_exact",
        "trg_source_fact_publication_members_unsealed",
        "trg_source_fact_publications_exact",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
        op.drop_table(table)
