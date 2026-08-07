"""Add revisioned bridges from legacy documents to canonical evidence.

Revision ID: 0231_legacy_document_evidence_bindings
Revises: 0230_evidence_subject_bindings

The historical ``documents`` relation models an SEC accession as one logical
source, while SEC CompanyFacts is retrieved as one evolving aggregate response.
A direct ``evidence_document_versions.legacy_document_id`` bridge is therefore
too narrow: many accessions share one immutable response, and a later response
must not rewrite the evidence used by facts already captured.

This migration adds an append-only binding revision.  Fact triggers resolve
through its current projection first and retain the original direct bridge as
a compatibility path for every other writer.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0231_legacy_document_evidence_bindings"
down_revision: str | Sequence[str] | None = "0230_evidence_subject_bindings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "legacy_document_evidence_binding_revisions"
_VIEW = "v_legacy_document_evidence_bindings_current"
_FACT_TABLES = ("financial_facts", "kpi_facts")
_LINKS = "fact_observation_revisions"


def _append_only_triggers() -> None:
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_append_only BEFORE UPDATE ON {_TABLE} "
        "BEGIN SELECT RAISE(ABORT, 'legacy evidence binding is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_append_only_delete BEFORE DELETE ON {_TABLE} "
        "BEGIN SELECT RAISE(ABORT, 'legacy evidence binding is append-only'); END"
    )


def _direct_document_value(source_document: str, column: str) -> str:
    return (
        f"(SELECT version.{column} FROM evidence_document_versions AS version "
        f"WHERE version.legacy_document_id = {source_document} "
        "ORDER BY version.version_sequence DESC LIMIT 1)"
    )


def _bound_document_value(source_document: str, column: str) -> str:
    return (
        f"(SELECT version.{column} FROM {_VIEW} AS binding "
        "JOIN evidence_document_versions AS version "
        "ON version.document_version_id = binding.document_version_id "
        f"WHERE binding.legacy_document_id = {source_document} LIMIT 1)"
    )


def _document_value(source_document: str, column: str, *, use_binding: bool) -> str:
    direct = _direct_document_value(source_document, column)
    if not use_binding:
        return direct
    return f"COALESCE({_bound_document_value(source_document, column)}, {direct})"


def _direct_source_value(source_document: str, column: str) -> str:
    return (
        f"(SELECT source.{column} FROM evidence_document_versions AS version "
        "JOIN evidence_source_observations AS source "
        "ON source.observation_id = version.observation_id "
        f"WHERE version.legacy_document_id = {source_document} "
        "ORDER BY version.version_sequence DESC LIMIT 1)"
    )


def _bound_source_value(source_document: str, column: str) -> str:
    return (
        f"(SELECT source.{column} FROM {_VIEW} AS binding "
        "JOIN evidence_document_versions AS version "
        "ON version.document_version_id = binding.document_version_id "
        "JOIN evidence_source_observations AS source "
        "ON source.observation_id = version.observation_id "
        f"WHERE binding.legacy_document_id = {source_document} LIMIT 1)"
    )


def _source_value(source_document: str, column: str, *, use_binding: bool) -> str:
    direct = _direct_source_value(source_document, column)
    if not use_binding:
        return direct
    return f"COALESCE({_bound_source_value(source_document, column)}, {direct})"


def _direct_evidence_node(source_document: str) -> str:
    return (
        "(SELECT node.node_id FROM evidence_document_versions AS version "
        "JOIN evidence_extraction_runs AS run "
        "ON run.document_version_id = version.document_version_id "
        "JOIN evidence_nodes AS node ON node.extraction_run_id = run.extraction_run_id "
        f"WHERE version.legacy_document_id = {source_document} "
        "AND run.outcome = 'succeeded' AND node.node_kind = 'document' "
        "ORDER BY version.version_sequence DESC, node.revision DESC, "
        "run.completed_at DESC, node.node_id DESC LIMIT 1)"
    )


def _evidence_node(source_document: str, *, use_binding: bool) -> str:
    direct = _direct_evidence_node(source_document)
    if not use_binding:
        return direct
    bound = (
        f"(SELECT binding.evidence_node_id FROM {_VIEW} AS binding "
        f"WHERE binding.legacy_document_id = {source_document} LIMIT 1)"
    )
    return f"COALESCE({bound}, {direct})"


def _period_type_expression(prefix: str) -> str:
    return (
        f"CASE UPPER({prefix}.fiscal_period_type) "
        "WHEN 'Q1' THEN 'quarter' WHEN 'Q2' THEN 'quarter' "
        "WHEN 'Q3' THEN 'quarter' WHEN 'Q4' THEN 'quarter' "
        "WHEN 'FY' THEN 'annual' WHEN 'ANNUAL' THEN 'annual' "
        "WHEN 'YTD' THEN 'year_to_date' WHEN 'TTM' THEN 'year_to_date' "
        "WHEN 'INSTANT' THEN 'instant' ELSE 'other' END"
    )


def _fact_expressions(table: str, prefix: str) -> tuple[str, str, str, str]:
    if table == "financial_facts":
        return (
            f"{prefix}.line_item",
            "'[]'",
            f"{prefix}.currency",
            (
                f"CASE WHEN LOWER(COALESCE({prefix}.extracted_by, '')) LIKE '%derived%' "
                "THEN 'derived' ELSE 'reported' END"
            ),
        )
    return (
        f"'kpi_definition:' || {prefix}.kpi_definition_id",
        (
            "'[{\"key\":\"kpi_definition_id\",\"value\":\"' || "
            + prefix
            + ".kpi_definition_id || '\"}]'"
        ),
        "NULL",
        (
            f"CASE WHEN {prefix}.computed_from IS NOT NULL "
            f"OR LOWER(COALESCE({prefix}.extracted_by, '')) LIKE '%derived%' "
            "THEN 'derived' ELSE 'reported' END"
        ),
    )


def _logical_key_expression(table: str, prefix: str) -> str:
    concept = (
        f"{prefix}.line_item"
        if table == "financial_facts"
        else f"'kpi_definition:' || {prefix}.kpi_definition_id"
    )
    return (
        f"'{table}:' || UPPER({prefix}.ticker) || ':' || {concept} || ':' || "
        f"substr({prefix}.period_end, 1, 10) || ':' || UPPER({prefix}.fiscal_period_type)"
    )


def _capture_statements(
    table: str,
    *,
    revision_expression: str,
    use_binding: bool,
) -> str:
    prefix = "NEW"
    source_document = f"{prefix}.source_doc_id"
    evidence_node = _evidence_node(source_document, use_binding=use_binding)
    issuer_id = _document_value(source_document, "issuer_id", use_binding=use_binding)
    period_start = _document_value(source_document, "period_start", use_binding=use_binding)
    available_at = _source_value(source_document, "retrieved_at", use_binding=use_binding)
    source_tier = (
        f"(SELECT source_quality_tier FROM documents WHERE id = {source_document})"
    )
    concept, dimensions, currency, observation_status = _fact_expressions(table, prefix)
    logical_key = _logical_key_expression(table, prefix)
    observation_id = (
        f"'{table}:' || {prefix}.id || ':' || 'r' || ({revision_expression})"
    )
    captured_at = "strftime('%Y-%m-%dT%H:%M:%f', 'now')"
    locator = f"{prefix}.locator"
    return (
        "SELECT CASE WHEN "
        + evidence_node
        + " IS NULL THEN RAISE(ABORT, 'fact write requires an evidence-backed source document') END; "
        "INSERT INTO reported_observations "
        "(observation_id, idempotency_key, issuer_id, ticker, concept_key, period_start, "
        "period_end, fiscal_period_type, dimensions_json, numeric_value, text_value, currency, "
        "unit, scale, observation_status, evidence_node_id, available_at, recorded_at, method, "
        "method_version, confidence, legacy_table, legacy_row_id) SELECT "
        f"{observation_id}, 'fact-capture:' || {observation_id}, {issuer_id}, "
        f"UPPER({prefix}.ticker), {concept}, COALESCE({period_start}, {prefix}.period_end), "
        f"{prefix}.period_end, {_period_type_expression(prefix)}, {dimensions}, "
        f"CAST({prefix}.value AS TEXT), NULL, {currency}, {prefix}.unit, 0, "
        f"{observation_status}, {evidence_node}, {available_at}, "
        f"CASE WHEN {captured_at} < {available_at} THEN {available_at} ELSE {captured_at} END, "
        f"COALESCE({prefix}.extracted_by, 'legacy-fact-writer'), '0231-trigger-v1', "
        f"{prefix}.confidence, NULL, NULL; "
        f"INSERT INTO {_LINKS} "
        "(fact_table, fact_row_id, fact_revision, observation_id, logical_key, "
        "source_document_id, source_tier, locator_json, captured_at) SELECT "
        f"'{table}', {prefix}.id, ({revision_expression}), {observation_id}, "
        f"{logical_key}, {source_document}, {source_tier}, {locator}, "
        f"CASE WHEN {captured_at} < {available_at} THEN {available_at} ELSE {captured_at} END; "
    )


def _capture_triggers(table: str, *, use_binding: bool) -> None:
    for suffix in ("insert", "update"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_observation_{suffix}")
    insert_revision = "1"
    next_revision = (
        f"COALESCE((SELECT MAX(fact_revision) + 1 FROM {_LINKS} "
        f"WHERE fact_table = '{table}' AND fact_row_id = NEW.id), 1)"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_observation_insert AFTER INSERT ON {table} BEGIN "
        f"{_capture_statements(table, revision_expression=insert_revision, use_binding=use_binding)} "
        "END"
    )
    semantic_columns = (
        "ticker, period_end, fiscal_period_type, line_item, value, currency, unit, "
        "source_doc_id, extracted_by, locator"
        if table == "financial_facts"
        else "ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, "
        "source_doc_id, extracted_by, locator, source_excerpt, computed_from, "
        "formula_id, formula_version"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_observation_update AFTER UPDATE OF {semantic_columns} "
        f"ON {table} BEGIN "
        f"{_capture_statements(table, revision_expression=next_revision, use_binding=use_binding)} "
        "END"
    )


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    op.create_table(
        _TABLE,
        sa.Column("binding_revision_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("legacy_document_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "document_version_id",
            sa.String(128),
            sa.ForeignKey("evidence_document_versions.document_version_id"),
            nullable=False,
        ),
        sa.Column(
            "evidence_node_id",
            sa.String(128),
            sa.ForeignKey("evidence_nodes.node_id"),
            nullable=False,
        ),
        sa.Column("scope_locator_json", sa.Text(), nullable=False),
        sa.Column("scope_locator_sha256", sa.String(64), nullable=False),
        sa.Column("scope_content_sha256", sa.String(64), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column(
            "supersedes_binding_revision_id",
            sa.String(128),
            sa.ForeignKey(f"{_TABLE}.binding_revision_id"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "legacy_document_id",
            "revision",
            name="uq_legacy_document_evidence_binding_revision",
        ),
        sa.CheckConstraint("revision > 0", name="ck_legacy_document_evidence_binding_revision"),
        sa.CheckConstraint(
            "json_valid(scope_locator_json)",
            name="ck_legacy_document_evidence_binding_locator_json",
        ),
        sa.CheckConstraint(
            "length(scope_locator_sha256) = 64",
            name="ck_legacy_document_evidence_binding_locator_sha",
        ),
        sa.CheckConstraint(
            "length(scope_content_sha256) = 64",
            name="ck_legacy_document_evidence_binding_content_sha",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_legacy_document_evidence_binding_clocks",
        ),
    )
    op.create_index(
        "ix_legacy_document_evidence_binding_current",
        _TABLE,
        ["legacy_document_id", "revision"],
    )
    op.create_index(
        "ix_legacy_document_evidence_binding_document",
        _TABLE,
        ["document_version_id", "evidence_node_id"],
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_revision_first BEFORE INSERT ON {_TABLE} "
        "WHEN NEW.revision = 1 AND NEW.supersedes_binding_revision_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'first legacy evidence binding cannot supersede'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_revision_parent BEFORE INSERT ON {_TABLE} "
        "WHEN NEW.revision > 1 AND (NEW.supersedes_binding_revision_id IS NULL OR "
        f"NOT EXISTS (SELECT 1 FROM {_TABLE} AS prior "
        "WHERE prior.binding_revision_id = NEW.supersedes_binding_revision_id "
        "AND prior.legacy_document_id = NEW.legacy_document_id "
        "AND prior.revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'legacy evidence binding must supersede prior revision'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_node_scope BEFORE INSERT ON {_TABLE} "
        "WHEN NOT EXISTS ("
        "SELECT 1 FROM evidence_nodes AS node "
        "JOIN evidence_extraction_runs AS run "
        "ON run.extraction_run_id = node.extraction_run_id "
        "WHERE node.node_id = NEW.evidence_node_id "
        "AND run.document_version_id = NEW.document_version_id "
        "AND node.node_kind IN ('document', 'section') AND run.outcome = 'succeeded') "
        "BEGIN SELECT RAISE(ABORT, "
        "'legacy evidence node must belong to same document version'); END"
    )
    if "documents" in existing:
        op.execute(
            f"CREATE TRIGGER trg_{_TABLE}_legacy_document BEFORE INSERT ON {_TABLE} "
            "WHEN NOT EXISTS (SELECT 1 FROM documents "
            "WHERE id = NEW.legacy_document_id) "
            "BEGIN SELECT RAISE(ABORT, 'legacy evidence binding document does not exist'); END"
        )
    _append_only_triggers()
    op.execute(
        f"CREATE VIEW {_VIEW} AS SELECT binding.* FROM {_TABLE} AS binding "
        f"WHERE NOT EXISTS (SELECT 1 FROM {_TABLE} AS newer "
        "WHERE newer.legacy_document_id = binding.legacy_document_id "
        "AND newer.revision > binding.revision)"
    )
    for table in _FACT_TABLES:
        if table in existing:
            _capture_triggers(table, use_binding=True)


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table in _FACT_TABLES:
        if table in existing:
            _capture_triggers(table, use_binding=False)
    op.execute(f"DROP VIEW IF EXISTS {_VIEW}")
    for trigger in (
        f"trg_{_TABLE}_append_only",
        f"trg_{_TABLE}_append_only_delete",
        f"trg_{_TABLE}_revision_first",
        f"trg_{_TABLE}_revision_parent",
        f"trg_{_TABLE}_node_scope",
        f"trg_{_TABLE}_legacy_document",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.drop_index("ix_legacy_document_evidence_binding_document", table_name=_TABLE)
    op.drop_index("ix_legacy_document_evidence_binding_current", table_name=_TABLE)
    op.drop_table(_TABLE)
