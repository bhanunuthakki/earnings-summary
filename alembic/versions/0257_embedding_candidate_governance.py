"""Govern inert embedding candidates and evidence-bound promotion receipts.

Revision ID: 0257_embedding_candidate_governance
Revises: 0256_population_cutover_receipts
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0257_embedding_candidate_governance"
down_revision: str | None = "0256_population_cutover_receipts"
branch_labels: str | None = None
depends_on: str | None = None

_PROMOTION_COLUMNS = (
    "evaluation_receipt_id",
    "evaluation_artifact_json",
    "runtime_registration_id",
    "approval_receipt_json",
    "approval_receipt_sha256",
)


def _append_only(table: str) -> None:
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_{event.lower()}_append_only "
            f"BEFORE {event} ON {table} BEGIN SELECT RAISE(ABORT, "
            f"'{table} is append-only'); END"
        )


def _candidate_runtime_triggers() -> None:
    op.execute(
        "CREATE TRIGGER trg_embedding_artifacts_runtime_binding "
        "BEFORE INSERT ON search_embedding_artifacts "
        "WHEN NEW.outcome='succeeded' AND ("
        "length(NEW.runtime_artifact_sha256)<>64 "
        "OR NEW.runtime_artifact_sha256 GLOB '*[^0-9a-f]*' "
        "OR NOT EXISTS (SELECT 1 FROM search_embedding_runtime_registrations runtime "
        "WHERE runtime.purpose='evidence_vector_retrieval' "
        "AND runtime.provider=NEW.provider AND runtime.model=NEW.model "
        "AND runtime.dimensions=NEW.dimensions "
        "AND runtime.runtime_artifact_sha256=NEW.runtime_artifact_sha256)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'successful embedding requires registered runtime artifact'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_vector_seals_runtime_binding "
        "BEFORE INSERT ON search_projection_seals "
        "WHEN NEW.index_kind='vector' AND ("
        "length(NEW.runtime_artifact_sha256)<>64 "
        "OR NEW.runtime_artifact_sha256 GLOB '*[^0-9a-f]*' "
        "OR NOT EXISTS (SELECT 1 FROM search_embedding_runtime_registrations runtime "
        "WHERE runtime.purpose='evidence_vector_retrieval' "
        "AND runtime.provider=NEW.provider AND runtime.model=NEW.model "
        "AND runtime.dimensions=NEW.dimensions "
        "AND runtime.runtime_artifact_sha256=NEW.runtime_artifact_sha256) "
        "OR NOT EXISTS (SELECT 1 FROM search_embedding_artifacts artifact "
        "WHERE artifact.index_run_id=NEW.index_run_id "
        "AND artifact.outcome='succeeded') "
        "OR EXISTS (SELECT 1 FROM search_embedding_artifacts artifact "
        "WHERE artifact.index_run_id=NEW.index_run_id "
        "AND artifact.outcome='succeeded' "
        "AND (artifact.runtime_artifact_sha256 IS NULL "
        "OR artifact.runtime_artifact_sha256<>NEW.runtime_artifact_sha256))) "
        "BEGIN SELECT RAISE(ABORT, "
        "'vector seal requires one registered runtime artifact'); END"
    )


def upgrade() -> None:
    op.create_table(
        "search_embedding_runtime_registrations",
        sa.Column("runtime_registration_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("runtime_artifact_json", sa.Text(), nullable=False),
        sa.Column("runtime_artifact_sha256", sa.String(64), nullable=False),
        sa.Column("registered_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "purpose",
            "runtime_artifact_sha256",
            name="uq_embedding_runtime_registration_artifact",
        ),
        sa.CheckConstraint(
            "dimensions>0 AND json_valid(runtime_artifact_json)=1 "
            "AND length(runtime_artifact_sha256)=64 "
            "AND runtime_artifact_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_embedding_runtime_registration_contract",
        ),
    )
    op.create_table(
        "search_embedding_evaluation_receipts",
        sa.Column("evaluation_receipt_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("golden_sha256", sa.String(64), nullable=False),
        sa.Column("evaluation_artifact_json", sa.Text(), nullable=False),
        sa.Column("evaluation_artifact_sha256", sa.String(64), nullable=False),
        sa.Column("candidate_set_json", sa.Text(), nullable=False),
        sa.Column("candidate_set_sha256", sa.String(64), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "json_valid(evaluation_artifact_json)=1 "
            "AND json_valid(candidate_set_json)=1 "
            "AND json_type(candidate_set_json)='array' "
            "AND length(golden_sha256)=64 "
            "AND length(evaluation_artifact_sha256)=64 "
            "AND length(candidate_set_sha256)=64",
            name="ck_embedding_evaluation_receipt_contract",
        ),
    )
    for name in _PROMOTION_COLUMNS:
        op.add_column(
            "search_embedding_model_promotions",
            sa.Column(name, sa.Text(), nullable=True),
        )

    # Preserve historical promotions and authorize their already-published
    # runtime bytes. They remain legacy: no evaluation/approval receipts are
    # invented, so they cannot activate after this migration.
    op.execute(
        "INSERT INTO search_embedding_runtime_registrations "
        "(runtime_registration_id,idempotency_key,purpose,provider,model,dimensions,"
        "runtime_artifact_json,runtime_artifact_sha256,registered_at) "
        "SELECT 'legacy-runtime:'||runtime_artifact_sha256,"
        "'legacy-runtime:'||runtime_artifact_sha256,purpose,provider,model,dimensions,"
        "runtime_artifact_json,runtime_artifact_sha256,MIN(approved_at) "
        "FROM search_embedding_model_promotions "
        "WHERE runtime_artifact_json IS NOT NULL "
        "GROUP BY purpose,runtime_artifact_sha256"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_search_embedding_model_promotions_append_only")
    op.execute(
        "UPDATE search_embedding_model_promotions "
        "SET runtime_registration_id='legacy-runtime:'||runtime_artifact_sha256 "
        "WHERE runtime_artifact_sha256 IS NOT NULL"
    )
    op.execute(
        "CREATE TRIGGER trg_search_embedding_model_promotions_append_only "
        "BEFORE UPDATE ON search_embedding_model_promotions "
        "BEGIN SELECT RAISE(ABORT, 'embedding promotions are append-only'); END"
    )

    op.execute(
        "CREATE TRIGGER trg_embedding_runtime_registration_exact "
        "BEFORE INSERT ON search_embedding_runtime_registrations WHEN "
        "NEW.runtime_artifact_sha256<>fact_sha256(NEW.runtime_artifact_json) "
        "OR NEW.runtime_registration_id<>('embedding-runtime:'||fact_sha256("
        "json_object('purpose',NEW.purpose,"
        "'runtime_artifact_sha256',NEW.runtime_artifact_sha256,"
        "'version','embedding-runtime-registration.v1'))) "
        "OR NEW.idempotency_key<>NEW.runtime_registration_id "
        "OR (SELECT COUNT(*) FROM json_each(NEW.runtime_artifact_json))<>8 "
        "OR EXISTS (SELECT 1 FROM json_each(NEW.runtime_artifact_json) WHERE key NOT IN "
        "('component_versions','dimensions','execution_provider','execution_settings',"
        "'files','model','provider','schema_version')) "
        "OR json_extract(NEW.runtime_artifact_json,'$.provider')<>NEW.provider "
        "OR json_extract(NEW.runtime_artifact_json,'$.model')<>NEW.model "
        "OR json_extract(NEW.runtime_artifact_json,'$.dimensions')<>NEW.dimensions "
        "OR json_type(NEW.runtime_artifact_json,'$.files') IS NOT 'array' "
        "OR json_array_length(NEW.runtime_artifact_json,'$.files')<1 "
        "BEGIN SELECT RAISE(ABORT, 'embedding runtime registration digest mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_embedding_evaluation_receipt_exact "
        "BEFORE INSERT ON search_embedding_evaluation_receipts WHEN "
        "NEW.evaluation_artifact_sha256<>fact_sha256(NEW.evaluation_artifact_json) "
        "OR NEW.candidate_set_sha256<>fact_sha256(NEW.candidate_set_json) "
        "OR NEW.evaluation_receipt_id<>"
        "('embedding-evaluation:'||NEW.evaluation_artifact_sha256) "
        "OR NEW.idempotency_key<>NEW.evaluation_receipt_id "
        "OR (SELECT COUNT(*) FROM json_each(NEW.evaluation_artifact_json))<>9 "
        "OR EXISTS (SELECT 1 FROM json_each(NEW.evaluation_artifact_json) WHERE key NOT IN "
        "('candidate_coordinates','evaluated_at','golden_sha256','k','purpose',"
        "'reason','recommended_model','results','thresholds')) "
        "OR json_extract(NEW.evaluation_artifact_json,'$.purpose')<>NEW.purpose "
        "OR json_extract(NEW.evaluation_artifact_json,'$.golden_sha256')<>NEW.golden_sha256 "
        "OR datetime(json_extract(NEW.evaluation_artifact_json,'$.evaluated_at'))"
        "<>datetime(NEW.evaluated_at) "
        "OR json_extract(NEW.evaluation_artifact_json,'$.candidate_coordinates')"
        "<>NEW.candidate_set_json "
        "OR json_type(NEW.evaluation_artifact_json,'$.results') IS NOT 'array' "
        "OR json_array_length(NEW.evaluation_artifact_json,'$.results')"
        "<>json_array_length(NEW.candidate_set_json) "
        "OR EXISTS (SELECT 1 FROM json_each("
        "NEW.evaluation_artifact_json,'$.results') result "
        "WHERE (SELECT COUNT(*) FROM json_each(result.value))<>8 "
        "OR EXISTS (SELECT 1 FROM json_each(result.value) WHERE key NOT IN "
        "('case_count','coverage','mean_latency_ms','model','mrr','ndcg',"
        "'recall_at_k','runtime_artifact_sha256')) "
        "OR NOT EXISTS (SELECT 1 FROM json_each(NEW.candidate_set_json) candidate "
        "WHERE candidate.key=result.key "
        "AND json_extract(candidate.value,'$.model')="
        "json_extract(result.value,'$.model') "
        "AND json_extract(candidate.value,'$.runtime_artifact_sha256')="
        "json_extract(result.value,'$.runtime_artifact_sha256'))) "
        "OR (json_extract(NEW.evaluation_artifact_json,'$.recommended_model') IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM json_each("
        "NEW.evaluation_artifact_json,'$.results') result "
        "WHERE json_extract(result.value,'$.model')="
        "json_extract(NEW.evaluation_artifact_json,'$.recommended_model'))) "
        "OR (SELECT COUNT(*) FROM json_each(NEW.candidate_set_json))<>2 "
        "OR (SELECT COUNT(*) FROM json_each(NEW.candidate_set_json) candidate "
        "WHERE json_extract(candidate.value,'$.model')="
        "'BAAI/bge-base-en-v1.5')<>1 "
        "OR (SELECT COUNT(*) FROM json_each(NEW.candidate_set_json) candidate "
        "WHERE json_extract(candidate.value,'$.model')="
        "'BAAI/bge-small-en-v1.5')<>1 "
        "OR (SELECT COUNT(DISTINCT json_extract(value,'$.model')) "
        "FROM json_each(NEW.candidate_set_json))<>"
        "json_array_length(NEW.candidate_set_json) "
        "OR (SELECT COUNT(DISTINCT json_extract(value,'$.runtime_registration_id')) "
        "FROM json_each(NEW.candidate_set_json))<>"
        "json_array_length(NEW.candidate_set_json) "
        "OR (SELECT COUNT(DISTINCT json_extract(value,'$.projection_seal_id')) "
        "FROM json_each(NEW.candidate_set_json))<>"
        "json_array_length(NEW.candidate_set_json) "
        "OR EXISTS (SELECT 1 FROM json_each(NEW.candidate_set_json) earlier "
        "JOIN json_each(NEW.candidate_set_json) later ON earlier.key<later.key "
        "WHERE json_extract(earlier.value,'$.model')>="
        "json_extract(later.value,'$.model')) "
        "OR EXISTS (SELECT 1 FROM json_each(NEW.candidate_set_json) candidate "
        "WHERE (SELECT COUNT(*) FROM json_each(candidate.value))<>12 "
        "OR EXISTS (SELECT 1 FROM json_each(candidate.value) WHERE key NOT IN "
        "('artifact_set_sha256','chunk_count','chunk_set_sha256','config_sha256',"
        "'index_run_id','manifest_id','model','projection_records_sha256',"
        "'projection_seal_id','runtime_artifact_sha256',"
        "'runtime_registration_id','sealed_at')) "
        "OR NOT EXISTS (SELECT 1 FROM search_projection_seals seal "
        "JOIN search_embedding_runtime_registrations runtime "
        "ON runtime.runtime_registration_id="
        "json_extract(candidate.value,'$.runtime_registration_id') "
        "WHERE seal.projection_seal_id="
        "json_extract(candidate.value,'$.projection_seal_id') "
        "AND seal.index_kind='vector' "
        "AND seal.provider='fastembed' "
        "AND seal.model=json_extract(candidate.value,'$.model') "
        "AND seal.index_run_id=json_extract(candidate.value,'$.index_run_id') "
        "AND seal.manifest_id=json_extract(candidate.value,'$.manifest_id') "
        "AND seal.chunk_count=json_extract(candidate.value,'$.chunk_count') "
        "AND seal.chunk_set_sha256=json_extract(candidate.value,'$.chunk_set_sha256') "
        "AND seal.projection_records_sha256="
        "json_extract(candidate.value,'$.projection_records_sha256') "
        "AND seal.artifact_set_sha256="
        "json_extract(candidate.value,'$.artifact_set_sha256') "
        "AND seal.config_sha256=json_extract(candidate.value,'$.config_sha256') "
        "AND datetime(seal.sealed_at)=datetime(json_extract(candidate.value,'$.sealed_at')) "
        "AND seal.runtime_artifact_sha256="
        "json_extract(candidate.value,'$.runtime_artifact_sha256') "
        "AND runtime.purpose=NEW.purpose "
        "AND runtime.provider=seal.provider "
        "AND runtime.model=json_extract(candidate.value,'$.model') "
        "AND runtime.dimensions=seal.dimensions "
        "AND runtime.runtime_artifact_sha256=seal.runtime_artifact_sha256)) "
        "OR (SELECT COUNT(DISTINCT "
        "json_extract(value,'$.manifest_id')||':'||"
        "json_extract(value,'$.chunk_count')||':'||"
        "json_extract(value,'$.chunk_set_sha256')) "
        "FROM json_each(NEW.candidate_set_json))<>1 "
        "BEGIN SELECT RAISE(ABORT, 'embedding evaluation receipt mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_embedding_promotions_governed "
        "BEFORE INSERT ON search_embedding_model_promotions WHEN "
        "NEW.evaluation_receipt_id IS NULL OR NEW.evaluation_artifact_json IS NULL "
        "OR NEW.runtime_registration_id IS NULL OR NEW.approval_receipt_json IS NULL "
        "OR NEW.approval_receipt_sha256 IS NULL "
        "OR NEW.evaluation_artifact_sha256<>fact_sha256(NEW.evaluation_artifact_json) "
        "OR NEW.approval_receipt_sha256<>fact_sha256(NEW.approval_receipt_json) "
        "OR (SELECT COUNT(*) FROM json_each(NEW.approval_receipt_json))<>7 "
        "OR EXISTS (SELECT 1 FROM json_each(NEW.approval_receipt_json) WHERE key NOT IN "
        "('approved_at','approved_by','approved_model','decision',"
        "'evaluation_artifact_sha256','purpose','rationale')) "
        "OR NOT EXISTS (SELECT 1 FROM search_embedding_runtime_registrations runtime "
        "WHERE runtime.runtime_registration_id=NEW.runtime_registration_id "
        "AND runtime.purpose=NEW.purpose AND runtime.provider=NEW.provider "
        "AND runtime.model=NEW.model AND runtime.dimensions=NEW.dimensions "
        "AND runtime.runtime_artifact_sha256=NEW.runtime_artifact_sha256 "
        "AND runtime.runtime_artifact_json=NEW.runtime_artifact_json) "
        "OR NOT EXISTS (SELECT 1 FROM search_embedding_evaluation_receipts receipt "
        "WHERE receipt.evaluation_receipt_id=NEW.evaluation_receipt_id "
        "AND receipt.evaluation_artifact_sha256=NEW.evaluation_artifact_sha256 "
        "AND receipt.evaluation_artifact_json=NEW.evaluation_artifact_json "
        "AND receipt.golden_sha256=NEW.golden_sha256 "
        "AND json_extract(receipt.evaluation_artifact_json,'$.recommended_model')=NEW.model) "
        "OR json_extract(NEW.approval_receipt_json,'$.decision')<>'approved' "
        "OR json_extract(NEW.approval_receipt_json,'$.purpose')<>NEW.purpose "
        "OR json_extract(NEW.approval_receipt_json,'$.approved_model')<>NEW.model "
        "OR json_extract(NEW.approval_receipt_json,'$.approved_by')<>NEW.approved_by "
        "OR datetime(json_extract(NEW.approval_receipt_json,'$.approved_at'))"
        "<>datetime(NEW.approved_at) "
        "OR json_extract(NEW.approval_receipt_json,'$.evaluation_artifact_sha256')"
        "<>NEW.evaluation_artifact_sha256 "
        "BEGIN SELECT RAISE(ABORT, 'embedding promotion governance receipt mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_population_cutover_embedding_governance "
        "BEFORE INSERT ON population_cutover_receipts WHEN NOT EXISTS ("
        "SELECT 1 FROM population_plane_receipts plane "
        "JOIN search_embedding_model_promotions promotion "
        "ON promotion.promotion_id=json_extract("
        "plane.canonical_details_json,'$.result.governance.promotion_id') "
        "JOIN search_embedding_evaluation_receipts evaluation "
        "ON evaluation.evaluation_receipt_id=json_extract("
        "plane.canonical_details_json,'$.result.governance.evaluation_receipt_id') "
        "JOIN search_embedding_runtime_registrations runtime "
        "ON runtime.runtime_registration_id=json_extract("
        "plane.canonical_details_json,'$.result.governance.runtime_registration_id') "
        "WHERE plane.population_run_id=NEW.population_run_id "
        "AND plane.plane_name='retrieval_runtime' AND plane.status='complete' "
        "AND json_type(plane.canonical_details_json,'$.result.governance')='object' "
        "AND (SELECT COUNT(*) FROM json_each("
        "plane.canonical_details_json,'$.result.governance'))=8 "
        "AND NOT EXISTS (SELECT 1 FROM json_each("
        "plane.canonical_details_json,'$.result.governance') WHERE key NOT IN "
        "('evaluation_evaluated_at','evaluation_receipt_id','promotion_id',"
        "'promotion_recorded_at','projection_seal_ids','projection_sealed_at',"
        "'runtime_registered_at','runtime_registration_id')) "
        "AND promotion.evaluation_receipt_id=evaluation.evaluation_receipt_id "
        "AND promotion.runtime_registration_id=runtime.runtime_registration_id "
        "AND promotion.evaluation_artifact_sha256="
        "evaluation.evaluation_artifact_sha256 "
        "AND promotion.runtime_artifact_sha256=runtime.runtime_artifact_sha256 "
        "AND datetime(promotion.recorded_at)=datetime(json_extract("
        "plane.canonical_details_json,'$.result.governance.promotion_recorded_at')) "
        "AND datetime(evaluation.evaluated_at)=datetime(json_extract("
        "plane.canonical_details_json,'$.result.governance.evaluation_evaluated_at')) "
        "AND datetime(runtime.registered_at)=datetime(json_extract("
        "plane.canonical_details_json,'$.result.governance.runtime_registered_at')) "
        "AND datetime(promotion.recorded_at)<=datetime(plane.observed_through) "
        "AND datetime(evaluation.evaluated_at)<=datetime(plane.observed_through) "
        "AND datetime(runtime.registered_at)<=datetime(plane.observed_through) "
        "AND json_type(plane.canonical_details_json,"
        "'$.result.governance.projection_seal_ids')='array' "
        "AND json_array_length(plane.canonical_details_json,"
        "'$.result.governance.projection_seal_ids')>0 "
        "AND NOT EXISTS (SELECT 1 FROM json_each(plane.canonical_details_json,"
        "'$.result.governance.projection_seal_ids') item "
        "JOIN json_each(plane.canonical_details_json,"
        "'$.result.governance.projection_seal_ids') prior "
        "ON prior.key<item.key WHERE prior.value>=item.value) "
        "AND (SELECT COUNT(*) FROM json_each(plane.canonical_details_json,"
        "'$.result.governance.projection_sealed_at'))="
        "json_array_length(plane.canonical_details_json,"
        "'$.result.governance.projection_seal_ids') "
        "AND NOT EXISTS (SELECT 1 FROM json_each(plane.canonical_details_json,"
        "'$.result.governance.projection_seal_ids') item "
        "WHERE NOT EXISTS (SELECT 1 FROM search_projection_seals seal "
        "JOIN json_each(plane.canonical_details_json,"
        "'$.result.governance.projection_sealed_at') seal_clock "
        "ON seal_clock.key=item.value "
        "WHERE seal.projection_seal_id=item.value "
        "AND seal.runtime_artifact_sha256=runtime.runtime_artifact_sha256 "
        "AND datetime(seal.sealed_at)=datetime(seal_clock.value) "
        "AND datetime(seal.sealed_at)<=datetime(plane.observed_through)))) "
        "BEGIN SELECT RAISE(ABORT, "
        "'population cutover requires governed embedding promotion'); END"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_embedding_artifacts_runtime_binding")
    op.execute("DROP TRIGGER IF EXISTS trg_vector_seals_runtime_binding")
    _candidate_runtime_triggers()
    _append_only("search_embedding_runtime_registrations")
    _append_only("search_embedding_evaluation_receipts")


def downgrade() -> None:
    bind = op.get_bind()
    orphan_count = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM search_embedding_runtime_registrations runtime "
                "WHERE NOT EXISTS (SELECT 1 FROM search_embedding_model_promotions promotion "
                "WHERE promotion.runtime_registration_id=runtime.runtime_registration_id)"
            )
        ).scalar()
        or 0
    )
    governed_count = int(
        bind.execute(
            sa.text(
                "SELECT (SELECT COUNT(*) FROM search_embedding_evaluation_receipts) "
                "+ (SELECT COUNT(*) FROM search_embedding_model_promotions "
                "WHERE evaluation_receipt_id IS NOT NULL)"
            )
        ).scalar()
        or 0
    )
    if orphan_count or governed_count:
        raise RuntimeError(
            "0257 downgrade would orphan candidate runtime or governed evaluation history"
        )
    for trigger in (
        "trg_population_cutover_embedding_governance",
        "trg_embedding_promotions_governed",
        "trg_embedding_evaluation_receipt_exact",
        "trg_embedding_runtime_registration_exact",
        "trg_search_embedding_evaluation_receipts_update_append_only",
        "trg_search_embedding_evaluation_receipts_delete_append_only",
        "trg_search_embedding_runtime_registrations_update_append_only",
        "trg_search_embedding_runtime_registrations_delete_append_only",
        "trg_embedding_artifacts_runtime_binding",
        "trg_vector_seals_runtime_binding",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.execute(
        "CREATE TRIGGER trg_embedding_artifacts_runtime_binding "
        "BEFORE INSERT ON search_embedding_artifacts "
        "WHEN NEW.outcome='succeeded' AND NOT EXISTS ("
        "SELECT 1 FROM search_embedding_model_promotions promotion "
        "WHERE promotion.provider=NEW.provider AND promotion.model=NEW.model "
        "AND promotion.dimensions=NEW.dimensions "
        "AND promotion.runtime_artifact_sha256=NEW.runtime_artifact_sha256) "
        "BEGIN SELECT RAISE(ABORT, "
        "'successful embedding requires promoted runtime artifact'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_vector_seals_runtime_binding "
        "BEFORE INSERT ON search_projection_seals "
        "WHEN NEW.index_kind='vector' AND NOT EXISTS ("
        "SELECT 1 FROM search_embedding_model_promotions promotion "
        "WHERE promotion.provider=NEW.provider AND promotion.model=NEW.model "
        "AND promotion.dimensions=NEW.dimensions "
        "AND promotion.runtime_artifact_sha256=NEW.runtime_artifact_sha256) "
        "BEGIN SELECT RAISE(ABORT, "
        "'vector seal requires one promoted runtime artifact'); END"
    )
    # SQLite reparses every schema trigger while rebuilding a table for
    # DROP COLUMN. Minimal supported legacy fixtures may intentionally omit
    # columns referenced by unrelated later triggers, so preserve the exact
    # trigger set and remove it only for the duration of this table rebuild.
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
        for name in reversed(_PROMOTION_COLUMNS):
            op.drop_column("search_embedding_model_promotions", name)
    finally:
        for _name, sql in triggers:
            op.execute(sql)
    op.drop_table("search_embedding_evaluation_receipts")
    op.drop_table("search_embedding_runtime_registrations")
