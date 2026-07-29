"""Seal exact research universes and bind expected documents to source duties.

Revision ID: 0252_research_universe_closure
Revises: 0249_embedding_runtime_artifact_binding
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0252_research_universe_closure"
# Provisional until the architecture branch containing 0250/0251 is rebased.
down_revision: str | Sequence[str] | None = "0249_embedding_runtime_artifact_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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
        "expected_documents",
        "source_obligation_revisions",
        "research_snapshot_headers",
        "research_snapshot_seals",
        "issuer_entities",
        "reporting_entities",
        "search_embedding_model_promotions",
    }
    missing = sorted(required - set(sa.inspect(bind).get_table_names()))
    if missing:
        raise RuntimeError(
            "research-universe closure requires the investor-grade evidence chain: "
            + ", ".join(missing)
        )
    existing_research_snapshots = int(
        bind.execute(
            sa.text(
                "SELECT (SELECT COUNT(*) FROM research_snapshot_headers) "
                "+ (SELECT COUNT(*) FROM research_snapshot_seals)"
            )
        ).scalar()
        or 0
    )
    if existing_research_snapshots:
        raise RuntimeError(
            "0252 cannot infer an exact universe for existing Research Snapshot "
            "history; export and rebuild it under the explicit universe contract"
        )

    op.add_column(
        "search_embedding_model_promotions",
        sa.Column("knowledge_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "search_embedding_model_promotions",
        sa.Column("recorded_at", sa.DateTime(), nullable=True),
    )
    # Existing promotions predate the bitemporal contract. Their owner approval
    # is the earliest defensible knowledge/recorded clock, so preserve it
    # exactly rather than inventing migration-time provenance.
    op.execute("DROP TRIGGER IF EXISTS trg_search_embedding_model_promotions_append_only")
    op.execute(
        "UPDATE search_embedding_model_promotions "
        "SET knowledge_at=approved_at,recorded_at=approved_at "
        "WHERE knowledge_at IS NULL OR recorded_at IS NULL"
    )
    op.execute(
        "CREATE TRIGGER trg_search_embedding_model_promotions_append_only "
        "BEFORE UPDATE ON search_embedding_model_promotions "
        "BEGIN SELECT RAISE(ABORT, 'embedding promotions are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_embedding_model_promotions_bitemporal "
        "BEFORE INSERT ON search_embedding_model_promotions WHEN "
        "NEW.knowledge_at IS NULL OR NEW.recorded_at IS NULL "
        "OR datetime(NEW.approved_at)>datetime(NEW.knowledge_at) "
        "OR datetime(NEW.knowledge_at)>datetime(NEW.recorded_at) "
        "BEGIN SELECT RAISE(ABORT, "
        "'embedding promotion clocks are invalid'); END"
    )

    op.create_table(
        "expected_document_obligation_bindings",
        sa.Column("binding_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "expected_document_id",
            sa.String(128),
            sa.ForeignKey("expected_documents.expected_document_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "source_obligation_revision_id",
            sa.String(128),
            sa.ForeignKey("source_obligation_revisions.obligation_revision_id"),
            nullable=False,
        ),
        sa.Column(
            "issuer_id",
            sa.String(128),
            sa.ForeignKey("issuer_entities.issuer_id"),
            nullable=False,
        ),
        sa.Column(
            "reporting_entity_id",
            sa.String(128),
            sa.ForeignKey("reporting_entities.reporting_entity_id"),
            nullable=True,
        ),
        sa.Column("document_family", sa.String(64), nullable=False),
        sa.Column("canonical_binding_json", sa.Text(), nullable=False),
        sa.Column("binding_sha256", sa.String(64), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "json_valid(canonical_binding_json) "
            "AND json_type(canonical_binding_json)='object'",
            name="ck_expected_document_obligation_binding_json",
        ),
        sa.CheckConstraint(
            "length(binding_sha256)=64 "
            "AND binding_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_expected_document_obligation_binding_hash",
        ),
        sa.CheckConstraint(
            "effective_at<=knowledge_at AND knowledge_at<=recorded_at",
            name="ck_expected_document_obligation_binding_clocks",
        ),
    )
    op.create_index(
        "ix_expected_document_obligation_scope",
        "expected_document_obligation_bindings",
        ["issuer_id", "reporting_entity_id", "document_family"],
    )
    op.execute(
        "CREATE TRIGGER trg_expected_document_obligation_binding_scope "
        "BEFORE INSERT ON expected_document_obligation_bindings WHEN NOT EXISTS ("
        "SELECT 1 FROM expected_documents AS expected "
        "JOIN source_obligation_revisions AS obligation "
        "ON obligation.obligation_revision_id=NEW.source_obligation_revision_id "
        "WHERE expected.expected_document_id=NEW.expected_document_id "
        "AND expected.issuer_id=NEW.issuer_id "
        "AND obligation.issuer_id=NEW.issuer_id "
        "AND obligation.reporting_entity_id IS NEW.reporting_entity_id "
        "AND obligation.document_family=NEW.document_family "
        "AND obligation.obligation_state IN ('required','optional') "
        "AND datetime(obligation.active_from)<=datetime(NEW.effective_at) "
        "AND (obligation.active_to IS NULL "
        "OR datetime(obligation.active_to)>datetime(NEW.effective_at)) "
        "AND datetime(obligation.knowledge_at)<=datetime(NEW.knowledge_at) "
        "AND datetime(obligation.recorded_at)<=datetime(NEW.recorded_at)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'expected document obligation binding scope mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_expected_document_obligation_binding_exact "
        "BEFORE INSERT ON expected_document_obligation_bindings WHEN "
        "NEW.canonical_binding_json<>json_object("
        "'document_family',NEW.document_family,"
        "'expected_document_id',NEW.expected_document_id,"
        "'issuer_id',NEW.issuer_id,"
        "'reporting_entity_id',NEW.reporting_entity_id,"
        "'source_obligation_revision_id',NEW.source_obligation_revision_id) "
        "OR NEW.binding_sha256<>fact_sha256(NEW.canonical_binding_json) "
        "BEGIN SELECT RAISE(ABORT, "
        "'expected document obligation commitment mismatch'); END"
    )
    _append_only(
        "expected_document_obligation_bindings",
        "expected-document obligation binding",
    )

    op.create_table(
        "research_snapshot_universe_commitments",
        sa.Column(
            "research_snapshot_id",
            sa.String(128),
            sa.ForeignKey("research_snapshot_headers.research_snapshot_id"),
            primary_key=True,
        ),
        sa.Column(
            "issuer_id",
            sa.String(128),
            sa.ForeignKey("issuer_entities.issuer_id"),
            nullable=False,
        ),
        sa.Column("reporting_entity_ids_json", sa.Text(), nullable=False),
        sa.Column("document_version_ids_json", sa.Text(), nullable=False),
        sa.Column("source_obligation_revision_ids_json", sa.Text(), nullable=False),
        sa.Column("canonical_universe_json", sa.Text(), nullable=False),
        sa.Column("universe_sha256", sa.String(64), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "json_valid(reporting_entity_ids_json) "
            "AND json_type(reporting_entity_ids_json)='array' "
            "AND json_array_length(reporting_entity_ids_json)>0 "
            "AND json_valid(document_version_ids_json) "
            "AND json_type(document_version_ids_json)='array' "
            "AND json_array_length(document_version_ids_json)>0 "
            "AND json_valid(source_obligation_revision_ids_json) "
            "AND json_type(source_obligation_revision_ids_json)='array' "
            "AND json_array_length(source_obligation_revision_ids_json)>0 "
            "AND json_valid(canonical_universe_json) "
            "AND json_type(canonical_universe_json)='object'",
            name="ck_research_snapshot_universe_json",
        ),
        sa.CheckConstraint(
            "length(universe_sha256)=64 "
            "AND universe_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_research_snapshot_universe_hash",
        ),
        sa.CheckConstraint(
            "recorded_at>=cutoff_at",
            name="ck_research_snapshot_universe_clocks",
        ),
    )
    op.execute(
        "CREATE TRIGGER trg_research_snapshot_universe_exact "
        "BEFORE INSERT ON research_snapshot_universe_commitments WHEN "
        "NOT EXISTS (SELECT 1 FROM research_snapshot_headers AS header "
        "WHERE header.research_snapshot_id=NEW.research_snapshot_id "
        "AND header.cutoff_at=NEW.cutoff_at "
        "AND header.recorded_at=NEW.recorded_at "
        "AND json_extract(header.request_json,"
        "'$.research_universe.issuer_id')=NEW.issuer_id "
        "AND json_extract(header.request_json,"
        "'$.research_universe.reporting_entity_ids')="
        "NEW.reporting_entity_ids_json "
        "AND json_extract(header.request_json,"
        "'$.research_universe.document_version_ids')="
        "NEW.document_version_ids_json "
        "AND json_extract(header.request_json,"
        "'$.research_universe.source_obligation_revision_ids')="
        "NEW.source_obligation_revision_ids_json) "
        "OR NEW.canonical_universe_json<>json_object("
        "'document_version_ids',json(NEW.document_version_ids_json),"
        "'issuer_id',NEW.issuer_id,"
        "'reporting_entity_ids',json(NEW.reporting_entity_ids_json),"
        "'source_obligation_revision_ids',"
        "json(NEW.source_obligation_revision_ids_json)) "
        "OR NEW.universe_sha256<>fact_sha256(NEW.canonical_universe_json) "
        "OR (SELECT COUNT(*) FROM json_each(NEW.reporting_entity_ids_json)) "
        "<>(SELECT COUNT(DISTINCT value) "
        "FROM json_each(NEW.reporting_entity_ids_json)) "
        "OR (SELECT COUNT(*) FROM json_each(NEW.document_version_ids_json)) "
        "<>(SELECT COUNT(DISTINCT value) "
        "FROM json_each(NEW.document_version_ids_json)) "
        "OR (SELECT COUNT(*) "
        "FROM json_each(NEW.source_obligation_revision_ids_json)) "
        "<>(SELECT COUNT(DISTINCT value) "
        "FROM json_each(NEW.source_obligation_revision_ids_json)) "
        "OR EXISTS (SELECT 1 FROM json_each(NEW.reporting_entity_ids_json) item "
        "LEFT JOIN reporting_entities entity "
        "ON entity.reporting_entity_id=item.value "
        "WHERE item.type<>'text' OR length(item.value)=0 "
        "OR entity.reporting_entity_id IS NULL "
        "OR entity.issuer_id<>NEW.issuer_id) "
        "OR EXISTS (SELECT 1 FROM json_each(NEW.document_version_ids_json) item "
        "LEFT JOIN v_evidence_document_versions_canonical document "
        "ON document.document_version_id=item.value "
        "WHERE item.type<>'text' OR length(item.value)=0 "
        "OR document.document_version_id IS NULL "
        "OR document.issuer_id<>NEW.issuer_id "
        "OR document.reporting_entity_id IS NULL "
        "OR NOT EXISTS (SELECT 1 "
        "FROM json_each(NEW.reporting_entity_ids_json) entity_id "
        "WHERE entity_id.value=document.reporting_entity_id)) "
        "OR EXISTS (SELECT 1 "
        "FROM json_each(NEW.source_obligation_revision_ids_json) item "
        "LEFT JOIN source_obligation_revisions obligation "
        "ON obligation.obligation_revision_id=item.value "
        "WHERE item.type<>'text' OR length(item.value)=0 "
        "OR obligation.obligation_revision_id IS NULL "
        "OR obligation.issuer_id<>NEW.issuer_id "
        "OR obligation.reporting_entity_id IS NULL "
        "OR NOT EXISTS (SELECT 1 "
        "FROM json_each(NEW.reporting_entity_ids_json) entity_id "
        "WHERE entity_id.value=obligation.reporting_entity_id)) "
        "BEGIN SELECT RAISE(ABORT, 'research universe commitment mismatch'); END"
    )
    _append_only(
        "research_snapshot_universe_commitments",
        "research snapshot universe",
    )
    op.execute(
        "CREATE TRIGGER trg_research_snapshot_seal_requires_universe "
        "BEFORE INSERT ON research_snapshot_seals WHEN NOT EXISTS ("
        "SELECT 1 FROM research_snapshot_universe_commitments AS universe "
        "JOIN research_snapshot_members AS member "
        "ON member.research_snapshot_id=universe.research_snapshot_id "
        "WHERE universe.research_snapshot_id=NEW.research_snapshot_id "
        "AND member.requested_lane='research_universe' "
        "AND member.reference_table='research_snapshot_universe_commitments' "
        "AND member.reference_id=universe.research_snapshot_id "
        "AND member.reference_commitment_sha256=universe.universe_sha256 "
        "AND datetime(member.reference_knowledge_at)=datetime(universe.cutoff_at) "
        "AND datetime(member.reference_recorded_at)=datetime(universe.recorded_at) "
        "GROUP BY universe.research_snapshot_id HAVING COUNT(*)=1) "
        "BEGIN SELECT RAISE(ABORT, "
        "'Research Snapshot seal requires exact universe commitment'); END"
    )


def downgrade() -> None:
    bind = op.get_bind()
    universe_count = int(
        bind.execute(
            sa.text("SELECT COUNT(*) FROM research_snapshot_universe_commitments")
        ).scalar()
        or 0
    )
    binding_count = int(
        bind.execute(
            sa.text("SELECT COUNT(*) FROM expected_document_obligation_bindings")
        ).scalar()
        or 0
    )
    if universe_count or binding_count:
        raise RuntimeError(
            "0252 downgrade refused: explicit research universes or "
            "expected-document obligation bindings would become unverifiable"
        )
    op.execute("DROP TRIGGER IF EXISTS trg_research_snapshot_seal_requires_universe")
    for table in (
        "research_snapshot_universe_commitments",
        "expected_document_obligation_bindings",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_research_snapshot_universe_exact")
    op.drop_table("research_snapshot_universe_commitments")
    op.execute("DROP TRIGGER IF EXISTS trg_expected_document_obligation_binding_exact")
    op.execute("DROP TRIGGER IF EXISTS trg_expected_document_obligation_binding_scope")
    op.drop_index(
        "ix_expected_document_obligation_scope",
        table_name="expected_document_obligation_bindings",
    )
    op.drop_table("expected_document_obligation_bindings")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_search_embedding_model_promotions_bitemporal"
    )
    # SQLite reparses every trigger in the database during DROP COLUMN. Legacy
    # fixture schemas can intentionally omit columns referenced by unrelated
    # later triggers, so preserve/drop/recreate the surviving trigger set around
    # this exact reversal (the same compatibility pattern used by 0249).
    trigger_rows = bind.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND sql IS NOT NULL ORDER BY name"
        )
    ).fetchall()
    triggers = [(str(row[0]), str(row[1])) for row in trigger_rows]
    for name, _sql in triggers:
        escaped = name.replace('"', '""')
        op.execute(f'DROP TRIGGER "{escaped}"')
    try:
        op.drop_column("search_embedding_model_promotions", "recorded_at")
        op.drop_column("search_embedding_model_promotions", "knowledge_at")
    finally:
        for _name, sql in triggers:
            op.execute(sql)
