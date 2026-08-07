"""Cut financial and KPI facts over to immutable observations and resolutions.

Every post-cutover INSERT and every semantic/provenance UPDATE is captured as
an immutable ``reported_observations`` revision anchored to the exact legacy
document's evidence node.  Canonical views expose only the selected observation
from a resolved, complete candidate set; unresolved material conflicts remain
preserved but fail closed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0225_financial_fact_resolution_cutover"
down_revision: str | Sequence[str] | None = "0224_expected_document_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LINKS = "fact_observation_revisions"
_OUTCOMES = "fact_resolution_outcomes"
_FACT_TABLES = ("financial_facts", "kpi_facts")


def _append_only_triggers(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'financial fact resolution ledger is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'financial fact resolution ledger is append-only'); END"
    )


def _evidence_node_expression(source_document: str) -> str:
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


def _document_value_expression(source_document: str, column: str) -> str:
    return (
        f"(SELECT version.{column} FROM evidence_document_versions AS version "
        f"WHERE version.legacy_document_id = {source_document} "
        "ORDER BY version.version_sequence DESC LIMIT 1)"
    )


def _source_observation_value_expression(source_document: str, column: str) -> str:
    return (
        f"(SELECT source.{column} FROM evidence_document_versions AS version "
        "JOIN evidence_source_observations AS source "
        "ON source.observation_id = version.observation_id "
        f"WHERE version.legacy_document_id = {source_document} "
        "ORDER BY version.version_sequence DESC LIMIT 1)"
    )


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
        concept = f"{prefix}.line_item"
        dimensions = "'[]'"
        currency = f"{prefix}.currency"
        derived = (
            f"CASE WHEN LOWER(COALESCE({prefix}.extracted_by, '')) LIKE '%derived%' "
            "THEN 'derived' ELSE 'reported' END"
        )
    else:
        concept = f"'kpi_definition:' || {prefix}.kpi_definition_id"
        dimensions = (
            "'[{\"key\":\"kpi_definition_id\",\"value\":\"' || "
            + prefix
            + ".kpi_definition_id || '\"}]'"
        )
        currency = "NULL"
        derived = (
            f"CASE WHEN {prefix}.computed_from IS NOT NULL "
            f"OR LOWER(COALESCE({prefix}.extracted_by, '')) LIKE '%derived%' "
            "THEN 'derived' ELSE 'reported' END"
        )
    return concept, dimensions, currency, derived


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
    evidence_node = _evidence_node_expression(source_document)
    issuer_id = _document_value_expression(source_document, "issuer_id")
    period_start = _document_value_expression(source_document, "period_start")
    available_at = _source_observation_value_expression(source_document, "retrieved_at")
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
        f"COALESCE({prefix}.extracted_by, 'legacy-fact-writer'), '0225-trigger-v1', "
        f"{prefix}.confidence, NULL, NULL; "
        f"INSERT INTO {_LINKS} "
        "(fact_table, fact_row_id, fact_revision, observation_id, logical_key, "
        "source_document_id, source_tier, locator_json, captured_at) SELECT "
        f"'{table}', {prefix}.id, ({revision_expression}), {observation_id}, "
        f"{logical_key}, {source_document}, {source_tier}, {locator}, "
        f"CASE WHEN {captured_at} < {available_at} THEN {available_at} ELSE {captured_at} END; "
    )


def _capture_triggers(table: str) -> None:
    insert_revision = "1"
    next_revision = (
        f"COALESCE((SELECT MAX(fact_revision) + 1 FROM {_LINKS} "
        f"WHERE fact_table = '{table}' AND fact_row_id = NEW.id), 1)"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_observation_insert AFTER INSERT ON {table} BEGIN "
        f"{_capture_statements(table, revision_expression=insert_revision)} END"
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
        f"ON {table} BEGIN {_capture_statements(table, revision_expression=next_revision)} END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_observation_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'financial fact history is append-only after cutover'); END"
    )


def _resolved_view(table: str) -> None:
    view = (
        "v_financial_facts_resolved_current"
        if table == "financial_facts"
        else "v_kpi_facts_resolved_current"
    )
    op.execute(
        f"CREATE VIEW {view} AS SELECT fact.*, link.observation_id AS reported_observation_id, "
        "resolution.resolution_id, resolution.policy_version AS resolution_policy_version, "
        "resolution.reason AS resolution_reason, "
        "resolution.material_dissent AS resolution_material_dissent, "
        "outcome.checks_json AS resolution_checks_json "
        f"FROM {table} AS fact "
        f"JOIN {_LINKS} AS link ON link.fact_table = '{table}' "
        "AND link.fact_row_id = fact.id "
        "AND link.fact_revision = (SELECT MAX(latest.fact_revision) "
        f"FROM {_LINKS} AS latest WHERE latest.fact_table = '{table}' "
        "AND latest.fact_row_id = fact.id) "
        "JOIN v_observation_resolution_current AS resolution "
        "ON resolution.selected_observation_id = link.observation_id "
        f"JOIN {_OUTCOMES} AS outcome ON outcome.resolution_id = resolution.resolution_id "
        "AND outcome.resolution_status = 'resolved'"
    )


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if _LINKS not in existing:
        op.create_table(
            _LINKS,
            sa.Column("fact_table", sa.String(length=32), primary_key=True),
            sa.Column("fact_row_id", sa.Integer(), primary_key=True),
            sa.Column("fact_revision", sa.Integer(), primary_key=True),
            sa.Column(
                "observation_id",
                sa.String(length=128),
                sa.ForeignKey("reported_observations.observation_id"),
                nullable=False,
                unique=True,
            ),
            sa.Column("logical_key", sa.String(length=256), nullable=False),
            sa.Column("source_document_id", sa.Integer(), nullable=False),
            sa.Column("source_tier", sa.String(length=32), nullable=False),
            sa.Column("locator_json", sa.Text(), nullable=True),
            sa.Column("captured_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "fact_table IN ('financial_facts', 'kpi_facts')",
                name="ck_fact_observation_table",
            ),
            sa.CheckConstraint("fact_row_id > 0", name="ck_fact_observation_row"),
            sa.CheckConstraint("fact_revision > 0", name="ck_fact_observation_revision"),
        )
        op.create_index(
            "ix_fact_observation_logical_revision",
            _LINKS,
            ["logical_key", "fact_table", "fact_row_id", "fact_revision"],
        )
        op.create_index(
            "ix_fact_observation_source_document",
            _LINKS,
            ["source_document_id"],
        )
    if _OUTCOMES not in existing:
        op.create_table(
            _OUTCOMES,
            sa.Column(
                "resolution_id",
                sa.String(length=128),
                sa.ForeignKey("observation_resolution_revisions.resolution_id"),
                primary_key=True,
            ),
            sa.Column("resolution_status", sa.String(length=32), nullable=False),
            sa.Column("candidate_set_sha256", sa.String(length=64), nullable=False),
            sa.Column("checks_json", sa.Text(), nullable=False),
            sa.Column("recorded_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "resolution_status IN ('resolved', 'unresolved_material')",
                name="ck_fact_resolution_status",
            ),
            sa.CheckConstraint(
                "length(candidate_set_sha256) = 64",
                name="ck_fact_resolution_candidate_digest",
            ),
        )
    _append_only_triggers(_LINKS)
    _append_only_triggers(_OUTCOMES)
    for table in _FACT_TABLES:
        if table not in existing:
            continue
        _capture_triggers(table)
        _resolved_view(table)


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for view in (
        "v_financial_facts_resolved_current",
        "v_kpi_facts_resolved_current",
    ):
        op.execute(f"DROP VIEW IF EXISTS {view}")
    for table in _FACT_TABLES:
        for suffix in ("insert", "update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_observation_{suffix}")
    for table in (_OUTCOMES, _LINKS):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
    if _OUTCOMES in existing:
        op.drop_table(_OUTCOMES)
    if _LINKS in existing:
        op.drop_index("ix_fact_observation_source_document", table_name=_LINKS)
        op.drop_index("ix_fact_observation_logical_revision", table_name=_LINKS)
        op.drop_table(_LINKS)
