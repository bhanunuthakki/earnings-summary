"""Add obligation-complete document-processing and research snapshots.

Revision ID: 0245_document_processing_research_snapshots
Revises: 0244_canonical_fact_resolution
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0245_document_processing_research_snapshots"
down_revision: str | Sequence[str] | None = "0244_canonical_fact_resolution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "document_processing_obligation_revisions",
    "document_processing_disposition_headers",
    "document_processing_disposition_members",
    "document_processing_disposition_seals",
    "document_processing_snapshot_headers",
    "document_processing_snapshot_members",
    "document_processing_snapshot_seals",
    "research_snapshot_headers",
    "research_snapshot_members",
    "research_snapshot_seals",
)


def _hex(column: str) -> str:
    return f"length({column})=64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _append_only(table: str) -> None:
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_{event.lower()}_append_only "
            f"BEFORE {event} ON {table} BEGIN SELECT RAISE(ABORT, "
            f"'{table} is append-only'); END"
        )


def _commitment_exact(table: str, json_column: str, sha_column: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_{sha_column}_exact BEFORE INSERT ON {table} "
        f"WHEN NEW.{sha_column} <> fact_sha256(NEW.{json_column}) "
        f"BEGIN SELECT RAISE(ABORT, '{table} commitment mismatch'); END"
    )


def _sealed_group(
    *,
    header_table: str,
    member_table: str,
    seal_table: str,
    key: str,
    label: str,
) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{member_table}_unsealed BEFORE INSERT ON {member_table} "
        f"WHEN EXISTS (SELECT 1 FROM {seal_table} seal "
        f"WHERE seal.{key}=NEW.{key}) BEGIN SELECT RAISE(ABORT, "
        f"'{label} is sealed'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{seal_table}_exact BEFORE INSERT ON {seal_table} "
        "WHEN NEW.member_count <> (SELECT COUNT(*) "
        f"FROM {member_table} member WHERE member.{key}=NEW.{key}) "
        "OR (NEW.member_count > 0 AND NEW.member_count <> 1 + ("
        f"SELECT MAX(member_ordinal) FROM {member_table} member "
        f"WHERE member.{key}=NEW.{key})) "
        "OR NEW.canonical_member_set_json <> COALESCE(("
        "SELECT json_group_array(json(canonical_member_json)) FROM ("
        f"SELECT canonical_member_json FROM {member_table} "
        f"WHERE {key}=NEW.{key} ORDER BY member_ordinal)), '[]') "
        "OR NEW.member_set_sha256 <> fact_sha256(NEW.canonical_member_set_json) "
        f"OR datetime(NEW.sealed_at) < datetime((SELECT recorded_at "
        f"FROM {header_table} header WHERE header.{key}=NEW.{key})) "
        f"BEGIN SELECT RAISE(ABORT, '{label} final seal mismatch'); END"
    )


def upgrade() -> None:
    required = {
        "source_obligation_revisions",
        "evidence_document_versions",
        "evidence_source_observations",
        "evidence_content_blobs",
        "canonical_fact_resolution_snapshot_seals",
    }
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    missing = sorted(required - existing)
    if missing:
        raise RuntimeError(
            "research snapshots require recorded obligation and evidence state: "
            + ", ".join(missing)
        )

    op.create_table(
        "document_processing_obligation_revisions",
        sa.Column("processing_obligation_revision_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("processing_obligation_key", sa.String(512), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column(
            "source_obligation_revision_id",
            sa.String(128),
            sa.ForeignKey("source_obligation_revisions.obligation_revision_id"),
            nullable=False,
        ),
        sa.Column(
            "document_version_id",
            sa.String(128),
            sa.ForeignKey("evidence_document_versions.document_version_id"),
            nullable=False,
        ),
        sa.Column("processing_lane", sa.String(64), nullable=False),
        sa.Column("applicability", sa.String(16), nullable=False),
        sa.Column("policy_name", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("policy_config_sha256", sa.String(64), nullable=False),
        sa.Column("source_state_json", sa.Text, nullable=False),
        sa.Column("source_state_sha256", sa.String(64), nullable=False),
        sa.Column("commitment_json", sa.Text, nullable=False),
        sa.Column("commitment_sha256", sa.String(64), nullable=False),
        sa.Column("effective_at", sa.DateTime, nullable=False),
        sa.Column("knowledge_at", sa.DateTime, nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.Column(
            "supersedes_processing_obligation_revision_id",
            sa.String(128),
            sa.ForeignKey(
                "document_processing_obligation_revisions.processing_obligation_revision_id"
            ),
        ),
        sa.UniqueConstraint(
            "processing_obligation_key",
            "revision",
            name="uq_document_processing_obligation_revision",
        ),
        sa.CheckConstraint(
            "revision > 0 AND applicability IN ('applicable','not_applicable') "
            "AND ((revision=1 AND "
            "supersedes_processing_obligation_revision_id IS NULL) OR "
            "(revision>1 AND "
            "supersedes_processing_obligation_revision_id IS NOT NULL))",
            name="ck_document_processing_obligation_shape",
        ),
        sa.CheckConstraint(
            "json_valid(source_state_json) AND json_type(source_state_json)='object' "
            "AND json_valid(commitment_json) AND json_type(commitment_json)='object'",
            name="ck_document_processing_obligation_json",
        ),
        sa.CheckConstraint(
            _hex("policy_config_sha256")
            + " AND "
            + _hex("source_state_sha256")
            + " AND "
            + _hex("commitment_sha256"),
            name="ck_document_processing_obligation_hashes",
        ),
        sa.CheckConstraint(
            "effective_at <= knowledge_at AND knowledge_at <= recorded_at",
            name="ck_document_processing_obligation_clocks",
        ),
    )
    op.create_index(
        "ix_document_processing_obligation_cutoff",
        "document_processing_obligation_revisions",
        [
            "document_version_id",
            "processing_lane",
            "knowledge_at",
            "recorded_at",
            "revision",
        ],
    )

    op.create_table(
        "document_processing_disposition_headers",
        sa.Column("processing_disposition_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "processing_obligation_revision_id",
            sa.String(128),
            sa.ForeignKey(
                "document_processing_obligation_revisions.processing_obligation_revision_id"
            ),
            nullable=False,
        ),
        sa.Column("terminal_status", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason_details_json", sa.Text, nullable=False),
        sa.Column("reason_details_sha256", sa.String(64), nullable=False),
        sa.Column("commitment_json", sa.Text, nullable=False),
        sa.Column("commitment_sha256", sa.String(64), nullable=False),
        sa.Column("knowledge_at", sa.DateTime, nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint(
            "processing_obligation_revision_id",
            name="uq_document_processing_disposition_obligation",
        ),
        sa.CheckConstraint(
            "terminal_status IN ('succeeded','not_applicable',"
            "'source_unavailable','quarantined','failed')",
            name="ck_document_processing_disposition_status",
        ),
        sa.CheckConstraint(
            "json_valid(reason_details_json) "
            "AND json_type(reason_details_json)='object' "
            "AND json_valid(commitment_json) "
            "AND json_type(commitment_json)='object'",
            name="ck_document_processing_disposition_json",
        ),
        sa.CheckConstraint(
            _hex("reason_details_sha256") + " AND " + _hex("commitment_sha256"),
            name="ck_document_processing_disposition_hashes",
        ),
        sa.CheckConstraint(
            "recorded_at >= knowledge_at",
            name="ck_document_processing_disposition_clocks",
        ),
    )
    op.create_table(
        "document_processing_disposition_members",
        sa.Column(
            "processing_disposition_id",
            sa.String(128),
            sa.ForeignKey("document_processing_disposition_headers.processing_disposition_id"),
            primary_key=True,
        ),
        sa.Column("member_ordinal", sa.Integer, primary_key=True),
        sa.Column("evidence_table", sa.String(128), nullable=False),
        sa.Column("evidence_id", sa.String(128), nullable=False),
        sa.Column("evidence_commitment_sha256", sa.String(64), nullable=False),
        sa.Column("evidence_knowledge_at", sa.DateTime, nullable=False),
        sa.Column("evidence_recorded_at", sa.DateTime, nullable=False),
        sa.Column("canonical_member_json", sa.Text, nullable=False),
        sa.Column("member_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "processing_disposition_id",
            "evidence_table",
            "evidence_id",
            name="uq_document_processing_disposition_evidence",
        ),
        sa.CheckConstraint(
            "member_ordinal >= 0 AND "
            + _hex("evidence_commitment_sha256")
            + " AND "
            + _hex("member_sha256")
            + " AND json_valid(canonical_member_json) "
            "AND json_type(canonical_member_json)='object' "
            "AND evidence_recorded_at >= evidence_knowledge_at",
            name="ck_document_processing_disposition_member",
        ),
    )
    op.create_table(
        "document_processing_disposition_seals",
        sa.Column(
            "processing_disposition_id",
            sa.String(128),
            sa.ForeignKey("document_processing_disposition_headers.processing_disposition_id"),
            primary_key=True,
        ),
        sa.Column("member_count", sa.Integer, nullable=False),
        sa.Column("canonical_member_set_json", sa.Text, nullable=False),
        sa.Column("member_set_sha256", sa.String(64), nullable=False),
        sa.Column("sealed_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "member_count >= 0 AND json_valid(canonical_member_set_json) "
            "AND json_type(canonical_member_set_json)='array' "
            "AND " + _hex("member_set_sha256"),
            name="ck_document_processing_disposition_seal",
        ),
    )

    op.create_table(
        "document_processing_snapshot_headers",
        sa.Column("processing_snapshot_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("scope_json", sa.Text, nullable=False),
        sa.Column("scope_sha256", sa.String(64), nullable=False),
        sa.Column("policy_json", sa.Text, nullable=False),
        sa.Column("policy_sha256", sa.String(64), nullable=False),
        sa.Column("cutoff_at", sa.DateTime, nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "json_valid(scope_json) AND json_type(scope_json)='object' "
            "AND json_valid(policy_json) AND json_type(policy_json)='object' "
            "AND " + _hex("scope_sha256") + " AND " + _hex("policy_sha256"),
            name="ck_document_processing_snapshot_header",
        ),
        sa.CheckConstraint(
            "recorded_at >= cutoff_at",
            name="ck_document_processing_snapshot_clocks",
        ),
    )
    op.create_table(
        "document_processing_snapshot_members",
        sa.Column(
            "processing_snapshot_id",
            sa.String(128),
            sa.ForeignKey("document_processing_snapshot_headers.processing_snapshot_id"),
            primary_key=True,
        ),
        sa.Column("member_ordinal", sa.Integer, primary_key=True),
        sa.Column(
            "processing_obligation_revision_id",
            sa.String(128),
            sa.ForeignKey(
                "document_processing_obligation_revisions.processing_obligation_revision_id"
            ),
            nullable=False,
        ),
        sa.Column(
            "processing_disposition_id",
            sa.String(128),
            sa.ForeignKey("document_processing_disposition_headers.processing_disposition_id"),
            nullable=False,
        ),
        sa.Column("processing_lane", sa.String(64), nullable=False),
        sa.Column("document_version_id", sa.String(128), nullable=False),
        sa.Column("canonical_member_json", sa.Text, nullable=False),
        sa.Column("member_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "processing_snapshot_id",
            "processing_obligation_revision_id",
            name="uq_document_processing_snapshot_obligation",
        ),
        sa.CheckConstraint(
            "member_ordinal >= 0 AND "
            + _hex("member_sha256")
            + " AND json_valid(canonical_member_json) "
            "AND json_type(canonical_member_json)='object'",
            name="ck_document_processing_snapshot_member",
        ),
    )
    op.create_table(
        "document_processing_snapshot_seals",
        sa.Column(
            "processing_snapshot_id",
            sa.String(128),
            sa.ForeignKey("document_processing_snapshot_headers.processing_snapshot_id"),
            primary_key=True,
        ),
        sa.Column("member_count", sa.Integer, nullable=False),
        sa.Column("canonical_member_set_json", sa.Text, nullable=False),
        sa.Column("member_set_sha256", sa.String(64), nullable=False),
        sa.Column("sealed_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "member_count >= 0 AND json_valid(canonical_member_set_json) "
            "AND json_type(canonical_member_set_json)='array' "
            "AND " + _hex("member_set_sha256"),
            name="ck_document_processing_snapshot_seal",
        ),
    )

    op.create_table(
        "research_snapshot_headers",
        sa.Column("research_snapshot_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("request_json", sa.Text, nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("cutoff_at", sa.DateTime, nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "json_valid(request_json) AND json_type(request_json)='object' "
            "AND " + _hex("request_sha256"),
            name="ck_research_snapshot_header",
        ),
        sa.CheckConstraint(
            "recorded_at >= cutoff_at",
            name="ck_research_snapshot_clocks",
        ),
    )
    op.create_table(
        "research_snapshot_members",
        sa.Column(
            "research_snapshot_id",
            sa.String(128),
            sa.ForeignKey("research_snapshot_headers.research_snapshot_id"),
            primary_key=True,
        ),
        sa.Column("member_ordinal", sa.Integer, primary_key=True),
        sa.Column("requested_lane", sa.String(256), nullable=False),
        sa.Column("reference_table", sa.String(128), nullable=False),
        sa.Column("reference_id", sa.String(128), nullable=False),
        sa.Column("reference_commitment_sha256", sa.String(64), nullable=False),
        sa.Column("reference_knowledge_at", sa.DateTime, nullable=False),
        sa.Column("reference_recorded_at", sa.DateTime, nullable=False),
        sa.Column("canonical_member_json", sa.Text, nullable=False),
        sa.Column("member_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "research_snapshot_id",
            "requested_lane",
            name="uq_research_snapshot_requested_lane",
        ),
        sa.CheckConstraint(
            "member_ordinal >= 0 AND "
            + _hex("reference_commitment_sha256")
            + " AND "
            + _hex("member_sha256")
            + " AND json_valid(canonical_member_json) "
            "AND json_type(canonical_member_json)='object' "
            "AND reference_recorded_at >= reference_knowledge_at",
            name="ck_research_snapshot_member",
        ),
    )
    op.create_table(
        "research_snapshot_seals",
        sa.Column(
            "research_snapshot_id",
            sa.String(128),
            sa.ForeignKey("research_snapshot_headers.research_snapshot_id"),
            primary_key=True,
        ),
        sa.Column("member_count", sa.Integer, nullable=False),
        sa.Column("canonical_member_set_json", sa.Text, nullable=False),
        sa.Column("member_set_sha256", sa.String(64), nullable=False),
        sa.Column("sealed_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "member_count >= 0 AND json_valid(canonical_member_set_json) "
            "AND json_type(canonical_member_set_json)='array' "
            "AND " + _hex("member_set_sha256"),
            name="ck_research_snapshot_seal",
        ),
    )

    for table in _TABLES:
        _append_only(table)
    for table, json_column, sha_column in (
        (
            "document_processing_obligation_revisions",
            "source_state_json",
            "source_state_sha256",
        ),
        (
            "document_processing_obligation_revisions",
            "commitment_json",
            "commitment_sha256",
        ),
        (
            "document_processing_disposition_headers",
            "reason_details_json",
            "reason_details_sha256",
        ),
        (
            "document_processing_disposition_headers",
            "commitment_json",
            "commitment_sha256",
        ),
        (
            "document_processing_disposition_members",
            "canonical_member_json",
            "member_sha256",
        ),
        (
            "document_processing_snapshot_headers",
            "scope_json",
            "scope_sha256",
        ),
        (
            "document_processing_snapshot_headers",
            "policy_json",
            "policy_sha256",
        ),
        (
            "document_processing_snapshot_members",
            "canonical_member_json",
            "member_sha256",
        ),
        ("research_snapshot_headers", "request_json", "request_sha256"),
        ("research_snapshot_members", "canonical_member_json", "member_sha256"),
    ):
        _commitment_exact(table, json_column, sha_column)

    op.execute(
        "CREATE TRIGGER trg_document_processing_obligation_parent_clocks "
        "BEFORE INSERT ON document_processing_obligation_revisions WHEN "
        "NOT EXISTS (SELECT 1 FROM source_obligation_revisions source "
        "WHERE source.obligation_revision_id=NEW.source_obligation_revision_id "
        "AND datetime(source.knowledge_at)<=datetime(NEW.knowledge_at) "
        "AND datetime(source.recorded_at)<=datetime(NEW.recorded_at)) "
        "OR NOT EXISTS (SELECT 1 FROM evidence_document_versions document "
        "JOIN evidence_source_observations observation "
        "ON observation.observation_id=document.observation_id "
        "JOIN evidence_content_blobs blob ON blob.sha256=document.blob_sha256 "
        "WHERE document.document_version_id=NEW.document_version_id "
        "AND datetime(observation.retrieved_at)<=datetime(NEW.knowledge_at) "
        "AND datetime(document.recorded_at)<=datetime(NEW.recorded_at) "
        "AND datetime(blob.recorded_at)<=datetime(NEW.recorded_at)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'processing obligation predates source evidence'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_document_processing_disposition_shape "
        "BEFORE INSERT ON document_processing_disposition_headers WHEN "
        "(NEW.terminal_status='succeeded' AND NOT EXISTS ("
        "SELECT 1 FROM document_processing_obligation_revisions obligation "
        "WHERE obligation.processing_obligation_revision_id="
        "NEW.processing_obligation_revision_id "
        "AND obligation.applicability='applicable')) "
        "OR (NEW.terminal_status='not_applicable' AND NOT EXISTS ("
        "SELECT 1 FROM document_processing_obligation_revisions obligation "
        "WHERE obligation.processing_obligation_revision_id="
        "NEW.processing_obligation_revision_id "
        "AND obligation.applicability='not_applicable')) "
        "BEGIN SELECT RAISE(ABORT, "
        "'processing disposition does not match obligation applicability'); END"
    )

    _sealed_group(
        header_table="document_processing_disposition_headers",
        member_table="document_processing_disposition_members",
        seal_table="document_processing_disposition_seals",
        key="processing_disposition_id",
        label="Document Processing Disposition",
    )
    _sealed_group(
        header_table="document_processing_snapshot_headers",
        member_table="document_processing_snapshot_members",
        seal_table="document_processing_snapshot_seals",
        key="processing_snapshot_id",
        label="Document Processing Snapshot",
    )
    _sealed_group(
        header_table="research_snapshot_headers",
        member_table="research_snapshot_members",
        seal_table="research_snapshot_seals",
        key="research_snapshot_id",
        label="Research Snapshot",
    )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_table(table)
