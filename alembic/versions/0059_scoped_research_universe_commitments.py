"""Bind scoped research universes to the exact immutable request scope.

Only the canonical JSON expression changes. Existing source/entity guards,
hashes, rows and legacy four-field payload bytes remain unchanged. The DDL uses
one savepoint, so interrupted replacement cannot leave an unguarded table.
"""

from __future__ import annotations

from alembic import op

revision = "0059_scoped_research_universe_commitments"
down_revision = "0058_sealed_snapshot_admission_fences"
branch_labels = None
depends_on = None

_TRIGGER = "trg_research_snapshot_universe_exact"
_LEGACY_TRIGGER_SQL = (
    "CREATE TRIGGER trg_research_snapshot_universe_exact BEFORE INSERT ON "
    "research_snapshot_universe_commitments WHEN NOT EXISTS (SELECT 1 FROM "
    "research_snapshot_headers AS header WHERE "
    "header.research_snapshot_id=NEW.research_snapshot_id AND header.cutoff_at=NEW.cutoff_at"
    " AND header.recorded_at=NEW.recorded_at AND "
    "json_extract(header.request_json,'$.research_universe.issuer_id')=NEW.issuer_id AND "
    "json_extract(header.request_json,'$.research_universe.reporting_entity_ids')=NEW.reporting_entity_ids_json"
    " AND "
    "json_extract(header.request_json,'$.research_universe.document_version_ids')=NEW.document_version_ids_json"
    " AND "
    "json_extract(header.request_json,'$.research_universe.source_obligation_revision_ids')=NEW.source_obligation_revision_ids_json)"
    " OR "
    "NEW.canonical_universe_json<>json_object('document_version_ids',json(NEW.document_version_ids_json),'issuer_id',NEW.issuer_id,'reporting_entity_ids',json(NEW.reporting_entity_ids_json),'source_obligation_revision_ids',json(NEW.source_obligation_revision_ids_json))"
    " OR NEW.universe_sha256<>fact_sha256(NEW.canonical_universe_json) OR (SELECT COUNT(*) "
    "FROM json_each(NEW.reporting_entity_ids_json)) <>(SELECT COUNT(DISTINCT value) FROM "
    "json_each(NEW.reporting_entity_ids_json)) OR (SELECT COUNT(*) FROM "
    "json_each(NEW.document_version_ids_json)) <>(SELECT COUNT(DISTINCT value) FROM "
    "json_each(NEW.document_version_ids_json)) OR (SELECT COUNT(*) FROM "
    "json_each(NEW.source_obligation_revision_ids_json)) <>(SELECT COUNT(DISTINCT value) "
    "FROM json_each(NEW.source_obligation_revision_ids_json)) OR EXISTS (SELECT 1 FROM "
    "json_each(NEW.reporting_entity_ids_json) item LEFT JOIN reporting_entities entity ON "
    "entity.reporting_entity_id=item.value WHERE item.type<>'text' OR length(item.value)=0 "
    "OR entity.reporting_entity_id IS NULL OR entity.issuer_id<>NEW.issuer_id) OR EXISTS "
    "(SELECT 1 FROM json_each(NEW.document_version_ids_json) item LEFT JOIN "
    "v_evidence_document_versions_canonical document ON "
    "document.document_version_id=item.value WHERE item.type<>'text' OR length(item.value)=0"
    " OR document.document_version_id IS NULL OR document.issuer_id<>NEW.issuer_id OR "
    "document.reporting_entity_id IS NULL OR NOT EXISTS (SELECT 1 FROM "
    "json_each(NEW.reporting_entity_ids_json) entity_id WHERE "
    "entity_id.value=document.reporting_entity_id)) OR EXISTS (SELECT 1 FROM "
    "json_each(NEW.source_obligation_revision_ids_json) item LEFT JOIN "
    "source_obligation_revisions obligation ON obligation.obligation_revision_id=item.value "
    "WHERE item.type<>'text' OR length(item.value)=0 OR obligation.obligation_revision_id IS"
    " NULL OR obligation.issuer_id<>NEW.issuer_id OR obligation.reporting_entity_id IS NULL "
    "OR NOT EXISTS (SELECT 1 FROM json_each(NEW.reporting_entity_ids_json) entity_id WHERE "
    "entity_id.value=obligation.reporting_entity_id)) BEGIN SELECT RAISE(ABORT, 'research "
    "universe commitment mismatch'); END"
)
_LEGACY_CANONICAL = (
    "json_object('document_version_ids',json(NEW.document_version_ids_json),"
    "'issuer_id',NEW.issuer_id,"
    "'reporting_entity_ids',json(NEW.reporting_entity_ids_json),"
    "'source_obligation_revision_ids',json(NEW.source_obligation_revision_ids_json))"
)
_SCOPE = (
    "(SELECT json_extract(header.request_json,'$.research_universe.analysis_scope') "
    "FROM research_snapshot_headers header "
    "WHERE header.research_snapshot_id=NEW.research_snapshot_id)"
)
_SCOPE_TYPE = (
    "(SELECT json_type(header.request_json,'$.research_universe.analysis_scope') "
    "FROM research_snapshot_headers header "
    "WHERE header.research_snapshot_id=NEW.research_snapshot_id)"
)
# Python canonical_json sorts object keys. The request already stores the scope
# with that same canonical encoding; json() retains its exact nested content.
_SCOPED_CANONICAL = (
    f"CASE WHEN {_SCOPE_TYPE}='object' THEN json_object("
    f"'analysis_scope',json({_SCOPE}),"
    "'document_version_ids',json(NEW.document_version_ids_json),"
    "'issuer_id',NEW.issuer_id,"
    "'reporting_entity_ids',json(NEW.reporting_entity_ids_json),"
    "'source_obligation_revision_ids',json(NEW.source_obligation_revision_ids_json)) "
    f"WHEN {_SCOPE_TYPE} IS NULL THEN {_LEGACY_CANONICAL} ELSE NULL END"
)
_SCOPED_TRIGGER_SQL = _LEGACY_TRIGGER_SQL.replace(
    "NEW.canonical_universe_json<>" + _LEGACY_CANONICAL,
    "NEW.canonical_universe_json IS NOT " + _SCOPED_CANONICAL,
    1,
)


def _normalized(sql: str) -> str:
    return " ".join(
        sql.replace("CREATE TRIGGER IF NOT EXISTS ", "CREATE TRIGGER ", 1)
        .strip()
        .removesuffix(";")
        .split()
    )


def _replace(expected: str, desired: str) -> None:
    bind = op.get_bind()
    row = bind.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (_TRIGGER,)
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "research universe trigger is missing; refusing an unverified replacement"
        )
    current = _normalized(str(row[0]))
    if current == _normalized(desired):
        return
    if current != _normalized(expected):
        raise RuntimeError("research universe trigger differs from the expected migration owner")
    with bind.begin_nested():
        op.execute(f"DROP TRIGGER {_TRIGGER}")
        op.execute(desired)


def upgrade() -> None:
    _replace(_LEGACY_TRIGGER_SQL, _SCOPED_TRIGGER_SQL)


def downgrade() -> None:
    retained = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT 1 FROM research_snapshot_universe_commitments "
            "WHERE json_type(canonical_universe_json,'$.analysis_scope') IS NOT NULL LIMIT 1"
        )
        .fetchone()
    )
    if retained is not None:
        raise RuntimeError("downgrade refused: scoped research universe commitments are retained")
    _replace(_SCOPED_TRIGGER_SQL, _LEGACY_TRIGGER_SQL)
