"""Close filing-native XBRL processor inputs and raw-source commitments.

Revision ID: 0254_filing_xbrl_processor_closure
Revises: 0253_ask_sealed_answer_audit
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0254_filing_xbrl_processor_closure"
down_revision: str | Sequence[str] | None = "0253_ask_sealed_answer_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _hex(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _append_only(table: str, identity_column: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_identity_reinsert BEFORE INSERT ON {table} "
        f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {identity_column}=NEW.{identity_column} "
        f"OR idempotency_key=NEW.idempotency_key) "
        f"BEGIN SELECT RAISE(ABORT, '{table} identity cannot be replaced'); END"
    )


def upgrade() -> None:
    op.create_table(
        "filing_xbrl_processor_artifacts",
        sa.Column("processor_artifact_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("bundle_name", sa.String(128), nullable=False),
        sa.Column("arelle_version", sa.String(64), nullable=False),
        sa.Column("edgar_version", sa.String(64), nullable=False),
        sa.Column("xule_version", sa.String(64), nullable=False),
        sa.Column("bridge_protocol_version", sa.String(64), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("sandbox_launcher_sha256", sa.String(64), nullable=False),
        sa.Column("bundle_python_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_manifest_json", sa.Text(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            _hex("artifact_sha256")
            + " AND "
            + _hex("sandbox_launcher_sha256")
            + " AND "
            + _hex("bundle_python_sha256")
            + " AND "
            + _hex("manifest_sha256"),
            name="ck_filing_xbrl_processor_artifact_hashes",
        ),
        sa.CheckConstraint(
            "json_valid(canonical_manifest_json) "
            "AND json_type(canonical_manifest_json) = 'object'",
            name="ck_filing_xbrl_processor_artifact_manifest",
        ),
    )
    op.create_table(
        "filing_xbrl_extraction_input_members",
        sa.Column("input_member_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "extraction_run_id",
            sa.String(128),
            sa.ForeignKey("evidence_extraction_runs.extraction_run_id"),
            nullable=False,
        ),
        sa.Column("member_ordinal", sa.Integer(), nullable=False),
        sa.Column("member_role", sa.String(32), nullable=False),
        sa.Column(
            "document_version_id",
            sa.String(128),
            sa.ForeignKey("evidence_document_versions.document_version_id"),
            nullable=True,
        ),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column(
            "blob_sha256",
            sa.String(64),
            sa.ForeignKey("evidence_content_blobs.sha256"),
            nullable=False,
        ),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(255), nullable=False),
        sa.Column("canonical_member_json", sa.Text(), nullable=False),
        sa.Column("member_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "extraction_run_id",
            "member_ordinal",
            name="uq_filing_xbrl_input_run_ordinal",
        ),
        sa.CheckConstraint("member_ordinal >= 0", name="ck_filing_xbrl_input_ordinal"),
        sa.CheckConstraint("byte_size >= 0", name="ck_filing_xbrl_input_byte_size"),
        sa.CheckConstraint(
            "member_role IN ('primary_document','filing_attachment',"
            "'issuer_taxonomy','standard_taxonomy','network_artifact')",
            name="ck_filing_xbrl_input_role",
        ),
        sa.CheckConstraint(
            "(member_role IN ('primary_document','filing_attachment','issuer_taxonomy') "
            "AND document_version_id IS NOT NULL) OR "
            "(member_role IN ('standard_taxonomy','network_artifact') "
            "AND document_version_id IS NULL)",
            name="ck_filing_xbrl_input_document_binding",
        ),
        sa.CheckConstraint(
            _hex("blob_sha256") + " AND " + _hex("member_sha256"),
            name="ck_filing_xbrl_input_hashes",
        ),
        sa.CheckConstraint(
            "json_valid(canonical_member_json) "
            "AND json_type(canonical_member_json) = 'object'",
            name="ck_filing_xbrl_input_member_json",
        ),
    )
    op.create_table(
        "filing_xbrl_extraction_input_seals",
        sa.Column("input_seal_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "extraction_run_id",
            sa.String(128),
            sa.ForeignKey("evidence_extraction_runs.extraction_run_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "processor_artifact_id",
            sa.String(128),
            sa.ForeignKey("filing_xbrl_processor_artifacts.processor_artifact_id"),
            nullable=False,
        ),
        sa.Column("accession_number", sa.String(32), nullable=False),
        sa.Column("expected_cik", sa.String(10), nullable=False),
        sa.Column(
            "issuer_id",
            sa.String(128),
            sa.ForeignKey("issuer_entities.issuer_id"),
            nullable=False,
        ),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("canonical_member_set_json", sa.Text(), nullable=False),
        sa.Column("member_set_sha256", sa.String(64), nullable=False),
        sa.Column("network_artifact_count", sa.Integer(), nullable=False),
        sa.Column("canonical_network_artifact_set_json", sa.Text(), nullable=False),
        sa.Column("network_artifact_set_sha256", sa.String(64), nullable=False),
        sa.Column("raw_fact_count", sa.Integer(), nullable=False),
        sa.Column("raw_fact_set_sha256", sa.String(64), nullable=False),
        sa.Column("footnote_count", sa.Integer(), nullable=False),
        sa.Column("canonical_footnote_set_json", sa.Text(), nullable=False),
        sa.Column("footnote_set_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_execution_evidence_json", sa.Text(), nullable=False),
        sa.Column("execution_evidence_sha256", sa.String(64), nullable=False),
        sa.Column("zero_fact_disposition", sa.String(64), nullable=True),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("member_count > 0", name="ck_filing_xbrl_input_seal_count"),
        sa.CheckConstraint(
            "length(accession_number)=20 AND length(expected_cik)=10 "
            "AND expected_cik NOT GLOB '*[^0-9]*'",
            name="ck_filing_xbrl_input_seal_filing_identity",
        ),
        sa.CheckConstraint(
            "network_artifact_count >= 0 AND raw_fact_count >= 0 AND footnote_count >= 0",
            name="ck_filing_xbrl_result_counts",
        ),
        sa.CheckConstraint(
            "(raw_fact_count=0 AND zero_fact_disposition='verified_no_inline_xbrl') "
            "OR (raw_fact_count>0 AND zero_fact_disposition IS NULL)",
            name="ck_filing_xbrl_zero_fact_disposition",
        ),
        sa.CheckConstraint(
            _hex("member_set_sha256")
            + " AND "
            + _hex("network_artifact_set_sha256")
            + " AND "
            + _hex("raw_fact_set_sha256")
            + " AND "
            + _hex("footnote_set_sha256")
            + " AND "
            + _hex("execution_evidence_sha256"),
            name="ck_filing_xbrl_input_seal_hash",
        ),
        sa.CheckConstraint(
            "json_valid(canonical_member_set_json) "
            "AND json_type(canonical_member_set_json) = 'array' "
            "AND json_valid(canonical_network_artifact_set_json) "
            "AND json_type(canonical_network_artifact_set_json) = 'array' "
            "AND json_valid(canonical_footnote_set_json) "
            "AND json_type(canonical_footnote_set_json) = 'array' "
            "AND json_valid(canonical_execution_evidence_json) "
            "AND json_type(canonical_execution_evidence_json) = 'object'",
            name="ck_filing_xbrl_input_seal_json",
        ),
    )
    op.create_table(
        "filing_xbrl_raw_fact_commitments",
        sa.Column("raw_fact_commitment_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "extraction_run_id",
            sa.String(128),
            sa.ForeignKey("evidence_extraction_runs.extraction_run_id"),
            nullable=False,
        ),
        sa.Column("input_ordinal", sa.Integer(), nullable=False),
        sa.Column(
            "evidence_node_id",
            sa.String(128),
            sa.ForeignKey("evidence_nodes.node_id"),
            nullable=False,
        ),
        sa.Column("package_member_ordinal", sa.Integer(), nullable=False),
        sa.Column("package_member_blob_sha256", sa.String(64), nullable=False),
        sa.Column("accession_number", sa.String(32), nullable=False),
        sa.Column("observed_cik", sa.String(10), nullable=False),
        sa.Column("source_entry_sha256", sa.String(64), nullable=False),
        sa.Column("source_locator_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_raw_fact_json", sa.Text(), nullable=False),
        sa.Column("raw_fact_sha256", sa.String(64), nullable=False),
        sa.Column("normalization_outcome", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=True),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "extraction_run_id",
            "input_ordinal",
            name="uq_filing_xbrl_raw_fact_run_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ("extraction_run_id", "package_member_ordinal"),
            (
                "filing_xbrl_extraction_input_members.extraction_run_id",
                "filing_xbrl_extraction_input_members.member_ordinal",
            ),
            name="fk_filing_xbrl_raw_fact_package_member",
        ),
        sa.CheckConstraint(
            "input_ordinal >= 0 AND package_member_ordinal >= 0",
            name="ck_filing_xbrl_raw_fact_ordinal",
        ),
        sa.CheckConstraint(
            "length(accession_number)=20 AND length(observed_cik)=10 "
            "AND observed_cik NOT GLOB '*[^0-9]*'",
            name="ck_filing_xbrl_raw_fact_filing_identity",
        ),
        sa.CheckConstraint(
            "normalization_outcome IN ('normalized','rejected')",
            name="ck_filing_xbrl_raw_fact_outcome",
        ),
        sa.CheckConstraint(
            "(normalization_outcome='normalized' AND reason_code IS NULL) OR "
            "(normalization_outcome='rejected' AND length(trim(reason_code)) > 0)",
            name="ck_filing_xbrl_raw_fact_reason",
        ),
        sa.CheckConstraint(
            _hex("source_entry_sha256")
            + " AND "
            + _hex("source_locator_sha256")
            + " AND "
            + _hex("package_member_blob_sha256")
            + " AND "
            + _hex("raw_fact_sha256"),
            name="ck_filing_xbrl_raw_fact_hashes",
        ),
        sa.CheckConstraint(
            "json_valid(canonical_raw_fact_json) "
            "AND json_type(canonical_raw_fact_json) = 'object'",
            name="ck_filing_xbrl_raw_fact_json",
        ),
    )
    op.create_table(
        "filing_xbrl_footnote_commitments",
        sa.Column("footnote_commitment_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "extraction_run_id",
            sa.String(128),
            sa.ForeignKey("evidence_extraction_runs.extraction_run_id"),
            nullable=False,
        ),
        sa.Column("input_ordinal", sa.Integer(), nullable=False),
        sa.Column("footnote_ordinal", sa.Integer(), nullable=False),
        sa.Column("canonical_footnote_json", sa.Text(), nullable=False),
        sa.Column("footnote_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "extraction_run_id",
            "input_ordinal",
            "footnote_ordinal",
            name="uq_filing_xbrl_footnote_run_fact_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ("extraction_run_id", "input_ordinal"),
            (
                "filing_xbrl_raw_fact_commitments.extraction_run_id",
                "filing_xbrl_raw_fact_commitments.input_ordinal",
            ),
            name="fk_filing_xbrl_footnote_raw_fact",
        ),
        sa.CheckConstraint(
            "input_ordinal >= 0 AND footnote_ordinal >= 0",
            name="ck_filing_xbrl_footnote_ordinals",
        ),
        sa.CheckConstraint(_hex("footnote_sha256"), name="ck_filing_xbrl_footnote_hash"),
        sa.CheckConstraint(
            "json_valid(canonical_footnote_json) "
            "AND json_type(canonical_footnote_json) = 'object'",
            name="ck_filing_xbrl_footnote_json",
        ),
    )

    op.execute(
        "CREATE TRIGGER trg_filing_xbrl_input_members_unsealed "
        "BEFORE INSERT ON filing_xbrl_extraction_input_members "
        "WHEN EXISTS (SELECT 1 FROM filing_xbrl_extraction_input_seals "
        "WHERE extraction_run_id=NEW.extraction_run_id) "
        "BEGIN SELECT RAISE(ABORT, 'filing-XBRL input set is sealed'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_filing_xbrl_input_seal_exact "
        "BEFORE INSERT ON filing_xbrl_extraction_input_seals "
        "WHEN (SELECT COUNT(*) FROM filing_xbrl_extraction_input_members member "
        "WHERE member.extraction_run_id=NEW.extraction_run_id)<>NEW.member_count "
        "OR (SELECT COUNT(*) FROM filing_xbrl_extraction_input_members member "
        "WHERE member.extraction_run_id=NEW.extraction_run_id "
        "AND member.member_role='primary_document')<>1 "
        "OR NOT EXISTS (SELECT 1 FROM filing_xbrl_extraction_input_members member "
        "WHERE member.extraction_run_id=NEW.extraction_run_id "
        "AND member.member_ordinal=0 AND member.member_role='primary_document') "
        "OR COALESCE((SELECT MIN(member_ordinal) "
        "FROM filing_xbrl_extraction_input_members member "
        "WHERE member.extraction_run_id=NEW.extraction_run_id),-1)<>0 "
        "OR COALESCE((SELECT MAX(member_ordinal) "
        "FROM filing_xbrl_extraction_input_members member "
        "WHERE member.extraction_run_id=NEW.extraction_run_id),-1)<>NEW.member_count-1 "
        "OR (SELECT COUNT(*) FROM filing_xbrl_extraction_input_members member "
        "WHERE member.extraction_run_id=NEW.extraction_run_id "
        "AND member.member_role IN "
        "('issuer_taxonomy','standard_taxonomy','network_artifact'))"
        "<>NEW.network_artifact_count "
        "OR NOT EXISTS (SELECT 1 FROM evidence_extraction_runs run "
        "WHERE run.extraction_run_id=NEW.extraction_run_id "
        "AND run.extractor_name='filing-native-xbrl' AND run.outcome='succeeded' "
        "AND julianday(run.started_at)=julianday(NEW.recorded_at) "
        "AND julianday(run.completed_at)=julianday(NEW.recorded_at)) "
        "OR NOT EXISTS (SELECT 1 FROM filing_xbrl_processor_artifacts artifact "
        "WHERE artifact.processor_artifact_id=NEW.processor_artifact_id "
        "AND artifact.arelle_version='2.39.8' AND artifact.edgar_version='26.1' "
        "AND artifact.xule_version='30052' "
        "AND artifact.bridge_protocol_version='filing-xbrl-bridge.v1' "
        "AND json_extract(artifact.canonical_manifest_json,'$.qualification.profile')="
        "'sec-inline-xbrl-investor-grade.v1' "
        "AND json_extract(artifact.canonical_manifest_json,"
        "'$.qualification.require_os_network_denial')=1 "
        "AND json_extract(artifact.canonical_manifest_json,"
        "'$.qualification.require_sec_filing_identity')=1 "
        "AND json_extract(artifact.canonical_manifest_json,"
        "'$.qualification.require_source_locator_commitments')=1 "
        "AND json_extract(artifact.canonical_manifest_json,"
        "'$.qualification.require_network_artifact_commitments')=1 "
        "AND json_extract(artifact.canonical_manifest_json,"
        "'$.qualification.require_footnote_commitments')=1 "
        "AND json_extract(artifact.canonical_manifest_json,"
        "'$.qualification.require_zero_fact_host_verification')=1) "
        "OR fact_sha256(NEW.canonical_member_set_json)<>NEW.member_set_sha256 "
        "OR fact_sha256(NEW.canonical_network_artifact_set_json)"
        "<>NEW.network_artifact_set_sha256 "
        "OR fact_sha256(NEW.canonical_footnote_set_json)<>NEW.footnote_set_sha256 "
        "OR fact_sha256(NEW.canonical_execution_evidence_json)"
        "<>NEW.execution_evidence_sha256 "
        "OR json_extract(NEW.canonical_execution_evidence_json,"
        "'$.sandbox_contract_version')<>'earnings-xbrl-os-sandbox.v1' "
        "OR json_extract(NEW.canonical_execution_evidence_json,"
        "'$.internet_connectivity')<>'os_denied' "
        "OR json_extract(NEW.canonical_execution_evidence_json,"
        "'$.network_requests_observed')<>0 "
        "OR json_extract(NEW.canonical_execution_evidence_json,"
        "'$.accession_number')<>NEW.accession_number "
        "OR json_extract(NEW.canonical_execution_evidence_json,"
        "'$.expected_cik')<>NEW.expected_cik "
        "OR NOT EXISTS (SELECT 1 FROM issuer_identifier_resolution_outcomes resolution "
        "JOIN issuer_identifier_assertions assertion "
        "ON assertion.assertion_id=resolution.selected_assertion_id "
        "WHERE resolution.resolution_key='sec_cik:'||NEW.expected_cik "
        "AND resolution.outcome='selected' AND assertion.issuer_id=NEW.issuer_id "
        "AND resolution.knowledge_at<=NEW.recorded_at "
        "AND assertion.knowledge_at<=NEW.recorded_at "
        "AND NOT EXISTS (SELECT 1 FROM issuer_identifier_resolution_outcomes newer "
        "WHERE newer.resolution_key=resolution.resolution_key "
        "AND newer.knowledge_at<=NEW.recorded_at "
        "AND newer.revision>resolution.revision)) "
        "OR json_extract(NEW.canonical_execution_evidence_json,"
        "'$.package_member_set_sha256')<>NEW.member_set_sha256 "
        "OR json_extract(NEW.canonical_execution_evidence_json,"
        "'$.runtime_artifact_sha256')<>(SELECT artifact.artifact_sha256 "
        "FROM filing_xbrl_processor_artifacts artifact "
        "WHERE artifact.processor_artifact_id=NEW.processor_artifact_id) "
        "OR EXISTS (SELECT 1 FROM filing_xbrl_extraction_input_members member "
        "LEFT JOIN evidence_content_blobs blob ON blob.sha256=member.blob_sha256 "
        "LEFT JOIN evidence_document_versions document "
        "ON document.document_version_id=member.document_version_id "
        "WHERE member.extraction_run_id=NEW.extraction_run_id "
        "AND (blob.sha256 IS NULL OR blob.byte_size<>member.byte_size "
        "OR blob.media_type<>member.media_type "
        "OR julianday(blob.recorded_at)>julianday(NEW.recorded_at) "
        "OR (member.document_version_id IS NOT NULL "
        "AND (document.document_version_id IS NULL "
        "OR document.blob_sha256<>member.blob_sha256 "
        "OR (member.member_role='primary_document' "
        "AND document.issuer_id<>NEW.issuer_id) "
        "OR julianday(document.recorded_at)>julianday(NEW.recorded_at))) "
        "OR (member.document_version_id IS NULL AND NOT EXISTS (SELECT 1 "
        "FROM evidence_source_observations observation "
        "WHERE observation.source_url=member.source_url "
        "AND observation.blob_sha256=member.blob_sha256 "
        "AND julianday(observation.retrieved_at)<=julianday(NEW.recorded_at))))) "
        "BEGIN SELECT RAISE(ABORT, 'filing-XBRL input seal is incomplete'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_filing_xbrl_raw_fact_binding "
        "BEFORE INSERT ON filing_xbrl_raw_fact_commitments "
        "WHEN NOT EXISTS (SELECT 1 FROM evidence_nodes node "
        "WHERE node.node_id=NEW.evidence_node_id "
        "AND node.extraction_run_id=NEW.extraction_run_id "
        "AND node.locator_sha256=NEW.source_locator_sha256) "
        "OR NOT EXISTS (SELECT 1 FROM filing_xbrl_extraction_input_members member "
        "WHERE member.extraction_run_id=NEW.extraction_run_id "
        "AND member.member_ordinal=NEW.package_member_ordinal "
        "AND member.blob_sha256=NEW.package_member_blob_sha256) "
        "OR NOT EXISTS (SELECT 1 FROM filing_xbrl_extraction_input_seals seal "
        "WHERE seal.extraction_run_id=NEW.extraction_run_id "
        "AND seal.accession_number=NEW.accession_number "
        "AND seal.expected_cik=NEW.observed_cik) "
        "OR NEW.input_ordinal >= (SELECT seal.raw_fact_count "
        "FROM filing_xbrl_extraction_input_seals seal "
        "WHERE seal.extraction_run_id=NEW.extraction_run_id) "
        "OR EXISTS (SELECT 1 FROM filing_xbrl_extraction_dispositions disposition "
        "WHERE disposition.extraction_run_id=NEW.extraction_run_id) "
        "OR (SELECT COUNT(*) FROM filing_xbrl_raw_fact_commitments raw "
        "WHERE raw.extraction_run_id=NEW.extraction_run_id) >= "
        "(SELECT seal.raw_fact_count FROM filing_xbrl_extraction_input_seals seal "
        "WHERE seal.extraction_run_id=NEW.extraction_run_id) "
        "BEGIN SELECT RAISE(ABORT, 'filing-XBRL raw fact binding is orphaned'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_filing_xbrl_footnote_raw_fact "
        "BEFORE INSERT ON filing_xbrl_footnote_commitments "
        "WHEN NOT EXISTS (SELECT 1 FROM filing_xbrl_raw_fact_commitments raw "
        "WHERE raw.extraction_run_id=NEW.extraction_run_id "
        "AND raw.input_ordinal=NEW.input_ordinal) "
        "OR (SELECT COUNT(*) FROM filing_xbrl_footnote_commitments footnote "
        "WHERE footnote.extraction_run_id=NEW.extraction_run_id) >= "
        "(SELECT seal.footnote_count FROM filing_xbrl_extraction_input_seals seal "
        "WHERE seal.extraction_run_id=NEW.extraction_run_id) "
        "OR EXISTS (SELECT 1 FROM filing_xbrl_extraction_dispositions disposition "
        "WHERE disposition.extraction_run_id=NEW.extraction_run_id) "
        "BEGIN SELECT RAISE(ABORT, 'filing-XBRL footnote has no raw fact'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_filing_xbrl_disposition_input_binding "
        "BEFORE INSERT ON filing_xbrl_extraction_dispositions "
        "WHEN EXISTS (SELECT 1 FROM evidence_extraction_runs run "
        "WHERE run.extraction_run_id=NEW.extraction_run_id "
        "AND run.extractor_name='filing-native-xbrl') "
        "AND NOT EXISTS (SELECT 1 FROM filing_xbrl_raw_fact_commitments raw "
        "JOIN filing_xbrl_extraction_input_seals seal "
        "ON seal.extraction_run_id=raw.extraction_run_id "
        "WHERE raw.extraction_run_id=NEW.extraction_run_id "
        "AND raw.input_ordinal=NEW.input_ordinal "
        "AND raw.source_entry_sha256=NEW.source_entry_sha256 "
        "AND raw.source_locator_sha256=NEW.source_locator_sha256 "
        "AND ((raw.normalization_outcome='normalized' "
        "AND NEW.disposition IN ('published','duplicate')) "
        "OR (raw.normalization_outcome='rejected' "
        "AND NEW.disposition='quarantined')) "
        "AND julianday(raw.recorded_at)=julianday(NEW.recorded_at) "
        "AND julianday(seal.recorded_at)=julianday(NEW.recorded_at)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'filing-XBRL disposition lacks its sealed raw commitment'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_filing_xbrl_disposition_seal_input_binding "
        "BEFORE INSERT ON filing_xbrl_extraction_disposition_seals "
        "WHEN EXISTS (SELECT 1 FROM evidence_extraction_runs run "
        "WHERE run.extraction_run_id=NEW.extraction_run_id "
        "AND run.extractor_name='filing-native-xbrl') "
        "AND NOT EXISTS (SELECT 1 FROM filing_xbrl_extraction_input_seals input "
        "WHERE input.extraction_run_id=NEW.extraction_run_id "
        "AND input.raw_fact_count=NEW.entry_count "
        "AND (SELECT COUNT(*) FROM filing_xbrl_raw_fact_commitments raw "
        "WHERE raw.extraction_run_id=NEW.extraction_run_id)=input.raw_fact_count "
        "AND (SELECT COUNT(*) FROM filing_xbrl_footnote_commitments footnote "
        "WHERE footnote.extraction_run_id=NEW.extraction_run_id)=input.footnote_count "
        "AND julianday(input.recorded_at)=julianday(NEW.recorded_at) "
        "AND julianday(NEW.knowledge_at)=julianday(NEW.recorded_at)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'filing-XBRL disposition seal lacks complete processor input closure'); END"
    )
    for table, identity_column in (
        ("filing_xbrl_processor_artifacts", "processor_artifact_id"),
        ("filing_xbrl_extraction_input_members", "input_member_id"),
        ("filing_xbrl_extraction_input_seals", "input_seal_id"),
        ("filing_xbrl_raw_fact_commitments", "raw_fact_commitment_id"),
        ("filing_xbrl_footnote_commitments", "footnote_commitment_id"),
    ):
        _append_only(table, identity_column)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_filing_xbrl_input_members_unsealed")
    op.execute("DROP TRIGGER IF EXISTS trg_filing_xbrl_input_seal_exact")
    op.execute("DROP TRIGGER IF EXISTS trg_filing_xbrl_raw_fact_binding")
    op.execute("DROP TRIGGER IF EXISTS trg_filing_xbrl_footnote_raw_fact")
    op.execute("DROP TRIGGER IF EXISTS trg_filing_xbrl_disposition_input_binding")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_filing_xbrl_disposition_seal_input_binding"
    )
    for table in (
        "filing_xbrl_footnote_commitments",
        "filing_xbrl_raw_fact_commitments",
        "filing_xbrl_extraction_input_seals",
        "filing_xbrl_extraction_input_members",
        "filing_xbrl_processor_artifacts",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_identity_reinsert")
        op.drop_table(table)
