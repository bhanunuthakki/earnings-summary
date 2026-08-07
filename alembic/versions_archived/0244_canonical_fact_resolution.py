"""Seal exhaustive cross-QName canonical-fact resolution evidence.

Revision ID: 0244_canonical_fact_resolution
Revises: 0243_metric_ontology
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0244_canonical_fact_resolution"
down_revision: str | Sequence[str] | None = "0243_metric_ontology"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _hex(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _append_only(table: str) -> None:
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_{event.lower()}_append_only BEFORE {event} "
            f"ON {table} BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
        )


def upgrade() -> None:
    required = {
        "fact_observations_v2",
        "fact_observation_payload_commitments_v2",
        "source_fact_publication_members",
        "source_fact_publication_seals",
        "filing_xbrl_extraction_dispositions",
        "filing_xbrl_extraction_disposition_seals",
        "canonical_metric_cells",
        "fact_cell_canonical_binding_revisions",
    }
    missing = sorted(required - set(sa.inspect(op.get_bind()).get_table_names()))
    if missing:
        raise RuntimeError(
            "canonical fact resolution requires sealed admission and ontology: "
            + ", ".join(missing)
        )

    op.create_table(
        "canonical_fact_candidate_universe_revisions",
        sa.Column("candidate_universe_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "canonical_metric_cell_id",
            sa.String(128),
            sa.ForeignKey("canonical_metric_cells.canonical_metric_cell_id"),
            nullable=False,
        ),
        sa.Column("cutoff_at", sa.DateTime, nullable=False),
        sa.Column("member_count", sa.Integer, nullable=False),
        sa.Column("canonical_member_set_json", sa.Text, nullable=False),
        sa.Column("member_set_sha256", sa.String(64), nullable=False),
        sa.Column("effective_at", sa.DateTime, nullable=False),
        sa.Column("knowledge_at", sa.DateTime, nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint(
            "canonical_metric_cell_id",
            "cutoff_at",
            name="uq_canonical_fact_universe_coordinate_cutoff",
        ),
        sa.CheckConstraint(
            "member_count >= 0 AND json_valid(canonical_member_set_json) AND json_type(canonical_member_set_json)='array'",
            name="ck_canonical_fact_universe_shape",
        ),
        sa.CheckConstraint(_hex("member_set_sha256"), name="ck_canonical_fact_universe_hash"),
        sa.CheckConstraint(
            "effective_at <= knowledge_at AND knowledge_at <= recorded_at AND recorded_at >= cutoff_at",
            name="ck_canonical_fact_universe_clocks",
        ),
    )
    op.create_table(
        "canonical_fact_candidate_dispositions",
        sa.Column("candidate_disposition_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "candidate_universe_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_candidate_universe_revisions.candidate_universe_id"),
            nullable=False,
        ),
        sa.Column("candidate_ordinal", sa.Integer, nullable=False),
        sa.Column(
            "observation_id",
            sa.String(128),
            sa.ForeignKey("fact_observations_v2.observation_id"),
            nullable=False,
        ),
        sa.Column("source_fact_cell_id", sa.String(128), nullable=False),
        sa.Column(
            "binding_revision_id",
            sa.String(128),
            sa.ForeignKey("fact_cell_canonical_binding_revisions.binding_revision_id"),
            nullable=False,
        ),
        sa.Column("binding_commitment_sha256", sa.String(64), nullable=False),
        sa.Column("mapping_commitment_sha256", sa.String(64), nullable=False),
        sa.Column("observation_payload_sha256", sa.String(64), nullable=False),
        sa.Column("source_publication_id", sa.String(128)),
        sa.Column("source_publication_seal_id", sa.String(128)),
        sa.Column("source_publication_member_id", sa.String(128)),
        sa.Column("source_publication_member_sha256", sa.String(64)),
        sa.Column("source_record_commitment_sha256", sa.String(64)),
        sa.Column(
            "filing_disposition_id",
            sa.String(128),
            sa.ForeignKey("filing_xbrl_extraction_dispositions.disposition_id"),
        ),
        sa.Column("source_lane", sa.String(32), nullable=False),
        sa.Column("eligibility", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason_details_json", sa.Text, nullable=False),
        sa.Column("effective_at", sa.DateTime, nullable=False),
        sa.Column("knowledge_at", sa.DateTime, nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint(
            "candidate_universe_id", "candidate_ordinal", name="uq_canonical_fact_candidate_ordinal"
        ),
        sa.UniqueConstraint(
            "candidate_universe_id",
            "observation_id",
            name="uq_canonical_fact_candidate_observation",
        ),
        sa.CheckConstraint(
            "candidate_ordinal >= 0 AND eligibility IN ('eligible','ineligible') "
            "AND source_lane IN ('filing_xbrl','reported_source_publication',"
            "'derived_terminal_exclusion','missing_publication_exclusion') "
            "AND ((eligibility='eligible' AND source_publication_id IS NOT NULL "
            "AND source_publication_seal_id IS NOT NULL "
            "AND source_publication_member_id IS NOT NULL "
            "AND source_publication_member_sha256 IS NOT NULL "
            "AND source_record_commitment_sha256 IS NOT NULL) "
            "OR (eligibility='ineligible' AND source_publication_id IS NULL "
            "AND source_publication_seal_id IS NULL "
            "AND source_publication_member_id IS NULL "
            "AND source_publication_member_sha256 IS NULL "
            "AND source_record_commitment_sha256 IS NULL)) "
            "AND json_valid(reason_details_json) "
            "AND json_type(reason_details_json)='object'",
            name="ck_canonical_fact_candidate_shape",
        ),
        sa.CheckConstraint(
            _hex("observation_payload_sha256")
            + " AND "
            + _hex("binding_commitment_sha256")
            + " AND "
            + _hex("mapping_commitment_sha256")
            + " AND (source_publication_member_sha256 IS NULL OR ("
            + _hex("source_publication_member_sha256")
            + ")) AND (source_record_commitment_sha256 IS NULL OR ("
            + _hex("source_record_commitment_sha256")
            + "))",
            name="ck_canonical_fact_candidate_hashes",
        ),
        sa.CheckConstraint(
            "effective_at <= knowledge_at AND knowledge_at <= recorded_at",
            name="ck_canonical_fact_candidate_clocks",
        ),
    )
    op.create_table(
        "canonical_fact_candidate_universe_seals",
        sa.Column(
            "candidate_universe_id",
            sa.String(128),
            sa.ForeignKey(
                "canonical_fact_candidate_universe_revisions.candidate_universe_id"
            ),
            primary_key=True,
        ),
        sa.Column(
            "candidate_universe_seal_id",
            sa.String(128),
            nullable=False,
            unique=True,
        ),
        sa.Column("member_count", sa.Integer, nullable=False),
        sa.Column("canonical_member_set_json", sa.Text, nullable=False),
        sa.Column("member_set_sha256", sa.String(64), nullable=False),
        sa.Column("sealed_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "member_count >= 0 AND json_valid(canonical_member_set_json) "
            "AND json_type(canonical_member_set_json)='array'",
            name="ck_canonical_fact_universe_seal_shape",
        ),
        sa.CheckConstraint(
            _hex("member_set_sha256"),
            name="ck_canonical_fact_universe_seal_hash",
        ),
    )
    op.create_table(
        "canonical_fact_relation_set_revisions",
        sa.Column("relation_set_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "candidate_universe_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_candidate_universe_revisions.candidate_universe_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("relation_count", sa.Integer, nullable=False),
        sa.Column("canonical_relation_set_json", sa.Text, nullable=False),
        sa.Column("relation_set_sha256", sa.String(64), nullable=False),
        sa.Column("effective_at", sa.DateTime, nullable=False),
        sa.Column("knowledge_at", sa.DateTime, nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "relation_count >= 0 AND json_valid(canonical_relation_set_json) AND json_type(canonical_relation_set_json)='array'",
            name="ck_canonical_fact_relation_set_shape",
        ),
        sa.CheckConstraint(_hex("relation_set_sha256"), name="ck_canonical_fact_relation_set_hash"),
        sa.CheckConstraint(
            "effective_at <= knowledge_at AND knowledge_at <= recorded_at",
            name="ck_canonical_fact_relation_set_clocks",
        ),
    )
    op.create_table(
        "canonical_fact_relation_assertions",
        sa.Column("relation_assertion_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "relation_set_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_relation_set_revisions.relation_set_id"),
            nullable=False,
        ),
        sa.Column("relation_ordinal", sa.Integer, nullable=False),
        sa.Column(
            "subject_observation_id",
            sa.String(128),
            sa.ForeignKey("fact_observations_v2.observation_id"),
        ),
        sa.Column(
            "subject_filing_disposition_id",
            sa.String(128),
            sa.ForeignKey("filing_xbrl_extraction_dispositions.disposition_id"),
        ),
        sa.Column(
            "object_observation_id",
            sa.String(128),
            sa.ForeignKey("fact_observations_v2.observation_id"),
            nullable=False,
        ),
        sa.Column("relation_kind", sa.String(32), nullable=False),
        sa.Column("evidence_json", sa.Text, nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("effective_at", sa.DateTime, nullable=False),
        sa.Column("knowledge_at", sa.DateTime, nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint(
            "relation_set_id", "relation_ordinal", name="uq_canonical_fact_relation_ordinal"
        ),
        sa.CheckConstraint(
            "relation_ordinal >= 0 AND relation_kind IN ('source_equivalent_to','exact_duplicate_of','conflicts_with','amends','recasts','supersedes') AND ((subject_observation_id IS NOT NULL) <> (subject_filing_disposition_id IS NOT NULL)) AND json_valid(evidence_json) AND json_type(evidence_json)='object'",
            name="ck_canonical_fact_relation_shape",
        ),
        sa.CheckConstraint(_hex("evidence_sha256"), name="ck_canonical_fact_relation_hash"),
        sa.CheckConstraint(
            "effective_at <= knowledge_at AND knowledge_at <= recorded_at",
            name="ck_canonical_fact_relation_clocks",
        ),
    )
    op.create_table(
        "canonical_fact_relation_set_seals",
        sa.Column(
            "relation_set_id",
            sa.String(128),
            sa.ForeignKey(
                "canonical_fact_relation_set_revisions.relation_set_id"
            ),
            primary_key=True,
        ),
        sa.Column(
            "relation_set_seal_id",
            sa.String(128),
            nullable=False,
            unique=True,
        ),
        sa.Column("relation_count", sa.Integer, nullable=False),
        sa.Column("canonical_relation_set_json", sa.Text, nullable=False),
        sa.Column("relation_set_sha256", sa.String(64), nullable=False),
        sa.Column("sealed_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "relation_count >= 0 AND json_valid(canonical_relation_set_json) "
            "AND json_type(canonical_relation_set_json)='array'",
            name="ck_canonical_fact_relation_seal_shape",
        ),
        sa.CheckConstraint(
            _hex("relation_set_sha256"),
            name="ck_canonical_fact_relation_seal_hash",
        ),
    )
    op.create_table(
        "canonical_fact_resolution_revisions",
        sa.Column("canonical_resolution_revision_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "canonical_metric_cell_id",
            sa.String(128),
            sa.ForeignKey("canonical_metric_cells.canonical_metric_cell_id"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column(
            "candidate_universe_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_candidate_universe_revisions.candidate_universe_id"),
            nullable=False,
        ),
        sa.Column(
            "relation_set_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_relation_set_revisions.relation_set_id"),
            nullable=False,
        ),
        sa.Column(
            "candidate_universe_seal_id",
            sa.String(128),
            sa.ForeignKey(
                "canonical_fact_candidate_universe_seals.candidate_universe_seal_id"
            ),
            nullable=False,
        ),
        sa.Column(
            "relation_set_seal_id",
            sa.String(128),
            sa.ForeignKey(
                "canonical_fact_relation_set_seals.relation_set_seal_id"
            ),
            nullable=False,
        ),
        sa.Column("candidate_universe_sha256", sa.String(64), nullable=False),
        sa.Column("relation_set_sha256", sa.String(64), nullable=False),
        sa.Column("policy_name", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("policy_config_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "selected_observation_id",
            sa.String(128),
            sa.ForeignKey("fact_observations_v2.observation_id"),
        ),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason_details_json", sa.Text, nullable=False),
        sa.Column("canonical_resolution_json", sa.Text, nullable=False),
        sa.Column("resolution_sha256", sa.String(64), nullable=False),
        sa.Column("effective_at", sa.DateTime, nullable=False),
        sa.Column("knowledge_at", sa.DateTime, nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.Column(
            "supersedes_resolution_revision_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_resolution_revisions.canonical_resolution_revision_id"),
        ),
        sa.UniqueConstraint(
            "canonical_metric_cell_id", "revision", name="uq_canonical_fact_resolution_revision"
        ),
        sa.CheckConstraint(
            "revision > 0 AND status IN ('resolved','unresolved','retired') AND ((status='resolved') = (selected_observation_id IS NOT NULL)) AND json_valid(reason_details_json) AND json_type(reason_details_json)='object' AND json_valid(canonical_resolution_json) AND json_type(canonical_resolution_json)='object' AND ((revision=1 AND supersedes_resolution_revision_id IS NULL) OR (revision>1 AND supersedes_resolution_revision_id IS NOT NULL))",
            name="ck_canonical_fact_resolution_shape",
        ),
        sa.CheckConstraint(
            _hex("policy_config_sha256")
            + " AND "
            + _hex("candidate_universe_sha256")
            + " AND "
            + _hex("relation_set_sha256")
            + " AND "
            + _hex("resolution_sha256"),
            name="ck_canonical_fact_resolution_hash",
        ),
        sa.CheckConstraint(
            "effective_at <= knowledge_at AND knowledge_at <= recorded_at",
            name="ck_canonical_fact_resolution_clocks",
        ),
    )
    op.create_table(
        "canonical_fact_resolution_snapshot_seals",
        sa.Column("resolution_snapshot_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("cutoff_at", sa.DateTime, nullable=False),
        sa.Column("member_count", sa.Integer, nullable=False),
        sa.Column("canonical_member_set_json", sa.Text, nullable=False),
        sa.Column("member_set_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "member_count >= 0 AND json_valid(canonical_member_set_json) AND json_type(canonical_member_set_json)='array'",
            name="ck_canonical_fact_snapshot_shape",
        ),
        sa.CheckConstraint(_hex("member_set_sha256"), name="ck_canonical_fact_snapshot_hash"),
        sa.CheckConstraint("recorded_at >= cutoff_at", name="ck_canonical_fact_snapshot_clocks"),
    )
    op.create_table(
        "canonical_fact_resolution_snapshot_members",
        sa.Column("resolution_snapshot_id", sa.String(128), primary_key=True),
        sa.Column("member_ordinal", sa.Integer, primary_key=True),
        sa.Column(
            "canonical_metric_cell_id",
            sa.String(128),
            sa.ForeignKey("canonical_metric_cells.canonical_metric_cell_id"),
            nullable=False,
        ),
        sa.Column(
            "candidate_universe_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_candidate_universe_revisions.candidate_universe_id"),
            nullable=False,
        ),
        sa.Column(
            "relation_set_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_relation_set_revisions.relation_set_id"),
            nullable=False,
        ),
        sa.Column(
            "canonical_resolution_revision_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_resolution_revisions.canonical_resolution_revision_id"),
            nullable=False,
        ),
        sa.Column("member_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "resolution_snapshot_id",
            "canonical_metric_cell_id",
            name="uq_canonical_fact_snapshot_coordinate",
        ),
        sa.CheckConstraint(
            "member_ordinal >= 0 AND " + _hex("member_sha256"),
            name="ck_canonical_fact_snapshot_member",
        ),
    )
    for table in (
        "canonical_fact_candidate_universe_revisions",
        "canonical_fact_candidate_dispositions",
        "canonical_fact_candidate_universe_seals",
        "canonical_fact_relation_set_revisions",
        "canonical_fact_relation_assertions",
        "canonical_fact_relation_set_seals",
        "canonical_fact_resolution_revisions",
        "canonical_fact_resolution_snapshot_seals",
        "canonical_fact_resolution_snapshot_members",
    ):
        _append_only(table)
    op.execute(
        "CREATE TRIGGER trg_canonical_fact_universe_seal_exact BEFORE INSERT ON canonical_fact_candidate_universe_revisions WHEN NEW.member_set_sha256 <> fact_sha256(NEW.canonical_member_set_json) BEGIN SELECT RAISE(ABORT, 'canonical candidate universe commitment mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_fact_candidate_universe_sealed BEFORE INSERT ON canonical_fact_candidate_dispositions WHEN EXISTS (SELECT 1 FROM canonical_fact_candidate_universe_seals s WHERE s.candidate_universe_id=NEW.candidate_universe_id) BEGIN SELECT RAISE(ABORT, 'canonical candidate universe is sealed'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_fact_universe_final_exact BEFORE INSERT ON canonical_fact_candidate_universe_seals WHEN "
        "NOT EXISTS (SELECT 1 FROM canonical_fact_candidate_universe_revisions h "
        "WHERE h.candidate_universe_id=NEW.candidate_universe_id "
        "AND h.member_count=NEW.member_count "
        "AND h.canonical_member_set_json=NEW.canonical_member_set_json "
        "AND h.member_set_sha256=NEW.member_set_sha256 "
        "AND h.recorded_at=NEW.sealed_at) "
        "OR NEW.member_count<>(SELECT COUNT(*) FROM canonical_fact_candidate_dispositions d WHERE d.candidate_universe_id=NEW.candidate_universe_id) "
        "OR (NEW.member_count>0 AND ((SELECT MIN(candidate_ordinal) FROM canonical_fact_candidate_dispositions WHERE candidate_universe_id=NEW.candidate_universe_id)<>0 "
        "OR (SELECT MAX(candidate_ordinal) FROM canonical_fact_candidate_dispositions WHERE candidate_universe_id=NEW.candidate_universe_id)<>NEW.member_count-1)) "
        "OR NEW.canonical_member_set_json<>COALESCE((SELECT json_group_array(json(ordered.payload)) FROM ("
        "SELECT json_object("
        "'binding_commitment_sha256',d.binding_commitment_sha256,"
        "'binding_revision_id',d.binding_revision_id,"
        "'candidate_ordinal',d.candidate_ordinal,"
        "'eligibility',d.eligibility,"
        "'filing_disposition_id',d.filing_disposition_id,"
        "'mapping_commitment_sha256',d.mapping_commitment_sha256,"
        "'observation_id',d.observation_id,"
        "'observation_payload_sha256',d.observation_payload_sha256,"
        "'publication_id',d.source_publication_id,"
        "'publication_member_id',d.source_publication_member_id,"
        "'publication_seal_id',d.source_publication_seal_id,"
        "'reason_code',d.reason_code,"
        "'source_lane',d.source_lane,"
        "'source_publication_member_sha256',d.source_publication_member_sha256,"
        "'source_publication_record_commitment_sha256',d.source_record_commitment_sha256"
        ") payload FROM canonical_fact_candidate_dispositions d "
        "WHERE d.candidate_universe_id=NEW.candidate_universe_id "
        "ORDER BY d.candidate_ordinal) ordered),'[]') "
        "OR NEW.member_set_sha256<>fact_sha256(NEW.canonical_member_set_json) "
        "BEGIN SELECT RAISE(ABORT, 'canonical candidate universe final seal mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_fact_relation_set_exact BEFORE INSERT ON canonical_fact_relation_set_revisions WHEN NEW.relation_set_sha256 <> fact_sha256(NEW.canonical_relation_set_json) BEGIN SELECT RAISE(ABORT, 'canonical relation set commitment mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_fact_relation_set_sealed BEFORE INSERT ON canonical_fact_relation_assertions WHEN EXISTS (SELECT 1 FROM canonical_fact_relation_set_seals s WHERE s.relation_set_id=NEW.relation_set_id) BEGIN SELECT RAISE(ABORT, 'canonical relation set is sealed'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_fact_relation_final_exact BEFORE INSERT ON canonical_fact_relation_set_seals WHEN "
        "NOT EXISTS (SELECT 1 FROM canonical_fact_relation_set_revisions h "
        "WHERE h.relation_set_id=NEW.relation_set_id "
        "AND h.relation_count=NEW.relation_count "
        "AND h.canonical_relation_set_json=NEW.canonical_relation_set_json "
        "AND h.relation_set_sha256=NEW.relation_set_sha256 "
        "AND h.recorded_at=NEW.sealed_at) "
        "OR NEW.relation_count<>(SELECT COUNT(*) FROM canonical_fact_relation_assertions a WHERE a.relation_set_id=NEW.relation_set_id) "
        "OR (NEW.relation_count>0 AND ((SELECT MIN(relation_ordinal) FROM canonical_fact_relation_assertions WHERE relation_set_id=NEW.relation_set_id)<>0 "
        "OR (SELECT MAX(relation_ordinal) FROM canonical_fact_relation_assertions WHERE relation_set_id=NEW.relation_set_id)<>NEW.relation_count-1)) "
        "OR NEW.canonical_relation_set_json<>COALESCE((SELECT json_group_array(json(ordered.payload)) FROM ("
        "SELECT json_object('evidence',json(a.evidence_json),"
        "'object_observation_id',a.object_observation_id,"
        "'relation_kind',a.relation_kind,"
        "'subject_filing_disposition_id',a.subject_filing_disposition_id,"
        "'subject_observation_id',a.subject_observation_id) payload "
        "FROM canonical_fact_relation_assertions a "
        "WHERE a.relation_set_id=NEW.relation_set_id "
        "ORDER BY a.relation_ordinal) ordered),'[]') "
        "OR NEW.relation_set_sha256<>fact_sha256(NEW.canonical_relation_set_json) "
        "BEGIN SELECT RAISE(ABORT, 'canonical relation set final seal mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_fact_resolution_selected BEFORE INSERT ON canonical_fact_resolution_revisions WHEN NEW.selected_observation_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM canonical_fact_candidate_dispositions d WHERE d.candidate_universe_id=NEW.candidate_universe_id AND d.observation_id=NEW.selected_observation_id AND d.eligibility='eligible') BEGIN SELECT RAISE(ABORT, 'selected canonical observation is not eligible'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_fact_resolution_commitment BEFORE INSERT ON canonical_fact_resolution_revisions WHEN NEW.resolution_sha256 <> fact_sha256(NEW.canonical_resolution_json) OR (NEW.revision>1 AND NOT EXISTS (SELECT 1 FROM canonical_fact_resolution_revisions prior WHERE prior.canonical_resolution_revision_id=NEW.supersedes_resolution_revision_id AND prior.canonical_metric_cell_id=NEW.canonical_metric_cell_id AND prior.revision=NEW.revision-1)) BEGIN SELECT RAISE(ABORT, 'canonical resolution commitment or revision parent mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_fact_resolution_exact_graph BEFORE INSERT ON canonical_fact_resolution_revisions WHEN NOT EXISTS ("
        "SELECT 1 FROM canonical_fact_candidate_universe_revisions u "
        "JOIN canonical_fact_candidate_universe_seals us ON us.candidate_universe_id=u.candidate_universe_id "
        "JOIN canonical_fact_relation_set_revisions r ON r.candidate_universe_id=u.candidate_universe_id "
        "JOIN canonical_fact_relation_set_seals rs ON rs.relation_set_id=r.relation_set_id "
        "WHERE u.candidate_universe_id=NEW.candidate_universe_id "
        "AND u.canonical_metric_cell_id=NEW.canonical_metric_cell_id "
        "AND r.relation_set_id=NEW.relation_set_id "
        "AND us.candidate_universe_seal_id=NEW.candidate_universe_seal_id "
        "AND rs.relation_set_seal_id=NEW.relation_set_seal_id "
        "AND us.member_set_sha256=NEW.candidate_universe_sha256 "
        "AND rs.relation_set_sha256=NEW.relation_set_sha256) "
        "BEGIN SELECT RAISE(ABORT, 'canonical resolution requires its exact sealed candidate universe and relation set'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_fact_snapshot_exact BEFORE INSERT ON canonical_fact_resolution_snapshot_seals WHEN "
        "NEW.member_count<>(SELECT COUNT(*) FROM canonical_fact_resolution_snapshot_members m WHERE m.resolution_snapshot_id=NEW.resolution_snapshot_id) "
        "OR (NEW.member_count>0 AND ((SELECT MIN(member_ordinal) FROM canonical_fact_resolution_snapshot_members WHERE resolution_snapshot_id=NEW.resolution_snapshot_id)<>0 "
        "OR (SELECT MAX(member_ordinal) FROM canonical_fact_resolution_snapshot_members WHERE resolution_snapshot_id=NEW.resolution_snapshot_id)<>NEW.member_count-1)) "
        "OR NEW.canonical_member_set_json<>COALESCE((SELECT json_group_array(json(ordered.payload)) FROM ("
        "SELECT json_object('candidate_universe_id',m.candidate_universe_id,"
        "'canonical_metric_cell_id',m.canonical_metric_cell_id,"
        "'canonical_resolution_revision_id',m.canonical_resolution_revision_id,"
        "'relation_set_id',m.relation_set_id) payload "
        "FROM canonical_fact_resolution_snapshot_members m "
        "WHERE m.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "ORDER BY m.member_ordinal) ordered),'[]') "
        "OR NEW.member_set_sha256<>fact_sha256(NEW.canonical_member_set_json) "
        "OR EXISTS (SELECT 1 FROM canonical_fact_resolution_snapshot_members m "
        "JOIN canonical_fact_resolution_revisions r "
        "ON r.canonical_resolution_revision_id=m.canonical_resolution_revision_id "
        "WHERE m.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "AND (r.canonical_metric_cell_id<>m.canonical_metric_cell_id "
        "OR r.candidate_universe_id<>m.candidate_universe_id "
        "OR r.relation_set_id<>m.relation_set_id "
        "OR m.member_sha256<>fact_sha256(json_object("
        "'candidate_universe_id',m.candidate_universe_id,"
        "'canonical_metric_cell_id',m.canonical_metric_cell_id,"
        "'canonical_resolution_revision_id',m.canonical_resolution_revision_id,"
        "'relation_set_id',m.relation_set_id)) "
        "OR r.knowledge_at>NEW.cutoff_at OR r.recorded_at>NEW.cutoff_at "
        "OR EXISTS (SELECT 1 FROM canonical_fact_resolution_revisions newer "
        "WHERE newer.canonical_metric_cell_id=r.canonical_metric_cell_id "
        "AND newer.knowledge_at<=NEW.cutoff_at "
        "AND newer.recorded_at<=NEW.cutoff_at "
        "AND newer.revision>r.revision))) "
        "OR EXISTS (SELECT 1 FROM canonical_fact_resolution_revisions r "
        "WHERE r.knowledge_at<=NEW.cutoff_at AND r.recorded_at<=NEW.cutoff_at "
        "AND NOT EXISTS (SELECT 1 FROM canonical_fact_resolution_revisions newer "
        "WHERE newer.canonical_metric_cell_id=r.canonical_metric_cell_id "
        "AND newer.knowledge_at<=NEW.cutoff_at "
        "AND newer.recorded_at<=NEW.cutoff_at "
        "AND newer.revision>r.revision) "
        "AND NOT EXISTS (SELECT 1 FROM canonical_fact_resolution_snapshot_members m "
        "WHERE m.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "AND m.canonical_resolution_revision_id=r.canonical_resolution_revision_id)) "
        "BEGIN SELECT RAISE(ABORT, 'canonical snapshot commitment mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_fact_snapshot_members_sealed BEFORE INSERT ON canonical_fact_resolution_snapshot_members WHEN EXISTS (SELECT 1 FROM canonical_fact_resolution_snapshot_seals s WHERE s.resolution_snapshot_id=NEW.resolution_snapshot_id) BEGIN SELECT RAISE(ABORT, 'canonical resolution snapshot is sealed'); END"
    )
    op.create_index(
        "ix_canonical_fact_universe_coordinate_cutoff",
        "canonical_fact_candidate_universe_revisions",
        ["canonical_metric_cell_id", "cutoff_at"],
    )
    op.create_index(
        "ix_canonical_fact_resolution_as_known",
        "canonical_fact_resolution_revisions",
        ["canonical_metric_cell_id", "knowledge_at", "recorded_at"],
    )


def downgrade() -> None:
    for trigger in (
        "trg_canonical_fact_snapshot_members_sealed",
        "trg_canonical_fact_snapshot_exact",
        "trg_canonical_fact_resolution_selected",
        "trg_canonical_fact_resolution_commitment",
        "trg_canonical_fact_resolution_exact_graph",
        "trg_canonical_fact_relation_set_sealed",
        "trg_canonical_fact_relation_final_exact",
        "trg_canonical_fact_relation_set_exact",
        "trg_canonical_fact_candidate_universe_sealed",
        "trg_canonical_fact_universe_final_exact",
        "trg_canonical_fact_universe_seal_exact",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in (
        "canonical_fact_resolution_snapshot_members",
        "canonical_fact_resolution_snapshot_seals",
        "canonical_fact_resolution_revisions",
        "canonical_fact_relation_set_seals",
        "canonical_fact_relation_assertions",
        "canonical_fact_relation_set_revisions",
        "canonical_fact_candidate_universe_seals",
        "canonical_fact_candidate_dispositions",
        "canonical_fact_candidate_universe_revisions",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_update_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_delete_append_only")
        op.drop_table(table)
