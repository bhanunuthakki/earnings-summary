"""Require issuer-scoped canonical resolution and projection snapshots.

Revision ID: 0255_scoped_canonical_resolution_snapshots
Revises: 0254_filing_xbrl_processor_closure
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0255_scoped_canonical_resolution_snapshots"
down_revision: str | None = "0254_filing_xbrl_processor_closure"
branch_labels: str | None = None
depends_on: str | None = None

_SCOPE_VERSION = "canonical-resolution-snapshot-scope.v1"
_DEPENDENT_TABLES = (
    "canonical_fact_resolution_snapshot_seals",
    "canonical_fact_resolution_snapshot_watermarks",
    "canonical_fact_projection_generations",
    "research_snapshot_headers",
    "research_snapshot_admission_receipts",
    "heterogeneous_retrieval_trace_headers",
    "ask_retrieval_scope_promotions",
)
_NEW_TABLES = (
    "canonical_fact_resolution_snapshot_scope_headers",
    "canonical_fact_resolution_snapshot_scope_members",
    "canonical_fact_resolution_snapshot_scope_seals",
    "canonical_fact_projection_scope_bindings",
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


def _nonempty_tables(tables: tuple[str, ...]) -> list[str]:
    bind = op.get_bind()
    present = set(sa.inspect(bind).get_table_names())
    return [
        table
        for table in tables
        if table in present
        and int(bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()) > 0
    ]


def upgrade() -> None:
    nonempty = _nonempty_tables(_DEPENDENT_TABLES)
    if nonempty:
        raise RuntimeError(
            "0255 refuses to infer issuer scope for existing canonical snapshots "
            "or downstream artifacts; export and rebuild: " + ", ".join(nonempty)
        )

    op.create_table(
        "canonical_fact_resolution_snapshot_scope_headers",
        sa.Column("resolution_snapshot_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "issuer_id",
            sa.String(128),
            sa.ForeignKey("issuer_entities.issuer_id"),
            nullable=False,
        ),
        sa.Column("scope_version", sa.String(64), nullable=False),
        sa.Column("canonical_scope_json", sa.Text(), nullable=False),
        sa.Column("scope_sha256", sa.String(64), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            f"scope_version='{_SCOPE_VERSION}' "
            "AND json_valid(canonical_scope_json) "
            "AND json_type(canonical_scope_json)='object' "
            f"AND {_hex('scope_sha256')}",
            name="ck_canonical_resolution_snapshot_scope_header",
        ),
        sa.CheckConstraint(
            "recorded_at>=cutoff_at",
            name="ck_canonical_resolution_snapshot_scope_header_clocks",
        ),
    )
    op.create_table(
        "canonical_fact_resolution_snapshot_scope_members",
        sa.Column(
            "resolution_snapshot_id",
            sa.String(128),
            sa.ForeignKey(
                "canonical_fact_resolution_snapshot_scope_headers.resolution_snapshot_id"
            ),
            primary_key=True,
        ),
        sa.Column("member_ordinal", sa.Integer(), primary_key=True),
        sa.Column(
            "reporting_entity_id",
            sa.String(128),
            sa.ForeignKey("reporting_entities.reporting_entity_id"),
            nullable=False,
        ),
        sa.Column("member_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "resolution_snapshot_id",
            "reporting_entity_id",
            name="uq_canonical_resolution_snapshot_scope_entity",
        ),
        sa.CheckConstraint(
            f"member_ordinal>=0 AND {_hex('member_sha256')}",
            name="ck_canonical_resolution_snapshot_scope_member",
        ),
    )
    op.create_table(
        "canonical_fact_resolution_snapshot_scope_seals",
        sa.Column(
            "resolution_snapshot_id",
            sa.String(128),
            sa.ForeignKey(
                "canonical_fact_resolution_snapshot_scope_headers.resolution_snapshot_id"
            ),
            primary_key=True,
        ),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("canonical_member_set_json", sa.Text(), nullable=False),
        sa.Column("member_set_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_snapshot_commitment_json", sa.Text(), nullable=False),
        sa.Column("snapshot_commitment_sha256", sa.String(64), nullable=False),
        sa.Column("sealed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "member_count>0 AND json_valid(canonical_member_set_json) "
            "AND json_type(canonical_member_set_json)='array' "
            "AND json_valid(canonical_snapshot_commitment_json) "
            "AND json_type(canonical_snapshot_commitment_json)='object' "
            f"AND {_hex('member_set_sha256')} "
            f"AND {_hex('snapshot_commitment_sha256')}",
            name="ck_canonical_resolution_snapshot_scope_seal",
        ),
    )
    op.create_table(
        "canonical_fact_projection_scope_bindings",
        sa.Column("generation_id", sa.String(128), primary_key=True),
        sa.Column(
            "resolution_snapshot_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_resolution_snapshot_scope_seals.resolution_snapshot_id"),
            nullable=False,
        ),
        sa.Column("resolution_scope_sha256", sa.String(64), nullable=False),
        sa.Column(
            "resolution_snapshot_commitment_sha256",
            sa.String(64),
            nullable=False,
        ),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            f"{_hex('resolution_scope_sha256')} "
            f"AND {_hex('resolution_snapshot_commitment_sha256')}",
            name="ck_canonical_fact_projection_scope_binding",
        ),
    )

    op.execute("DROP TRIGGER trg_canonical_fact_snapshot_exact")
    op.execute(
        "CREATE TRIGGER trg_canonical_resolution_scope_header_exact "
        "BEFORE INSERT ON canonical_fact_resolution_snapshot_scope_headers WHEN "
        "NEW.scope_sha256<>fact_sha256(NEW.canonical_scope_json) "
        "OR NEW.canonical_scope_json<>json_object("
        "'issuer_id',NEW.issuer_id,"
        "'reporting_entity_ids',json_extract(NEW.canonical_scope_json,"
        "'$.reporting_entity_ids'),"
        "'scope_version',NEW.scope_version) "
        "OR json_array_length(json_extract(NEW.canonical_scope_json,"
        "'$.reporting_entity_ids'))<1 "
        "BEGIN SELECT RAISE(ABORT, 'canonical resolution scope header mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_resolution_scope_member_exact "
        "BEFORE INSERT ON canonical_fact_resolution_snapshot_scope_members WHEN "
        "NEW.member_sha256<>fact_sha256(json_object("
        "'reporting_entity_id',NEW.reporting_entity_id)) "
        "OR NOT EXISTS (SELECT 1 "
        "FROM canonical_fact_resolution_snapshot_scope_headers header "
        "JOIN reporting_entities entity "
        "ON entity.reporting_entity_id=NEW.reporting_entity_id "
        "WHERE header.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "AND entity.issuer_id=header.issuer_id "
        "AND json_extract(header.canonical_scope_json,"
        "'$.reporting_entity_ids['||NEW.member_ordinal||']')="
        "NEW.reporting_entity_id) "
        "BEGIN SELECT RAISE(ABORT, 'canonical resolution scope member mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_fact_snapshot_exact "
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
        "OR resolution.recorded_at>NEW.cutoff_at "
        "OR NOT EXISTS (SELECT 1 "
        "FROM canonical_fact_resolution_snapshot_scope_members scope_member "
        "WHERE scope_member.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "AND scope_member.reporting_entity_id=cell.reporting_entity_id) "
        "OR EXISTS (SELECT 1 FROM canonical_fact_resolution_revisions newer "
        "WHERE newer.canonical_metric_cell_id=resolution.canonical_metric_cell_id "
        "AND newer.knowledge_at<=NEW.cutoff_at "
        "AND newer.recorded_at<=NEW.cutoff_at "
        "AND newer.revision>resolution.revision))) "
        "OR EXISTS (SELECT 1 FROM canonical_fact_resolution_revisions resolution "
        "JOIN canonical_metric_cells cell "
        "ON cell.canonical_metric_cell_id=resolution.canonical_metric_cell_id "
        "JOIN canonical_fact_resolution_snapshot_scope_members scope_member "
        "ON scope_member.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "AND scope_member.reporting_entity_id=cell.reporting_entity_id "
        "WHERE resolution.knowledge_at<=NEW.cutoff_at "
        "AND resolution.recorded_at<=NEW.cutoff_at "
        "AND NOT EXISTS (SELECT 1 FROM canonical_fact_resolution_revisions newer "
        "WHERE newer.canonical_metric_cell_id=resolution.canonical_metric_cell_id "
        "AND newer.knowledge_at<=NEW.cutoff_at "
        "AND newer.recorded_at<=NEW.cutoff_at "
        "AND newer.revision>resolution.revision) "
        "AND NOT EXISTS (SELECT 1 "
        "FROM canonical_fact_resolution_snapshot_members member "
        "WHERE member.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "AND member.canonical_resolution_revision_id="
        "resolution.canonical_resolution_revision_id)) "
        "BEGIN SELECT RAISE(ABORT, 'canonical scoped snapshot commitment mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_resolution_scope_seal_exact "
        "BEFORE INSERT ON canonical_fact_resolution_snapshot_scope_seals WHEN "
        "NEW.member_count<>(SELECT COUNT(*) "
        "FROM canonical_fact_resolution_snapshot_scope_members member "
        "WHERE member.resolution_snapshot_id=NEW.resolution_snapshot_id) "
        "OR (SELECT MIN(member_ordinal) "
        "FROM canonical_fact_resolution_snapshot_scope_members "
        "WHERE resolution_snapshot_id=NEW.resolution_snapshot_id)<>0 "
        "OR (SELECT MAX(member_ordinal) "
        "FROM canonical_fact_resolution_snapshot_scope_members "
        "WHERE resolution_snapshot_id=NEW.resolution_snapshot_id)<>NEW.member_count-1 "
        "OR NEW.canonical_member_set_json<>(SELECT json_group_array(json(ordered.payload)) "
        "FROM (SELECT json_object("
        "'reporting_entity_id',member.reporting_entity_id) payload "
        "FROM canonical_fact_resolution_snapshot_scope_members member "
        "WHERE member.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "ORDER BY member.member_ordinal) ordered) "
        "OR NEW.member_set_sha256<>fact_sha256(NEW.canonical_member_set_json) "
        "OR NEW.canonical_snapshot_commitment_json<>json_object("
        "'cutoff_at',(SELECT cutoff_at "
        "FROM canonical_fact_resolution_snapshot_scope_headers "
        "WHERE resolution_snapshot_id=NEW.resolution_snapshot_id),"
        "'member_set_sha256',(SELECT member_set_sha256 "
        "FROM canonical_fact_resolution_snapshot_seals "
        "WHERE resolution_snapshot_id=NEW.resolution_snapshot_id),"
        "'resolution_snapshot_id',NEW.resolution_snapshot_id,"
        "'scope_member_set_sha256',NEW.member_set_sha256,"
        "'scope_sha256',(SELECT scope_sha256 "
        "FROM canonical_fact_resolution_snapshot_scope_headers "
        "WHERE resolution_snapshot_id=NEW.resolution_snapshot_id)) "
        "OR NEW.snapshot_commitment_sha256<>"
        "fact_sha256(NEW.canonical_snapshot_commitment_json) "
        "OR NOT EXISTS (SELECT 1 "
        "FROM canonical_fact_resolution_snapshot_seals resolution "
        "JOIN canonical_fact_resolution_snapshot_scope_headers scope "
        "ON scope.resolution_snapshot_id=resolution.resolution_snapshot_id "
        "WHERE resolution.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "AND NEW.sealed_at>=resolution.recorded_at "
        "AND NEW.sealed_at>=scope.recorded_at) "
        "BEGIN SELECT RAISE(ABORT, 'canonical resolution scope seal mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_resolution_scope_members_sealed "
        "BEFORE INSERT ON canonical_fact_resolution_snapshot_scope_members WHEN "
        "EXISTS (SELECT 1 FROM canonical_fact_resolution_snapshot_scope_seals seal "
        "WHERE seal.resolution_snapshot_id=NEW.resolution_snapshot_id) "
        "BEGIN SELECT RAISE(ABORT, 'canonical resolution scope is sealed'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_projection_scope_binding_exact "
        "BEFORE INSERT ON canonical_fact_projection_scope_bindings WHEN "
        "NOT EXISTS (SELECT 1 "
        "FROM canonical_fact_resolution_snapshot_scope_headers header "
        "JOIN canonical_fact_resolution_snapshot_scope_seals seal "
        "ON seal.resolution_snapshot_id=header.resolution_snapshot_id "
        "WHERE header.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "AND header.scope_sha256=NEW.resolution_scope_sha256 "
        "AND seal.snapshot_commitment_sha256="
        "NEW.resolution_snapshot_commitment_sha256) "
        "BEGIN SELECT RAISE(ABORT, 'canonical projection scope binding mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_projection_generation_scope "
        "BEFORE INSERT ON canonical_fact_projection_generations WHEN "
        "NOT EXISTS (SELECT 1 FROM canonical_fact_projection_scope_bindings binding "
        "WHERE binding.generation_id=NEW.generation_id "
        "AND binding.resolution_snapshot_id=NEW.resolution_snapshot_id) "
        "OR (NEW.parent_generation_id IS NOT NULL AND NOT EXISTS (SELECT 1 "
        "FROM canonical_fact_projection_scope_bindings child "
        "JOIN canonical_fact_projection_scope_bindings parent "
        "ON parent.generation_id=NEW.parent_generation_id "
        "WHERE child.generation_id=NEW.generation_id "
        "AND child.resolution_scope_sha256=parent.resolution_scope_sha256)) "
        "BEGIN SELECT RAISE(ABORT, 'canonical projection generation scope mismatch'); END"
    )

    for table in _NEW_TABLES:
        _append_only(table)


def downgrade() -> None:
    nonempty = _nonempty_tables(_NEW_TABLES)
    if nonempty:
        raise RuntimeError(
            "0255 refuses to discard committed scoped snapshot state: " + ", ".join(nonempty)
        )
    for trigger in (
        "trg_canonical_fact_projection_scope_bindings_delete_append_only",
        "trg_canonical_fact_projection_scope_bindings_update_append_only",
        "trg_canonical_fact_resolution_snapshot_scope_seals_delete_append_only",
        "trg_canonical_fact_resolution_snapshot_scope_seals_update_append_only",
        "trg_canonical_fact_resolution_snapshot_scope_members_delete_append_only",
        "trg_canonical_fact_resolution_snapshot_scope_members_update_append_only",
        "trg_canonical_fact_resolution_snapshot_scope_headers_delete_append_only",
        "trg_canonical_fact_resolution_snapshot_scope_headers_update_append_only",
        "trg_canonical_projection_generation_scope",
        "trg_canonical_projection_scope_binding_exact",
        "trg_canonical_resolution_scope_members_sealed",
        "trg_canonical_resolution_scope_seal_exact",
        "trg_canonical_resolution_scope_member_exact",
        "trg_canonical_resolution_scope_header_exact",
    ):
        op.execute(f"DROP TRIGGER {trigger}")
    op.execute("DROP TRIGGER trg_canonical_fact_snapshot_exact")
    op.execute(
        "CREATE TRIGGER trg_canonical_fact_snapshot_exact BEFORE INSERT ON "
        "canonical_fact_resolution_snapshot_seals WHEN "
        "NEW.member_count<>(SELECT COUNT(*) FROM "
        "canonical_fact_resolution_snapshot_members m "
        "WHERE m.resolution_snapshot_id=NEW.resolution_snapshot_id) "
        "OR (NEW.member_count>0 AND ((SELECT MIN(member_ordinal) FROM "
        "canonical_fact_resolution_snapshot_members WHERE "
        "resolution_snapshot_id=NEW.resolution_snapshot_id)<>0 "
        "OR (SELECT MAX(member_ordinal) FROM "
        "canonical_fact_resolution_snapshot_members WHERE "
        "resolution_snapshot_id=NEW.resolution_snapshot_id)<>NEW.member_count-1)) "
        "OR NEW.canonical_member_set_json<>COALESCE((SELECT "
        "json_group_array(json(ordered.payload)) FROM (SELECT json_object("
        "'candidate_universe_id',m.candidate_universe_id,"
        "'canonical_metric_cell_id',m.canonical_metric_cell_id,"
        "'canonical_resolution_revision_id',m.canonical_resolution_revision_id,"
        "'relation_set_id',m.relation_set_id) payload FROM "
        "canonical_fact_resolution_snapshot_members m WHERE "
        "m.resolution_snapshot_id=NEW.resolution_snapshot_id "
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
    for table in reversed(_NEW_TABLES):
        op.drop_table(table)
