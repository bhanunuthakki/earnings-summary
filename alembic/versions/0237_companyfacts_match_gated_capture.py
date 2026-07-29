"""Gate legacy CompanyFacts observation capture on deterministic match proof.

Revision ID: 0237_companyfacts_match_gated_capture
Revises: 0236_fact_observation_match_proofs

The 0231 triggers remain authoritative for ordinary evidence-backed facts.
CompanyFacts facts are different: their source document is an accession scoped
into an aggregate snapshot, so automatic trigger capture would create an
observation before the accession cell has been deterministically matched.

This migration adds only a trigger ``WHEN`` guard.  It validates the complete
0231 trigger body before replacing it and validates the guarded body before
restoring the exact predecessor SQL on downgrade.  Any drift fails closed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0237_companyfacts_match_gated_capture"
down_revision: str | Sequence[str] | None = "0236_fact_observation_match_proofs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BINDING_VIEW = "v_legacy_document_evidence_bindings_current"
_FACT_TABLES = ("financial_facts", "kpi_facts")
_LINKS = "fact_observation_revisions"
_TRIGGER_MARKER = "'0231-trigger-v1'"


def _direct_document_value(source_document: str, column: str) -> str:
    return (
        f"(SELECT version.{column} FROM evidence_document_versions AS version "
        f"WHERE version.legacy_document_id = {source_document} "
        "ORDER BY version.version_sequence DESC LIMIT 1)"
    )


def _bound_document_value(source_document: str, column: str) -> str:
    return (
        f"(SELECT version.{column} FROM {_BINDING_VIEW} AS binding "
        "JOIN evidence_document_versions AS version "
        "ON version.document_version_id = binding.document_version_id "
        f"WHERE binding.legacy_document_id = {source_document} LIMIT 1)"
    )


def _document_value(source_document: str, column: str) -> str:
    return (
        f"COALESCE({_bound_document_value(source_document, column)}, "
        f"{_direct_document_value(source_document, column)})"
    )


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
        f"(SELECT source.{column} FROM {_BINDING_VIEW} AS binding "
        "JOIN evidence_document_versions AS version "
        "ON version.document_version_id = binding.document_version_id "
        "JOIN evidence_source_observations AS source "
        "ON source.observation_id = version.observation_id "
        f"WHERE binding.legacy_document_id = {source_document} LIMIT 1)"
    )


def _source_value(source_document: str, column: str) -> str:
    return (
        f"COALESCE({_bound_source_value(source_document, column)}, "
        f"{_direct_source_value(source_document, column)})"
    )


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


def _evidence_node(source_document: str) -> str:
    bound = (
        f"(SELECT binding.evidence_node_id FROM {_BINDING_VIEW} AS binding "
        f"WHERE binding.legacy_document_id = {source_document} LIMIT 1)"
    )
    return f"COALESCE({bound}, {_direct_evidence_node(source_document)})"


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


def _capture_statements(table: str, *, revision_expression: str) -> str:
    prefix = "NEW"
    source_document = f"{prefix}.source_doc_id"
    evidence_node = _evidence_node(source_document)
    issuer_id = _document_value(source_document, "issuer_id")
    period_start = _document_value(source_document, "period_start")
    available_at = _source_value(source_document, "retrieved_at")
    source_tier = (
        f"(SELECT source_quality_tier FROM documents WHERE id = {source_document})"
    )
    concept, dimensions, currency, observation_status = _fact_expressions(
        table, prefix
    )
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
        f"COALESCE({prefix}.extracted_by, 'legacy-fact-writer'), {_TRIGGER_MARKER}, "
        f"{prefix}.confidence, NULL, NULL; "
        f"INSERT INTO {_LINKS} "
        "(fact_table, fact_row_id, fact_revision, observation_id, logical_key, "
        "source_document_id, source_tier, locator_json, captured_at) SELECT "
        f"'{table}', {prefix}.id, ({revision_expression}), {observation_id}, "
        f"{logical_key}, {source_document}, {source_tier}, {locator}, "
        f"CASE WHEN {captured_at} < {available_at} THEN {available_at} ELSE {captured_at} END; "
    )


def _original_trigger_sql(table: str, suffix: str) -> str:
    if suffix == "insert":
        return (
            f"CREATE TRIGGER trg_{table}_observation_insert AFTER INSERT ON {table} BEGIN "
            f"{_capture_statements(table, revision_expression='1')} END"
        )
    semantic_columns = (
        "ticker, period_end, fiscal_period_type, line_item, value, currency, unit, "
        "source_doc_id, extracted_by, locator"
        if table == "financial_facts"
        else "ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, "
        "source_doc_id, extracted_by, locator, source_excerpt, computed_from, "
        "formula_id, formula_version"
    )
    next_revision = (
        f"COALESCE((SELECT MAX(fact_revision) + 1 FROM {_LINKS} "
        f"WHERE fact_table = '{table}' AND fact_row_id = NEW.id), 1)"
    )
    return (
        f"CREATE TRIGGER trg_{table}_observation_update AFTER UPDATE OF {semantic_columns} "
        f"ON {table} BEGIN "
        f"{_capture_statements(table, revision_expression=next_revision)} END"
    )


def _companyfacts_guard() -> str:
    source_document = "NEW.source_doc_id"
    return (
        f"COALESCE({_bound_source_value(source_document, 'source_kind')}, "
        f"{_direct_source_value(source_document, 'source_kind')}, '') "
        "<> 'sec_companyfacts'"
    )


def _guarded_trigger_sql(table: str, suffix: str) -> str:
    original = _original_trigger_sql(table, suffix)
    anchor = (
        f"AFTER INSERT ON {table} BEGIN "
        if suffix == "insert"
        else f"ON {table} BEGIN "
    )
    if original.count(anchor) != 1:
        raise RuntimeError(f"internal 0237 trigger anchor drift for {table} {suffix}")
    return original.replace(
        anchor,
        f"{anchor.removesuffix('BEGIN ')}WHEN {_companyfacts_guard()} BEGIN ",
        1,
    )


def _canonical_sql(sql: str) -> str:
    return " ".join(sql.split())


def _read_trigger_sql(bind: sa.engine.Connection, name: str) -> str:
    row = bind.execute(
        sa.text(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'trigger' AND name = :name"
        ),
        {"name": name},
    ).scalar_one_or_none()
    if not isinstance(row, str):
        raise RuntimeError(f"0237 requires predecessor trigger {name}")
    return row


def _validate_trigger(
    bind: sa.engine.Connection,
    *,
    table: str,
    suffix: str,
    guarded: bool,
) -> str:
    name = f"trg_{table}_observation_{suffix}"
    actual = _read_trigger_sql(bind, name)
    expected = (
        _guarded_trigger_sql(table, suffix)
        if guarded
        else _original_trigger_sql(table, suffix)
    )
    if _TRIGGER_MARKER not in actual or _canonical_sql(actual) != _canonical_sql(
        expected
    ):
        state = "0237 guarded" if guarded else "0231 predecessor"
        raise RuntimeError(f"{name} does not match the expected {state} body")
    return expected


def _replace_triggers(*, guarded: bool) -> None:
    bind = op.get_bind()
    required_tables = {
        "evidence_document_versions",
        "evidence_source_observations",
        _LINKS,
    }
    existing_tables = set(sa.inspect(bind).get_table_names())
    missing = sorted(required_tables - existing_tables)
    if missing:
        raise RuntimeError(
            "0237 requires hardened predecessor tables: " + ", ".join(missing)
        )
    legacy_fact_tables = tuple(
        table for table in _FACT_TABLES if table in existing_tables
    )
    if not legacy_fact_tables:
        return
    existing_views = set(sa.inspect(bind).get_view_names())
    if _BINDING_VIEW not in existing_views:
        raise RuntimeError(f"0237 requires predecessor view {_BINDING_VIEW}")

    replacements: list[tuple[str, str]] = []
    for table in legacy_fact_tables:
        for suffix in ("insert", "update"):
            _validate_trigger(
                bind,
                table=table,
                suffix=suffix,
                guarded=not guarded,
            )
            name = f"trg_{table}_observation_{suffix}"
            replacement = (
                _guarded_trigger_sql(table, suffix)
                if guarded
                else _original_trigger_sql(table, suffix)
            )
            replacements.append((name, replacement))

    for name, replacement in replacements:
        op.execute(f"DROP TRIGGER {name}")
        op.execute(replacement)


def upgrade() -> None:
    _replace_triggers(guarded=True)


def downgrade() -> None:
    _replace_triggers(guarded=False)
