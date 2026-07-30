"""Seal full-universe population completeness and legacy parity.

Revision ID: 0256_population_cutover_receipts
Revises: 0255_scoped_canonical_resolution_snapshots
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0256_population_cutover_receipts"
down_revision: str | None = "0255_scoped_canonical_resolution_snapshots"
branch_labels: str | None = None
depends_on: str | None = None

_REQUIRED_PLANES = (
    "identity_scope",
    "source_fact_ontology",
    "canonical_resolution",
    "canonical_projection",
    "document_processing",
    "research_snapshot",
    "retrieval_runtime",
)
_REQUIRED_GATES = (
    "source_fact_publications",
    "source_fact_publication_stream",
    "filing_xbrl_dispositions",
    "ontology_snapshots",
    "canonical_resolution_snapshots",
    "canonical_projection_generations",
    "document_processing_evidence",
    "document_processing_snapshots",
    "research_snapshots",
    "heterogeneous_retrieval_traces",
    "embedding_runtime_promotions",
    "embedding_runtime_artifacts",
    "embedding_runtime_projection_seals",
)
_TABLES = (
    "population_run_headers",
    "population_plane_receipts",
    "population_parity_receipts",
    "population_cutover_audit_receipts",
    "population_cutover_receipts",
)
_ASK_PROMOTION_COLUMNS = (
    "population_run_id",
    "population_receipt_set_sha256",
    "population_observed_through",
)
_RETRIEVAL_TRACE_COLUMNS = _ASK_PROMOTION_COLUMNS


def _hex(column: str) -> str:
    return f"length({column})=64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _append_only(table: str) -> None:
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_{event.lower()}_append_only "
            f"BEFORE {event} ON {table} BEGIN SELECT RAISE(ABORT, "
            f"'{table} is append-only'); END"
        )


def _canonical_snapshot_trigger(*, recorded_clock: str) -> str:
    return (
        "CREATE TRIGGER trg_canonical_fact_snapshot_exact "  # nosec B608 -- fixed internal schema DDL; only the recorded-clock column name varies over a closed migration constant
        "BEFORE INSERT ON canonical_fact_resolution_snapshot_seals WHEN "
        "NOT EXISTS (SELECT 1 "
        "FROM canonical_fact_resolution_snapshot_scope_headers scope "
        "WHERE scope.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "AND scope.cutoff_at=NEW.cutoff_at "
        "AND scope.recorded_at=NEW.recorded_at) "
        "OR NEW.member_count<>(SELECT COUNT(*) "
        "FROM canonical_fact_resolution_snapshot_members member "
        "WHERE member.resolution_snapshot_id=NEW.resolution_snapshot_id) "
        "OR (NEW.member_count>0 AND ("
        "(SELECT MIN(member_ordinal) FROM canonical_fact_resolution_snapshot_members "
        "WHERE resolution_snapshot_id=NEW.resolution_snapshot_id)<>0 "
        "OR (SELECT MAX(member_ordinal) FROM canonical_fact_resolution_snapshot_members "
        "WHERE resolution_snapshot_id=NEW.resolution_snapshot_id)<>NEW.member_count-1)) "
        "OR NEW.canonical_member_set_json<>COALESCE((SELECT "
        "json_group_array(json(ordered.payload)) FROM (SELECT json_object("
        "'candidate_universe_id',member.candidate_universe_id,"
        "'canonical_metric_cell_id',member.canonical_metric_cell_id,"
        "'canonical_resolution_revision_id',"
        "member.canonical_resolution_revision_id,"
        "'relation_set_id',member.relation_set_id) payload "
        "FROM canonical_fact_resolution_snapshot_members member "
        "WHERE member.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "ORDER BY member.member_ordinal) ordered),'[]') "
        "OR NEW.member_set_sha256<>fact_sha256(NEW.canonical_member_set_json) "
        "OR EXISTS (SELECT 1 "
        "FROM canonical_fact_resolution_snapshot_members member "
        "JOIN canonical_fact_resolution_revisions resolution "
        "ON resolution.canonical_resolution_revision_id="
        "member.canonical_resolution_revision_id "
        "JOIN canonical_metric_cells cell "
        "ON cell.canonical_metric_cell_id=member.canonical_metric_cell_id "
        "WHERE member.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "AND (resolution.canonical_metric_cell_id<>member.canonical_metric_cell_id "
        "OR resolution.candidate_universe_id<>member.candidate_universe_id "
        "OR resolution.relation_set_id<>member.relation_set_id "
        "OR member.member_sha256<>fact_sha256(json_object("
        "'candidate_universe_id',member.candidate_universe_id,"
        "'canonical_metric_cell_id',member.canonical_metric_cell_id,"
        "'canonical_resolution_revision_id',"
        "member.canonical_resolution_revision_id,"
        "'relation_set_id',member.relation_set_id)) "
        "OR resolution.knowledge_at>NEW.cutoff_at "
        f"OR resolution.recorded_at>{recorded_clock} "
        "OR NOT EXISTS (SELECT 1 "
        "FROM canonical_fact_resolution_snapshot_scope_members scope_member "
        "WHERE scope_member.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "AND scope_member.reporting_entity_id=cell.reporting_entity_id) "
        "OR EXISTS (SELECT 1 FROM canonical_fact_resolution_revisions newer "
        "WHERE newer.canonical_metric_cell_id=resolution.canonical_metric_cell_id "
        "AND newer.knowledge_at<=NEW.cutoff_at "
        f"AND newer.recorded_at<={recorded_clock} "
        "AND newer.revision>resolution.revision))) "
        "OR EXISTS (SELECT 1 FROM canonical_fact_resolution_revisions resolution "
        "JOIN canonical_metric_cells cell "
        "ON cell.canonical_metric_cell_id=resolution.canonical_metric_cell_id "
        "JOIN canonical_fact_resolution_snapshot_scope_members scope_member "
        "ON scope_member.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "AND scope_member.reporting_entity_id=cell.reporting_entity_id "
        "WHERE resolution.knowledge_at<=NEW.cutoff_at "
        f"AND resolution.recorded_at<={recorded_clock} "
        "AND NOT EXISTS (SELECT 1 FROM canonical_fact_resolution_revisions newer "
        "WHERE newer.canonical_metric_cell_id=resolution.canonical_metric_cell_id "
        "AND newer.knowledge_at<=NEW.cutoff_at "
        f"AND newer.recorded_at<={recorded_clock} "
        "AND newer.revision>resolution.revision) "
        "AND NOT EXISTS (SELECT 1 "
        "FROM canonical_fact_resolution_snapshot_members member "
        "WHERE member.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "AND member.canonical_resolution_revision_id="
        "resolution.canonical_resolution_revision_id)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'canonical scoped snapshot commitment mismatch'); END"
    )


def _canonical_watermark_trigger(*, observed_clock: str) -> str:
    return (
        "CREATE TRIGGER trg_canonical_resolution_snapshot_watermark_exact "
        "BEFORE INSERT ON canonical_fact_resolution_snapshot_watermarks WHEN "
        "NOT EXISTS ("
        "SELECT 1 FROM canonical_fact_resolution_snapshot_seals AS snapshot "
        "WHERE snapshot.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "AND snapshot.cutoff_at=NEW.cutoff_at "
        f"AND snapshot.recorded_at<={observed_clock}) "
        f"OR (NEW.publication_high_watermark=0 AND "
        f"NEW.high_watermark_event_sha256<>'{'0' * 64}') "
        f"OR (NEW.publication_high_watermark>0 AND "
        f"NEW.high_watermark_event_sha256='{'0' * 64}') "
        "OR (NEW.publication_high_watermark>0 AND NOT EXISTS ("
        "SELECT 1 FROM source_fact_publication_stream AS event "
        "WHERE event.publication_sequence=NEW.publication_high_watermark "
        "AND event.stream_id=NEW.stream_id "
        "AND event.event_sha256=NEW.high_watermark_event_sha256 "
        "AND julianday(event.sealed_at)<=julianday(NEW.cutoff_at) "
        f"AND julianday(event.assigned_at)<=julianday({observed_clock}))) "
        "OR EXISTS ("
        "SELECT 1 FROM source_fact_publication_stream AS included "
        "WHERE included.stream_id=NEW.stream_id "
        "AND included.publication_sequence<=NEW.publication_high_watermark "
        "AND (julianday(included.sealed_at)>julianday(NEW.cutoff_at) "
        f"OR julianday(included.assigned_at)>julianday({observed_clock}))) "
        "OR EXISTS ("
        "SELECT 1 FROM source_fact_publication_stream AS later "
        "WHERE later.stream_id=NEW.stream_id "
        "AND later.publication_sequence>NEW.publication_high_watermark "
        "AND julianday(later.sealed_at)<=julianday(NEW.cutoff_at) "
        f"AND julianday(later.assigned_at)<=julianday({observed_clock})) "
        "OR NEW.canonical_watermark_json <> "
        "canonical_resolution_snapshot_watermark_v1("
        "NEW.resolution_snapshot_id,NEW.stream_id,"
        "NEW.publication_high_watermark,"
        "NEW.high_watermark_event_sha256,NEW.cutoff_at,NEW.recorded_at) "
        "OR NEW.watermark_sha256<>fact_sha256(NEW.canonical_watermark_json) "
        "BEGIN SELECT RAISE(ABORT, "
        "'canonical resolution snapshot watermark mismatch'); END"
    )


def _legacy_canonical_watermark_trigger() -> str:
    return (
        "CREATE TRIGGER trg_canonical_resolution_snapshot_watermark_exact "
        "BEFORE INSERT ON canonical_fact_resolution_snapshot_watermarks WHEN "
        "NOT EXISTS ("
        "SELECT 1 FROM canonical_fact_resolution_snapshot_seals AS snapshot "
        "WHERE snapshot.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "AND snapshot.cutoff_at=NEW.cutoff_at "
        "AND snapshot.recorded_at<=NEW.recorded_at) "
        f"OR (NEW.publication_high_watermark=0 AND "
        f"NEW.high_watermark_event_sha256<>'{'0' * 64}') "
        f"OR (NEW.publication_high_watermark>0 AND "
        f"NEW.high_watermark_event_sha256='{'0' * 64}') "
        "OR (NEW.publication_high_watermark>0 AND NOT EXISTS ("
        "SELECT 1 FROM source_fact_publication_stream AS event "
        "WHERE event.publication_sequence=NEW.publication_high_watermark "
        "AND event.stream_id=NEW.stream_id "
        "AND event.event_sha256=NEW.high_watermark_event_sha256 "
        "AND julianday(event.sealed_at)<=julianday(NEW.cutoff_at))) "
        "OR EXISTS ("
        "SELECT 1 FROM source_fact_publication_stream AS later "
        "WHERE later.stream_id=NEW.stream_id "
        "AND later.publication_sequence>NEW.publication_high_watermark "
        "AND julianday(later.sealed_at)<=julianday(NEW.cutoff_at)) "
        "OR NEW.canonical_watermark_json <> "
        "canonical_resolution_snapshot_watermark_v1("
        "NEW.resolution_snapshot_id,NEW.stream_id,"
        "NEW.publication_high_watermark,"
        "NEW.high_watermark_event_sha256,NEW.cutoff_at,NEW.recorded_at) "
        "OR NEW.watermark_sha256<>fact_sha256(NEW.canonical_watermark_json) "
        "BEGIN SELECT RAISE(ABORT, "
        "'canonical resolution snapshot watermark mismatch'); END"
    )


def upgrade() -> None:
    plane_literals = ",".join(f"'{plane}'" for plane in _REQUIRED_PLANES)
    gate_literals = ",".join(f"'{gate}'" for gate in _REQUIRED_GATES)
    op.create_table(
        "population_run_headers",
        sa.Column("population_run_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("policy_name", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("policy_config_sha256", sa.String(64), nullable=False),
        sa.Column("source_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("knowledge_cutoff", sa.DateTime(), nullable=False),
        sa.Column("observed_through", sa.DateTime(), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=False),
        sa.Column("canonical_identity_json", sa.Text(), nullable=False),
        sa.Column("identity_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint(
            f"{_hex('policy_config_sha256')} "
            f"AND {_hex('source_snapshot_sha256')} "
            f"AND {_hex('identity_sha256')} "
            "AND json_valid(canonical_identity_json) "
            "AND json_type(canonical_identity_json)='object'",
            name="ck_population_run_hashes",
        ),
        sa.CheckConstraint(
            "observed_through>=knowledge_cutoff AND verified_at>=observed_through",
            name="ck_population_run_clocks",
        ),
        sa.UniqueConstraint(
            "policy_config_sha256",
            "source_snapshot_sha256",
            "knowledge_cutoff",
            "observed_through",
            name="uq_population_run_evidence_coordinate",
        ),
    )
    op.create_table(
        "population_plane_receipts",
        sa.Column(
            "population_run_id",
            sa.String(128),
            sa.ForeignKey("population_run_headers.population_run_id"),
            primary_key=True,
        ),
        sa.Column("plane_name", sa.String(64), primary_key=True),
        sa.Column("expected_count", sa.Integer(), nullable=False),
        sa.Column("materialized_count", sa.Integer(), nullable=False),
        sa.Column("excluded_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("input_commitment_sha256", sa.String(64), nullable=False),
        sa.Column("output_commitment_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("canonical_details_json", sa.Text(), nullable=False),
        sa.Column("details_sha256", sa.String(64), nullable=False),
        sa.Column("knowledge_cutoff", sa.DateTime(), nullable=False),
        sa.Column("observed_through", sa.DateTime(), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            f"plane_name IN ({plane_literals})",
            name="ck_population_plane_name",
        ),
        sa.CheckConstraint(
            "expected_count>0 AND materialized_count>=0 "
            "AND excluded_count>=0 AND failed_count>=0 "
            "AND expected_count=materialized_count+excluded_count+failed_count",
            name="ck_population_plane_conservation",
        ),
        sa.CheckConstraint(
            "status IN ('complete','blocked') "
            "AND ((status='complete' AND failed_count=0 AND materialized_count>0) "
            "OR (status='blocked' AND (failed_count>0 OR materialized_count=0)))",
            name="ck_population_plane_status",
        ),
        sa.CheckConstraint(
            f"{_hex('input_commitment_sha256')} "
            f"AND {_hex('output_commitment_sha256')} "
            f"AND {_hex('details_sha256')} "
            "AND json_valid(canonical_details_json) "
            "AND json_type(canonical_details_json)='object'",
            name="ck_population_plane_payload",
        ),
    )
    op.create_table(
        "population_parity_receipts",
        sa.Column(
            "population_run_id",
            sa.String(128),
            sa.ForeignKey("population_run_headers.population_run_id"),
            primary_key=True,
        ),
        sa.Column("eligible_legacy_count", sa.Integer(), nullable=False),
        sa.Column("canonical_count", sa.Integer(), nullable=False),
        sa.Column("matched_count", sa.Integer(), nullable=False),
        sa.Column("mismatched_count", sa.Integer(), nullable=False),
        sa.Column("absent_count", sa.Integer(), nullable=False),
        sa.Column("extra_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("canonical_report_json", sa.Text(), nullable=False),
        sa.Column("report_sha256", sa.String(64), nullable=False),
        sa.Column("knowledge_cutoff", sa.DateTime(), nullable=False),
        sa.Column("observed_through", sa.DateTime(), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "eligible_legacy_count>0 AND canonical_count>0 "
            "AND matched_count>=0 AND mismatched_count>=0 "
            "AND absent_count>=0 AND extra_count>=0 "
            "AND eligible_legacy_count=matched_count+mismatched_count+absent_count "
            "AND canonical_count=matched_count+extra_count",
            name="ck_population_parity_conservation",
        ),
        sa.CheckConstraint(
            "status IN ('complete','blocked') "
            "AND ((status='complete' AND mismatched_count=0 "
            "AND absent_count=0 AND extra_count=0) "
            "OR (status='blocked' AND "
            "(mismatched_count>0 OR absent_count>0 OR extra_count>0)))",
            name="ck_population_parity_status",
        ),
        sa.CheckConstraint(
            f"{_hex('report_sha256')} "
            "AND json_valid(canonical_report_json) "
            "AND json_type(canonical_report_json)='object'",
            name="ck_population_parity_payload",
        ),
    )
    op.create_table(
        "population_cutover_audit_receipts",
        sa.Column(
            "population_run_id",
            sa.String(128),
            sa.ForeignKey("population_run_headers.population_run_id"),
            primary_key=True,
        ),
        sa.Column("verifier_name", sa.String(128), nullable=False),
        sa.Column("verifier_version", sa.String(64), nullable=False),
        sa.Column("verifier_code_sha256", sa.String(64), nullable=False),
        sa.Column("verifier_config_sha256", sa.String(64), nullable=False),
        sa.Column("knowledge_cutoff", sa.DateTime(), nullable=False),
        sa.Column("observed_through", sa.DateTime(), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=False),
        sa.Column("required_gate_count", sa.Integer(), nullable=False),
        sa.Column("eligible_count", sa.Integer(), nullable=False),
        sa.Column("verified_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("canonical_evidence_json", sa.Text(), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_receipt_json", sa.Text(), nullable=False),
        sa.Column("receipt_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "verifier_name='population-cutover-readiness-auditor' "
            "AND verifier_version='2' "
            f"AND {_hex('verifier_code_sha256')} "
            f"AND {_hex('verifier_config_sha256')} "
            f"AND {_hex('evidence_sha256')} "
            f"AND {_hex('receipt_sha256')}",
            name="ck_population_cutover_audit_identity",
        ),
        sa.CheckConstraint(
            "required_gate_count=13 AND eligible_count>0 "
            "AND verified_count=eligible_count AND failed_count=0",
            name="ck_population_cutover_audit_counts",
        ),
        sa.CheckConstraint(
            "observed_through>=knowledge_cutoff "
            "AND verified_at>=observed_through "
            "AND json_valid(canonical_evidence_json) "
            "AND json_type(canonical_evidence_json)='object' "
            "AND json_valid(canonical_receipt_json) "
            "AND json_type(canonical_receipt_json)='object'",
            name="ck_population_cutover_audit_payload",
        ),
    )
    op.create_table(
        "population_cutover_receipts",
        sa.Column(
            "population_run_id",
            sa.String(128),
            sa.ForeignKey("population_run_headers.population_run_id"),
            primary_key=True,
        ),
        sa.Column("required_plane_count", sa.Integer(), nullable=False),
        sa.Column("complete_plane_count", sa.Integer(), nullable=False),
        sa.Column("audit_receipt_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_receipt_set_json", sa.Text(), nullable=False),
        sa.Column("receipt_set_sha256", sa.String(64), nullable=False),
        sa.Column("knowledge_cutoff", sa.DateTime(), nullable=False),
        sa.Column("observed_through", sa.DateTime(), nullable=False),
        sa.Column("sealed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            f"required_plane_count={len(_REQUIRED_PLANES)} "
            f"AND complete_plane_count={len(_REQUIRED_PLANES)}",
            name="ck_population_cutover_plane_counts",
        ),
        sa.CheckConstraint(
            f"{_hex('audit_receipt_sha256')} "
            f"AND {_hex('receipt_set_sha256')} "
            "AND json_valid(canonical_receipt_set_json) "
            "AND json_type(canonical_receipt_set_json)='object' "
            "AND observed_through>=knowledge_cutoff "
            "AND sealed_at>=observed_through",
            name="ck_population_cutover_payload",
        ),
    )
    op.add_column(
        "ask_retrieval_scope_promotions",
        sa.Column("population_run_id", sa.String(128), nullable=True),
    )
    op.add_column(
        "ask_retrieval_scope_promotions",
        sa.Column(
            "population_receipt_set_sha256",
            sa.String(64),
            nullable=True,
        ),
    )
    op.add_column(
        "ask_retrieval_scope_promotions",
        sa.Column("population_observed_through", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "heterogeneous_retrieval_trace_headers",
        sa.Column("population_run_id", sa.String(128), nullable=True),
    )
    op.add_column(
        "heterogeneous_retrieval_trace_headers",
        sa.Column(
            "population_receipt_set_sha256",
            sa.String(64),
            nullable=True,
        ),
    )
    op.add_column(
        "heterogeneous_retrieval_trace_headers",
        sa.Column("population_observed_through", sa.DateTime(), nullable=True),
    )
    op.execute("DROP TRIGGER trg_canonical_fact_snapshot_exact")
    op.execute(_canonical_snapshot_trigger(recorded_clock="NEW.recorded_at"))
    op.execute("DROP TRIGGER trg_canonical_resolution_snapshot_watermark_exact")
    op.execute(_canonical_watermark_trigger(observed_clock="NEW.recorded_at"))
    op.execute(
        "CREATE TRIGGER trg_ask_retrieval_scope_promotion_population_cutover "
        "BEFORE INSERT ON ask_retrieval_scope_promotions WHEN "
        "NEW.population_run_id IS NULL "
        "OR NEW.population_receipt_set_sha256 IS NULL "
        "OR NEW.population_observed_through IS NULL "
        f"OR NOT ({_hex('NEW.population_receipt_set_sha256')}) "
        "OR NOT EXISTS (SELECT 1 FROM population_run_headers run "
        "JOIN population_cutover_receipts receipt "
        "ON receipt.population_run_id=run.population_run_id "
        "WHERE run.population_run_id=NEW.population_run_id "
        "AND receipt.receipt_set_sha256=NEW.population_receipt_set_sha256 "
        "AND datetime(run.knowledge_cutoff)=datetime(NEW.cutoff_at) "
        "AND datetime(run.observed_through)="
        "datetime(NEW.population_observed_through) "
        "AND datetime(receipt.knowledge_cutoff)=datetime(NEW.cutoff_at) "
        "AND datetime(receipt.observed_through)="
        "datetime(NEW.population_observed_through)) "
        "OR NOT EXISTS (SELECT 1 FROM v_population_cutover_current current "
        "WHERE current.population_run_id=NEW.population_run_id "
        "AND current.receipt_set_sha256=NEW.population_receipt_set_sha256 "
        "AND datetime(current.knowledge_cutoff)=datetime(NEW.cutoff_at) "
        "AND datetime(current.observed_through)="
        "datetime(NEW.population_observed_through)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'Ask retrieval promotion requires its exact current population cutover'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_heterogeneous_trace_population_cutover "  # nosec B608 -- fixed internal migration DDL; no external values
        "BEFORE INSERT ON heterogeneous_retrieval_trace_headers WHEN "
        "(NEW.population_run_id IS NOT NULL "
        "OR NEW.population_receipt_set_sha256 IS NOT NULL "
        "OR NEW.population_observed_through IS NOT NULL) "
        "AND (NEW.population_run_id IS NULL "
        "OR NEW.population_receipt_set_sha256 IS NULL "
        "OR NEW.population_observed_through IS NULL "
        f"OR NOT ({_hex('NEW.population_receipt_set_sha256')}) "
        "OR NOT EXISTS (SELECT 1 FROM population_run_headers run "
        "JOIN population_cutover_receipts receipt "
        "ON receipt.population_run_id=run.population_run_id "
        "WHERE run.population_run_id=NEW.population_run_id "
        "AND receipt.receipt_set_sha256=NEW.population_receipt_set_sha256 "
        "AND datetime(run.knowledge_cutoff)=datetime(NEW.cutoff_at) "
        "AND datetime(run.observed_through)="
        "datetime(NEW.population_observed_through))) "
        "BEGIN SELECT RAISE(ABORT, "
        "'heterogeneous trace population cutover mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_population_run_header_exact "
        "BEFORE INSERT ON population_run_headers WHEN "
        "NEW.identity_sha256<>fact_sha256(NEW.canonical_identity_json) "
        "OR NEW.population_run_id<>'population-run:'||NEW.identity_sha256 "
        "OR NEW.idempotency_key<>NEW.population_run_id "
        "OR (SELECT COUNT(*) FROM json_each(NEW.canonical_identity_json))<>5 "
        "OR EXISTS (SELECT 1 FROM json_each(NEW.canonical_identity_json) "
        "WHERE key NOT IN ('knowledge_cutoff','observed_through',"
        "'policy_config_sha256','source_snapshot_sha256','version')) "
        "OR json_extract(NEW.canonical_identity_json,'$.version')"
        "<>'population-run-identity.v2' "
        "OR json_extract(NEW.canonical_identity_json,'$.policy_config_sha256')"
        "<>NEW.policy_config_sha256 "
        "OR json_extract(NEW.canonical_identity_json,'$.source_snapshot_sha256')"
        "<>NEW.source_snapshot_sha256 "
        "OR datetime(json_extract(NEW.canonical_identity_json,'$.knowledge_cutoff'))"
        "<>datetime(NEW.knowledge_cutoff) "
        "OR datetime(json_extract(NEW.canonical_identity_json,'$.observed_through'))"
        "<>datetime(NEW.observed_through) "
        "OR datetime(NEW.observed_through)<datetime(NEW.knowledge_cutoff) "
        "OR datetime(NEW.verified_at)<datetime(NEW.observed_through) "
        "BEGIN SELECT RAISE(ABORT, 'population run header mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_population_plane_receipt_exact "
        "BEFORE INSERT ON population_plane_receipts WHEN "
        "NEW.details_sha256 IS NULL "
        "OR NEW.details_sha256<>fact_sha256(NEW.canonical_details_json) "
        "OR (SELECT COUNT(*) FROM json_each(NEW.canonical_details_json))<>5 "
        "OR EXISTS (SELECT 1 FROM json_each(NEW.canonical_details_json) "
        "WHERE key NOT IN ('artifact_sets','exclusion_counts','result',"
        "'temporal_scope','verifier')) "
        "OR json_type(NEW.canonical_details_json,'$.artifact_sets') IS NOT 'array' "
        "OR json_array_length(json_extract("
        "NEW.canonical_details_json,'$.artifact_sets'))=0 "
        "OR EXISTS (SELECT 1 FROM json_each("
        "NEW.canonical_details_json,'$.artifact_sets') artifact "
        "WHERE (SELECT COUNT(*) FROM json_each(artifact.value))<>4 "
        "OR EXISTS (SELECT 1 FROM json_each(artifact.value) field "
        "WHERE field.key NOT IN ('row_count','rows_sha256',"
        "'selection_policy_id','table')) "
        "OR json_type(artifact.value,'$.row_count') IS NOT 'integer' "
        "OR json_extract(artifact.value,'$.row_count')<0 "
        "OR length(json_extract(artifact.value,'$.rows_sha256'))<>64 "
        "OR json_extract(artifact.value,'$.rows_sha256') GLOB '*[^0-9a-f]*' "
        "OR NULLIF(json_extract(artifact.value,'$.selection_policy_id'),'') IS NULL "
        "OR NULLIF(json_extract(artifact.value,'$.table'),'') IS NULL) "
        "OR json_type(NEW.canonical_details_json,'$.temporal_scope') IS NOT 'object' "
        "OR (SELECT COUNT(*) FROM json_each("
        "NEW.canonical_details_json,'$.temporal_scope'))<>2 "
        "OR datetime(json_extract(NEW.canonical_details_json,"
        "'$.temporal_scope.knowledge_cutoff'))<>datetime(NEW.knowledge_cutoff) "
        "OR datetime(json_extract(NEW.canonical_details_json,"
        "'$.temporal_scope.observed_through'))<>datetime(NEW.observed_through) "
        "OR json_type(NEW.canonical_details_json,'$.verifier') IS NOT 'object' "
        "OR json_type(NEW.canonical_details_json,'$.exclusion_counts') IS NOT 'object' "
        "OR json_type(NEW.canonical_details_json,'$.result') IS NOT 'object' "
        "OR (NEW.plane_name='retrieval_runtime' AND ("
        "json_type(NEW.canonical_details_json,'$.result.governance') IS NOT 'object' "
        "OR (SELECT COUNT(*) FROM json_each("
        "NEW.canonical_details_json,'$.result.governance'))<>8 "
        "OR EXISTS (SELECT 1 FROM json_each("
        "NEW.canonical_details_json,'$.result.governance') governance_field "
        "WHERE governance_field.key NOT IN ('evaluation_receipt_id',"
        "'evaluation_evaluated_at','promotion_id','promotion_recorded_at',"
        "'projection_seal_ids','projection_sealed_at','runtime_registered_at',"
        "'runtime_registration_id')) "
        "OR NULLIF(json_extract(NEW.canonical_details_json,"
        "'$.result.governance.evaluation_receipt_id'),'') IS NULL "
        "OR NULLIF(json_extract(NEW.canonical_details_json,"
        "'$.result.governance.promotion_id'),'') IS NULL "
        "OR NULLIF(json_extract(NEW.canonical_details_json,"
        "'$.result.governance.runtime_registration_id'),'') IS NULL "
        "OR json_type(NEW.canonical_details_json,"
        "'$.result.governance.projection_seal_ids') IS NOT 'array' "
        "OR json_array_length(json_extract(NEW.canonical_details_json,"
        "'$.result.governance.projection_seal_ids'))=0 "
        "OR json_type(NEW.canonical_details_json,"
        "'$.result.governance.projection_sealed_at') IS NOT 'object' "
        "OR (SELECT COUNT(*) FROM json_each(NEW.canonical_details_json,"
        "'$.result.governance.projection_seal_ids'))<>"
        "(SELECT COUNT(*) FROM json_each(NEW.canonical_details_json,"
        "'$.result.governance.projection_sealed_at')) "
        "OR EXISTS (SELECT 1 FROM json_each(NEW.canonical_details_json,"
        "'$.result.governance.projection_seal_ids') seal_id "
        "WHERE seal_id.type<>'text' OR NULLIF(seal_id.value,'') IS NULL "
        "OR NOT EXISTS (SELECT 1 FROM json_each(NEW.canonical_details_json,"
        "'$.result.governance.projection_sealed_at') seal_clock "
        "WHERE seal_clock.key=seal_id.value AND seal_clock.type='text')) "
        "OR datetime(json_extract(NEW.canonical_details_json,"
        "'$.result.governance.evaluation_evaluated_at'))>datetime(NEW.observed_through) "
        "OR datetime(json_extract(NEW.canonical_details_json,"
        "'$.result.governance.promotion_recorded_at'))>datetime(NEW.observed_through) "
        "OR datetime(json_extract(NEW.canonical_details_json,"
        "'$.result.governance.runtime_registered_at'))>datetime(NEW.observed_through) "
        "OR EXISTS (SELECT 1 FROM json_each(NEW.canonical_details_json,"
        "'$.result.governance.projection_sealed_at') seal_clock "
        "WHERE datetime(seal_clock.value)>datetime(NEW.observed_through)))) "
        "OR (SELECT COUNT(*) FROM json_each("
        "NEW.canonical_details_json,'$.verifier'))<>4 "
        "OR EXISTS (SELECT 1 FROM json_each("
        "NEW.canonical_details_json,'$.verifier') "
        "WHERE key NOT IN ('code_sha256','name','result_sha256','version')) "
        "OR NULLIF(json_extract("
        "NEW.canonical_details_json,'$.verifier.name'),'') IS NULL "
        "OR NULLIF(json_extract("
        "NEW.canonical_details_json,'$.verifier.version'),'') IS NULL "
        "OR json_type(NEW.canonical_details_json,"
        "'$.verifier.code_sha256') IS NOT 'text' "
        "OR json_type(NEW.canonical_details_json,"
        "'$.verifier.result_sha256') IS NOT 'text' "
        "OR length(json_extract(NEW.canonical_details_json,'$.verifier.code_sha256'))<>64 "
        "OR json_extract(NEW.canonical_details_json,'$.verifier.code_sha256') "
        "GLOB '*[^0-9a-f]*' "
        "OR length(json_extract(NEW.canonical_details_json,'$.verifier.result_sha256'))<>64 "
        "OR json_extract(NEW.canonical_details_json,'$.verifier.result_sha256') "
        "GLOB '*[^0-9a-f]*' "
        "OR json_extract(NEW.canonical_details_json,'$.verifier.result_sha256')"
        "<>fact_sha256(json_extract(NEW.canonical_details_json,'$.result')) "
        "OR NEW.excluded_count<>COALESCE((SELECT SUM(CAST(value AS INTEGER)) "
        "FROM json_each(NEW.canonical_details_json,'$.exclusion_counts')),0) "
        "OR EXISTS (SELECT 1 "
        "FROM json_each(NEW.canonical_details_json,'$.exclusion_counts') "
        "WHERE type<>'integer' OR CAST(value AS INTEGER)<0) "
        "OR (NEW.plane_name IN ('identity_scope','canonical_resolution',"
        "'canonical_projection','research_snapshot','retrieval_runtime') "
        "AND NEW.excluded_count<>0) "
        "OR (NEW.plane_name='source_fact_ontology' AND EXISTS (SELECT 1 "
        "FROM json_each(NEW.canonical_details_json,'$.exclusion_counts') "
        "WHERE key NOT IN ('after_data_cutoff','derived_without_formula_lineage',"
        "'incomplete_extraction_run','llm_synthesized_source',"
        "'no_selected_subject_binding_as_of_cutoff','unapproved_document_type'))) "
        "OR (NEW.plane_name='document_processing' AND EXISTS (SELECT 1 "
        "FROM json_each(NEW.canonical_details_json,'$.exclusion_counts') "
        "WHERE key NOT IN ('expected_document_not_current',"
        "'sec_form_outside_reporting_policy','sec_supporting_artifact',"
        "'sec_xbrl_report_attachment'))) "
        "OR datetime(NEW.knowledge_cutoff)<>datetime((SELECT knowledge_cutoff "
        "FROM population_run_headers WHERE population_run_id=NEW.population_run_id)) "
        "OR datetime(NEW.observed_through)<>datetime((SELECT observed_through "
        "FROM population_run_headers WHERE population_run_id=NEW.population_run_id)) "
        "OR datetime(NEW.verified_at)<datetime(NEW.observed_through) "
        "BEGIN SELECT RAISE(ABORT, 'population plane receipt mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_population_parity_receipt_exact "
        "BEFORE INSERT ON population_parity_receipts WHEN "
        "NEW.report_sha256<>fact_sha256(NEW.canonical_report_json) "
        "OR datetime(NEW.knowledge_cutoff)<>datetime((SELECT knowledge_cutoff "
        "FROM population_run_headers WHERE population_run_id=NEW.population_run_id)) "
        "OR datetime(NEW.observed_through)<>datetime((SELECT observed_through "
        "FROM population_run_headers WHERE population_run_id=NEW.population_run_id)) "
        "OR datetime(NEW.verified_at)<datetime(NEW.observed_through) "
        "BEGIN SELECT RAISE(ABORT, 'population parity receipt mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_population_cutover_audit_receipt_exact "
        "BEFORE INSERT ON population_cutover_audit_receipts WHEN "
        "NEW.evidence_sha256 IS NULL "
        "OR NEW.receipt_sha256 IS NULL "
        "OR NEW.evidence_sha256<>fact_sha256(NEW.canonical_evidence_json) "
        "OR NEW.receipt_sha256<>fact_sha256(NEW.canonical_receipt_json) "
        "OR datetime(NEW.knowledge_cutoff)<>datetime((SELECT knowledge_cutoff "
        "FROM population_run_headers WHERE population_run_id=NEW.population_run_id)) "
        "OR datetime(NEW.observed_through)<>datetime((SELECT observed_through "
        "FROM population_run_headers WHERE population_run_id=NEW.population_run_id)) "
        "OR datetime(NEW.verified_at)<datetime(NEW.observed_through) "
        "OR (SELECT COUNT(*) FROM json_each(NEW.canonical_evidence_json))<>8 "
        "OR EXISTS (SELECT 1 FROM json_each(NEW.canonical_evidence_json) "
        "WHERE key NOT IN ('coverage','findings','gate_evidence','has_blockers',"
        "'schema_version','tables_present','watermark_material','watermark_sha256')) "
        "OR json_type(NEW.canonical_evidence_json,'$.coverage') IS NOT 'array' "
        "OR json_type(NEW.canonical_evidence_json,'$.gate_evidence') IS NOT 'array' "
        "OR json_type(NEW.canonical_evidence_json,'$.watermark_material') IS NOT 'object' "
        "OR json_type(NEW.canonical_evidence_json,'$.has_blockers') IS NOT 'false' "
        "OR json_extract(NEW.canonical_evidence_json,'$.schema_version') "
        "IS NOT 'data-cutover-readiness-audit/v1' "
        "OR json_type(NEW.canonical_evidence_json,'$.findings') IS NOT 'array' "
        "OR json_type(NEW.canonical_evidence_json,'$.tables_present') IS NOT 'array' "
        "OR EXISTS (SELECT 1 FROM json_each("
        "NEW.canonical_evidence_json,'$.findings') finding "
        "WHERE (SELECT COUNT(*) FROM json_each(finding.value))<>6 "
        "OR EXISTS (SELECT 1 FROM json_each(finding.value) finding_field "
        "WHERE finding_field.key NOT IN ('code','count','query_context',"
        "'remediation','samples','severity')) "
        "OR NULLIF(json_extract(finding.value,'$.code'),'') IS NULL "
        "OR json_type(finding.value,'$.count') IS NOT 'integer' "
        "OR json_extract(finding.value,'$.count')<0 "
        "OR json_type(finding.value,'$.query_context') IS NOT 'text' "
        "OR json_extract(finding.value,'$.remediation') "
        "NOT IN ('repairable','backfill','reingest','manual','hard-stop') "
        "OR json_type(finding.value,'$.samples') IS NOT 'array' "
        "OR json_extract(finding.value,'$.severity') "
        "NOT IN ('advisory','warning')) "
        "OR EXISTS (SELECT 1 FROM json_each("
        "NEW.canonical_evidence_json,'$.tables_present') table_name "
        "WHERE table_name.type<>'text' OR NULLIF(table_name.value,'') IS NULL) "
        "OR (SELECT COUNT(*) FROM json_each("
        "NEW.canonical_evidence_json,'$.tables_present'))<>"
        "(SELECT COUNT(DISTINCT value) FROM json_each("
        "NEW.canonical_evidence_json,'$.tables_present')) "
        "OR json_extract(NEW.canonical_evidence_json,'$.tables_present')<>"
        "COALESCE((SELECT json_group_array(value) FROM (SELECT value "
        "FROM json_each(NEW.canonical_evidence_json,'$.tables_present') "
        "ORDER BY value)),'[]') "
        f"OR (SELECT COUNT(*) FROM json_each("
        "NEW.canonical_evidence_json,'$.coverage'))<>13 "
        f"OR (SELECT COUNT(DISTINCT json_extract(value,'$.gate')) FROM json_each("
        "NEW.canonical_evidence_json,'$.coverage'))<>13 "
        f"OR EXISTS (SELECT 1 FROM json_each("
        "NEW.canonical_evidence_json,'$.coverage') coverage_row "
        f"WHERE json_extract(coverage_row.value,'$.gate') NOT IN ({gate_literals}) "
        "OR (SELECT COUNT(*) FROM json_each(coverage_row.value))<>4 "
        "OR EXISTS (SELECT 1 FROM json_each(coverage_row.value) coverage_field "
        "WHERE coverage_field.key NOT IN ('eligible_count','failed_count','gate',"
        "'verified_count')) "
        "OR json_type(coverage_row.value,'$.eligible_count') IS NOT 'integer' "
        "OR json_type(coverage_row.value,'$.verified_count') IS NOT 'integer' "
        "OR json_type(coverage_row.value,'$.failed_count') IS NOT 'integer' "
        "OR json_extract(coverage_row.value,'$.eligible_count')<0 "
        "OR json_extract(coverage_row.value,'$.verified_count')<0 "
        "OR json_extract(coverage_row.value,'$.failed_count')<0 "
        "OR json_extract(coverage_row.value,'$.eligible_count')<>"
        "json_extract(coverage_row.value,'$.verified_count')+"
        "json_extract(coverage_row.value,'$.failed_count')) "
        "OR NEW.eligible_count<>(SELECT SUM(json_extract(value,'$.eligible_count')) "
        "FROM json_each(NEW.canonical_evidence_json,'$.coverage')) "
        "OR NEW.verified_count<>(SELECT SUM(json_extract(value,'$.verified_count')) "
        "FROM json_each(NEW.canonical_evidence_json,'$.coverage')) "
        "OR NEW.failed_count<>(SELECT SUM(json_extract(value,'$.failed_count')) "
        "FROM json_each(NEW.canonical_evidence_json,'$.coverage')) "
        "OR (SELECT COUNT(*) FROM json_each("
        "NEW.canonical_evidence_json,'$.gate_evidence'))<>13 "
        "OR (SELECT COUNT(DISTINCT json_extract(value,'$.gate')) FROM json_each("
        "NEW.canonical_evidence_json,'$.gate_evidence'))<>13 "
        "OR EXISTS (SELECT 1 FROM json_each("
        "NEW.canonical_evidence_json,'$.gate_evidence') evidence_row "
        f"WHERE json_extract(evidence_row.value,'$.gate') NOT IN ({gate_literals}) "
        "OR (SELECT COUNT(*) FROM json_each(evidence_row.value))<>3 "
        "OR EXISTS (SELECT 1 FROM json_each(evidence_row.value) evidence_field "
        "WHERE evidence_field.key NOT IN ('gate','gate_evidence_sha256','tables')) "
        "OR json_type(evidence_row.value,'$.tables') IS NOT 'array' "
        "OR json_type(evidence_row.value,'$.gate_evidence_sha256') IS NOT 'text' "
        "OR length(json_extract(evidence_row.value,'$.gate_evidence_sha256'))<>64 "
        "OR json_extract(evidence_row.value,'$.gate_evidence_sha256') "
        "GLOB '*[^0-9a-f]*' "
        "OR json_extract(evidence_row.value,'$.gate_evidence_sha256')"
        "<>fact_sha256(json_object("
        "'gate',json_extract(evidence_row.value,'$.gate'),"
        "'tables',json(json_extract(evidence_row.value,'$.tables'))))) "
        "OR EXISTS (SELECT 1 FROM json_each("
        "NEW.canonical_evidence_json,'$.gate_evidence') gate_row,"
        "json_each(gate_row.value,'$.tables') table_row "
        "WHERE (SELECT COUNT(*) FROM json_each(table_row.value))<>3 "
        "OR EXISTS (SELECT 1 FROM json_each(table_row.value) table_field "
        "WHERE table_field.key NOT IN ('row_count','rows_sha256','table')) "
        "OR json_type(table_row.value,'$.row_count') IS NOT 'integer' "
        "OR json_extract(table_row.value,'$.row_count')<0 "
        "OR json_type(table_row.value,'$.rows_sha256') IS NOT 'text' "
        "OR length(json_extract(table_row.value,'$.rows_sha256'))<>64 "
        "OR json_extract(table_row.value,'$.rows_sha256') GLOB '*[^0-9a-f]*' "
        "OR NULLIF(json_extract(table_row.value,'$.table'),'') IS NULL) "
        "OR (SELECT COUNT(*) FROM json_each("
        "NEW.canonical_evidence_json,'$.watermark_material'))<>3 "
        "OR EXISTS (SELECT 1 FROM json_each("
        "NEW.canonical_evidence_json,'$.watermark_material') "
        "WHERE key NOT IN ('gates','knowledge_cutoff','observed_through')) "
        "OR json_type(NEW.canonical_evidence_json,"
        "'$.watermark_material.knowledge_cutoff') IS NOT 'text' "
        "OR datetime(json_extract("
        "NEW.canonical_evidence_json,'$.watermark_material.knowledge_cutoff'))"
        "<>datetime(NEW.knowledge_cutoff) "
        "OR json_type(NEW.canonical_evidence_json,"
        "'$.watermark_material.observed_through') IS NOT 'text' "
        "OR datetime(json_extract("
        "NEW.canonical_evidence_json,'$.watermark_material.observed_through'))"
        "<>datetime(NEW.observed_through) "
        "OR json_type("
        "NEW.canonical_evidence_json,'$.watermark_material.gates') IS NOT 'array' "
        "OR (SELECT COUNT(*) FROM json_each("
        "NEW.canonical_evidence_json,'$.watermark_material.gates'))<>13 "
        "OR (SELECT COUNT(DISTINCT json_extract(value,'$.gate')) FROM json_each("
        "NEW.canonical_evidence_json,'$.watermark_material.gates'))<>13 "
        "OR EXISTS (SELECT 1 FROM json_each("
        "NEW.canonical_evidence_json,'$.watermark_material.gates') material "
        "WHERE (SELECT COUNT(*) FROM json_each(material.value))<>2 "
        "OR EXISTS (SELECT 1 FROM json_each(material.value) material_field "
        "WHERE material_field.key NOT IN ('gate','gate_evidence_sha256')) "
        f"OR json_extract(material.value,'$.gate') NOT IN ({gate_literals}) "
        "OR json_type(material.value,'$.gate_evidence_sha256') IS NOT 'text' "
        "OR length(json_extract(material.value,'$.gate_evidence_sha256'))<>64 "
        "OR json_extract(material.value,'$.gate_evidence_sha256') "
        "GLOB '*[^0-9a-f]*' "
        "OR NOT EXISTS (SELECT 1 FROM json_each("
        "NEW.canonical_evidence_json,'$.gate_evidence') evidence "
        "WHERE json_extract(evidence.value,'$.gate')="
        "json_extract(material.value,'$.gate') "
        "AND json_extract(evidence.value,'$.gate_evidence_sha256')="
        "json_extract(material.value,'$.gate_evidence_sha256'))) "
        "OR json_type(NEW.canonical_evidence_json,"
        "'$.watermark_sha256') IS NOT 'text' "
        "OR length(json_extract("
        "NEW.canonical_evidence_json,'$.watermark_sha256'))<>64 "
        "OR json_extract(NEW.canonical_evidence_json,'$.watermark_sha256')"
        "<>fact_sha256(json_extract("
        "NEW.canonical_evidence_json,'$.watermark_material')) "
        "OR (SELECT COUNT(*) FROM json_each(NEW.canonical_receipt_json))<>14 "
        "OR EXISTS (SELECT 1 FROM json_each(NEW.canonical_receipt_json) "
        "WHERE key NOT IN ('audit_version','knowledge_cutoff','observed_through',"
        "'eligible_count','evidence_sha256','failed_count','population_run_id',"
        "'required_gate_count','verified_count','verifier_code_sha256',"
        "'verifier_config_sha256','verifier_name','verifier_version','verified_at')) "
        "OR json_extract(NEW.canonical_receipt_json,'$.audit_version')"
        " IS NOT 'population-cutover-audit.v2' "
        "OR json_extract(NEW.canonical_receipt_json,'$.population_run_id')"
        " IS NOT NEW.population_run_id "
        "OR json_extract(NEW.canonical_receipt_json,'$.evidence_sha256')"
        " IS NOT NEW.evidence_sha256 "
        "OR json_extract(NEW.canonical_receipt_json,'$.verifier_code_sha256')"
        " IS NOT NEW.verifier_code_sha256 "
        "OR json_extract(NEW.canonical_receipt_json,'$.verifier_config_sha256')"
        " IS NOT NEW.verifier_config_sha256 "
        "OR json_extract(NEW.canonical_receipt_json,'$.verifier_name')"
        " IS NOT NEW.verifier_name "
        "OR json_extract(NEW.canonical_receipt_json,'$.verifier_version')"
        " IS NOT NEW.verifier_version "
        "OR json_extract(NEW.canonical_receipt_json,'$.required_gate_count')"
        " IS NOT NEW.required_gate_count "
        "OR json_extract(NEW.canonical_receipt_json,'$.eligible_count')"
        " IS NOT NEW.eligible_count "
        "OR json_extract(NEW.canonical_receipt_json,'$.verified_count')"
        " IS NOT NEW.verified_count "
        "OR json_extract(NEW.canonical_receipt_json,'$.failed_count')"
        " IS NOT NEW.failed_count "
        "OR json_type(NEW.canonical_receipt_json,'$.knowledge_cutoff') IS NOT 'text' "
        "OR json_type(NEW.canonical_receipt_json,'$.observed_through') IS NOT 'text' "
        "OR json_type(NEW.canonical_receipt_json,'$.verified_at') IS NOT 'text' "
        "OR datetime(json_extract(NEW.canonical_receipt_json,'$.knowledge_cutoff'))"
        "<>datetime(NEW.knowledge_cutoff) "
        "OR datetime(json_extract(NEW.canonical_receipt_json,'$.observed_through'))"
        "<>datetime(NEW.observed_through) "
        "OR datetime(json_extract(NEW.canonical_receipt_json,'$.verified_at'))"
        "<>datetime(NEW.verified_at) "
        "BEGIN SELECT RAISE(ABORT, 'population cutover audit receipt mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_population_cutover_receipt_exact "  # nosec B608 -- fixed internal schema DDL; the required-plane count comes from a closed migration constant
        "BEFORE INSERT ON population_cutover_receipts WHEN "
        f"(SELECT COUNT(*) FROM population_plane_receipts plane "
        "WHERE plane.population_run_id=NEW.population_run_id "
        f"AND plane.status='complete')<>{len(_REQUIRED_PLANES)} "
        "OR EXISTS (SELECT 1 FROM population_plane_receipts plane "
        "WHERE plane.population_run_id=NEW.population_run_id "
        "AND plane.status<>'complete') "
        "OR NOT EXISTS (SELECT 1 FROM population_parity_receipts parity "
        "WHERE parity.population_run_id=NEW.population_run_id "
        "AND parity.status='complete') "
        "OR NOT EXISTS (SELECT 1 FROM population_cutover_audit_receipts audit "
        "WHERE audit.population_run_id=NEW.population_run_id "
        "AND audit.failed_count=0 "
        "AND audit.receipt_sha256=NEW.audit_receipt_sha256) "
        "OR NEW.canonical_receipt_set_json<>json_object("
        "'audit_receipt_sha256',NEW.audit_receipt_sha256,"
        "'parity_report_sha256',(SELECT parity.report_sha256 "
        "FROM population_parity_receipts parity "
        "WHERE parity.population_run_id=NEW.population_run_id),"
        "'plane_receipts',json((SELECT json_group_array(json(item.payload)) "
        "FROM (SELECT json_object("
        "'details_sha256',plane.details_sha256,"
        "'input_commitment_sha256',plane.input_commitment_sha256,"
        "'output_commitment_sha256',plane.output_commitment_sha256,"
        "'plane_name',plane.plane_name,"
        "'status',plane.status) payload "
        "FROM population_plane_receipts plane "
        "WHERE plane.population_run_id=NEW.population_run_id "
        "ORDER BY plane.plane_name) item)),"
        "'population_run_id',NEW.population_run_id,"
        "'receipt_version','population-cutover-receipt.v3',"
        "'temporal_scope',json(json_extract("
        "NEW.canonical_receipt_set_json,'$.temporal_scope'))) "
        "OR NEW.receipt_set_sha256<>fact_sha256(NEW.canonical_receipt_set_json) "
        "OR (SELECT COUNT(*) FROM json_each("
        "NEW.canonical_receipt_set_json,'$.temporal_scope'))<>2 "
        "OR datetime(json_extract(NEW.canonical_receipt_set_json,"
        "'$.temporal_scope.knowledge_cutoff'))<>datetime(NEW.knowledge_cutoff) "
        "OR datetime(json_extract(NEW.canonical_receipt_set_json,"
        "'$.temporal_scope.observed_through'))<>datetime(NEW.observed_through) "
        "OR datetime(NEW.knowledge_cutoff)<>datetime((SELECT knowledge_cutoff "
        "FROM population_run_headers WHERE population_run_id=NEW.population_run_id)) "
        "OR datetime(NEW.observed_through)<>datetime((SELECT observed_through "
        "FROM population_run_headers WHERE population_run_id=NEW.population_run_id)) "
        "OR datetime(NEW.sealed_at)<datetime(NEW.observed_through) "
        "OR NEW.sealed_at<(SELECT MAX(verified_at) FROM ("
        "SELECT verified_at FROM population_plane_receipts "
        "WHERE population_run_id=NEW.population_run_id "
        "UNION ALL SELECT verified_at FROM population_parity_receipts "
        "WHERE population_run_id=NEW.population_run_id "
        "UNION ALL SELECT verified_at FROM population_cutover_audit_receipts "
        "WHERE population_run_id=NEW.population_run_id)) "
        "BEGIN SELECT RAISE(ABORT, 'population cutover receipt mismatch'); END"
    )
    for table in _TABLES:
        _append_only(table)
    op.execute(
        "CREATE VIEW v_population_cutover_current AS "
        "SELECT receipt.*,run.policy_name,run.policy_version,"
        "run.policy_config_sha256,run.source_snapshot_sha256,"
        "run.knowledge_cutoff,run.observed_through,run.verified_at "
        "FROM population_cutover_receipts receipt "
        "JOIN population_run_headers run USING (population_run_id) "
        "ORDER BY datetime(run.knowledge_cutoff) DESC,"
        "datetime(run.observed_through) DESC,"
        "datetime(receipt.sealed_at) DESC,receipt.population_run_id DESC LIMIT 1"
    )


def downgrade() -> None:
    bind = op.get_bind()
    populated = tuple(
        table
        for table in _TABLES
        if bind.execute(  # nosec B608 -- table names come from the closed migration constant
            sa.text(f"SELECT 1 FROM {table} LIMIT 1")
        ).first()
        is not None
    )
    if populated:
        raise RuntimeError(
            "0256 downgrade would destroy sealed population evidence: " + ",".join(populated)
        )
    op.execute("DROP VIEW IF EXISTS v_population_cutover_current")
    op.execute("DROP TRIGGER IF EXISTS trg_ask_retrieval_scope_promotion_population_cutover")
    op.execute("DROP TRIGGER IF EXISTS trg_heterogeneous_trace_population_cutover")
    trigger_rows = bind.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND sql IS NOT NULL ORDER BY name"
        )
    ).fetchall()
    triggers = [(str(row[0]), str(row[1])) for row in trigger_rows]
    for name, _sql in triggers:
        escaped = name.replace('"', '""')
        op.execute(f'DROP TRIGGER "{escaped}"')  # nosec B608 -- exact sqlite_master trigger identity
    try:
        for table, names in (
            (
                "heterogeneous_retrieval_trace_headers",
                _RETRIEVAL_TRACE_COLUMNS,
            ),
            ("ask_retrieval_scope_promotions", _ASK_PROMOTION_COLUMNS),
        ):
            for name in reversed(names):
                op.drop_column(table, name)
    finally:
        for _name, sql in triggers:
            op.execute(sql)
    for trigger in (
        "trg_population_cutover_receipt_exact",
        "trg_population_cutover_audit_receipt_exact",
        "trg_population_parity_receipt_exact",
        "trg_population_plane_receipt_exact",
        "trg_population_run_header_exact",
        *(
            f"trg_{table}_{event}_append_only"
            for table in _TABLES
            for event in ("update", "delete")
        ),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in reversed(_TABLES):
        op.drop_table(table)
    op.execute("DROP TRIGGER trg_canonical_fact_snapshot_exact")
    op.execute(_canonical_snapshot_trigger(recorded_clock="NEW.cutoff_at"))
    op.execute("DROP TRIGGER trg_canonical_resolution_snapshot_watermark_exact")
    op.execute(_legacy_canonical_watermark_trigger())
