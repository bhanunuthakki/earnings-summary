"""Build an additive live cutover candidate without replacing either authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from sqlite3 import Connection
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

# Bumped 0259 → 0260 (2026-07-31, pre_earnings_brief plumbing): 0260 adds one
# COLUMN (ticker_settings.auto_pre_earnings_brief) and one llm_budgets ROW —
# no tables — so the exhaustive 0259 table registry below remains exact and
# the count is unchanged.
_AUTHORITY_SCHEMA_REVISION = "0260_pre_earnings_brief_plumbing"
_AUTHORITY_SCHEMA_TABLE_COUNT = 309
GOVERNED_TABLES_0259: frozenset[str] = frozenset(
    [
        "alembic_version",
        "ask_retrieval_scope_promotions",
        "ask_retrieval_trace_hits",
        "ask_retrieval_trace_items",
        "ask_retrieval_traces",
        "canonical_fact_candidate_dispositions",
        "canonical_fact_candidate_universe_revisions",
        "canonical_fact_candidate_universe_seals",
        "canonical_fact_projection_audit_receipts",
        "canonical_fact_projection_batches",
        "canonical_fact_projection_buckets",
        "canonical_fact_projection_entries",
        "canonical_fact_projection_generations",
        "canonical_fact_projection_scope_bindings",
        "canonical_fact_projection_seals",
        "canonical_fact_relation_assertions",
        "canonical_fact_relation_set_revisions",
        "canonical_fact_relation_set_seals",
        "canonical_fact_resolution_revisions",
        "canonical_fact_resolution_snapshot_members",
        "canonical_fact_resolution_snapshot_scope_headers",
        "canonical_fact_resolution_snapshot_scope_members",
        "canonical_fact_resolution_snapshot_scope_seals",
        "canonical_fact_resolution_snapshot_seals",
        "canonical_fact_resolution_snapshot_watermarks",
        "document_processing_disposition_headers",
        "document_processing_disposition_members",
        "document_processing_disposition_seals",
        "document_processing_evidence_headers",
        "document_processing_evidence_members",
        "document_processing_evidence_seals",
        "document_processing_obligation_revisions",
        "document_processing_snapshot_headers",
        "document_processing_snapshot_members",
        "document_processing_snapshot_seals",
        "document_semantic_disposition_revisions",
        "evidence_blob_location_observations",
        "evidence_content_blobs",
        "evidence_document_observation_links",
        "evidence_document_versions",
        "evidence_extraction_runs",
        "evidence_nodes",
        "evidence_source_observations",
        "expected_document_lifecycle_revisions",
        "expected_document_obligation_bindings",
        "expected_documents",
        "fact_cell_canonical_binding_revisions",
        "fact_cell_identity_seals_v2",
        "fact_cells_v2",
        "fact_derivation_basis_commitments_v2",
        "fact_derivation_input_edges_v2",
        "fact_derivation_seals_v2",
        "fact_dimensions_normalized_v2",
        "fact_extraction_run_completeness_seals_v2",
        "fact_observation_match_proofs",
        "fact_observation_payload_commitments_v2",
        "fact_observation_relations_v2",
        "fact_observation_revisions",
        "fact_observations_v2",
        "fact_overrides",
        "fact_reported_observation_anchors_v2",
        "fact_resolution_candidates_v2",
        "fact_resolution_outcomes",
        "fact_resolution_revisions_v2",
        "fact_selection_decisions",
        "filing_xbrl_extraction_disposition_seals",
        "filing_xbrl_extraction_dispositions",
        "filing_xbrl_extraction_input_members",
        "filing_xbrl_extraction_input_seals",
        "filing_xbrl_footnote_commitments",
        "filing_xbrl_processor_artifacts",
        "filing_xbrl_raw_fact_commitments",
        "heterogeneous_retrieval_trace_candidates",
        "heterogeneous_retrieval_trace_headers",
        "heterogeneous_retrieval_trace_results",
        "heterogeneous_retrieval_trace_seals",
        "image_ocr_assessments",
        "image_ocr_extraction_governance",
        "image_ocr_results",
        "issuer_authority_surface_revisions",
        "issuer_entities",
        "issuer_identifier_assertions",
        "issuer_identifier_resolution_outcomes",
        "issuer_profile_revisions",
        "issuer_reporting_scope_revisions",
        "legacy_document_evidence_binding_revisions",
        "legacy_fact_evidence_match_revisions",
        "legacy_issuer_binding_revisions",
        "metric_computation_attempts",
        "metric_mapping_revisions",
        "observation_resolution_candidates",
        "observation_resolution_revisions",
        "ocr_document_assessments",
        "ocr_extraction_governance",
        "ocr_page_results",
        "ocr_preflight_pages",
        "ontology_snapshot_headers",
        "ontology_snapshot_members",
        "ontology_snapshot_seals",
        "pdf_table_extraction_artifact_headers",
        "pdf_table_extraction_artifact_members",
        "pdf_table_extraction_artifact_seals",
        "population_cutover_audit_receipts",
        "population_cutover_receipts",
        "population_parity_receipts",
        "population_plane_receipts",
        "population_run_headers",
        "recorded_subject_binding_revisions",
        "reported_observations",
        "reporting_entities",
        "reporting_entity_identifier_assertions",
        "reporting_entity_identifier_resolution_outcomes",
        "research_snapshot_admission_receipts",
        "research_snapshot_headers",
        "research_snapshot_members",
        "research_snapshot_seals",
        "research_snapshot_universe_commitments",
        "search_chunks",
        "search_corpus_document_memberships",
        "search_corpus_manifest_seals",
        "search_corpus_manifests",
        "search_embedding_artifacts",
        "search_embedding_evaluation_receipts",
        "search_embedding_model_promotions",
        "search_embedding_runtime_registrations",
        "search_fact_projection_memberships",
        "search_fact_projection_rows",
        "search_fact_projection_runs",
        "search_fact_projection_seals",
        "search_index_memberships",
        "search_index_runs",
        "search_lexical_chunks",
        "search_lexical_chunks_config",
        "search_lexical_chunks_content",
        "search_lexical_chunks_data",
        "search_lexical_chunks_docsize",
        "search_lexical_chunks_idx",
        "search_manifest_source_inventories",
        "search_projection_seals",
        "securities",
        "security_identifier_assertions",
        "security_identifier_resolution_outcomes",
        "security_listing_assertions",
        "security_listing_resolution_outcomes",
        "security_reporting_entity_revisions",
        "source_coverage_assessments",
        "source_dimension_mapping_revisions",
        "source_fact_publication_members",
        "source_fact_publication_seals",
        "source_fact_publication_stream",
        "source_fact_publication_stream_clock",
        "source_fact_publications",
        "source_inventory_components",
        "source_inventory_snapshot_seals",
        "source_inventory_snapshots",
        "source_obligation_revisions",
        "source_observation_taxonomy_assertions",
        "source_taxonomy_components",
    ]
)
OPERATIONAL_TABLES_0259: frozenset[str] = frozenset(
    [
        "advisor_memos",
        "alerts",
        "analyst_notes",
        "analyst_notes_fts",
        "analyst_notes_fts_config",
        "analyst_notes_fts_data",
        "analyst_notes_fts_docsize",
        "analyst_notes_fts_idx",
        "ask_answer_audit_citations",
        "ask_answer_audit_claim_citations",
        "ask_answer_audit_claims",
        "ask_answer_audit_records",
        "ask_answer_audit_retrievals",
        "ask_answer_audit_seals",
        "ask_answer_groundings",
        "ask_sessions",
        "ask_turns",
        "brief_provenance_log",
        "business_factor_exposures",
        "candidate_models",
        "canonical_axes",
        "canonical_members",
        "canonical_metric_cell_dimensions",
        "canonical_metric_cell_seals",
        "canonical_metric_cells",
        "canonical_metric_definition_revisions",
        "canonical_metrics",
        "capital_actions",
        "capture_audit_log",
        "coach_mutes",
        "coach_pings",
        "commitment_scan_log",
        "comp_set_metrics_daily",
        "comparable_set_members",
        "comparable_sets",
        "concept_aliases",
        "concept_definitions",
        "concepts",
        "confidence_observations",
        "critical_accounting_estimates",
        "customer_concentrations",
        "dcf_run_inputs",
        "dcf_runs",
        "decision_drafts",
        "decision_nudges",
        "decisions",
        "disclosure_events",
        "discovery_candidates",
        "discovery_signals",
        "discovery_sources",
        "documents",
        "earnings_surprises",
        "entities",
        "entity_aliases",
        "entity_mentions",
        "entity_relationships",
        "etf_holdings",
        "etf_profile",
        "eval_case_features",
        "eval_case_results",
        "eval_runs",
        "exec_comp_packages",
        "exec_holdings",
        "expected_earnings",
        "extractions",
        "filing_section_coverage",
        "filing_section_items",
        "filing_sections",
        "financial_facts",
        "fmp_endpoint_status",
        "footnote_facts",
        "formula_definitions",
        "forward_looking_statements",
        "fx_rates",
        "global_dcf_assumptions",
        "ingestion_runs",
        "insider_transactions",
        "insight_notes",
        "insights",
        "investor_calibration",
        "ir_fetch_status",
        "kpi_aliases",
        "kpi_definitions",
        "kpi_facts",
        "lease_commitments",
        "litigation_matters",
        "llm_artifacts",
        "llm_budget_alerts",
        "llm_budgets",
        "llm_calls",
        "macro_sensitivities",
        "macro_series",
        "management_commitments",
        "mapping_proposals",
        "model_eval_verdicts",
        "model_pin_overrides",
        "news",
        "numerical_claims",
        "optimizer_nominations",
        "owner_profile_facts",
        "panel_activation_counts",
        "pending_telegram_replies",
        "pipeline_attempts",
        "pipeline_runs",
        "pipeline_stage_transitions",
        "portfolio_risk_snapshot_history",
        "portfolio_risk_snapshots",
        "position_entries",
        "position_sizing_intent",
        "positioning_intents",
        "predictions",
        "prompt_ab_verdicts",
        "prompt_arms",
        "prompt_calibration_scores",
        "prompt_experiments",
        "prompt_pin_overrides",
        "quarterly_artifacts",
        "query_criteria",
        "queued_actions",
        "raw_capture_sessions",
        "red_team_items",
        "research_hot_flags",
        "research_proposals",
        "research_tasks",
        "risk_factors",
        "saved_views",
        "saydo_historical_metrics",
        "segment_aliases",
        "segment_dimensions",
        "segment_periods",
        "segment_quarterly_coverage",
        "signals",
        "source_calls",
        "stage_transitions",
        "stance_scores",
        "standup_messages",
        "strategic_targets",
        "tenants",
        "thesis_evaluations",
        "thesis_ledger_entries",
        "thesis_state",
        "ticker_settings",
        "timeseries_signals",
        "tracked_companies",
        "transcript_segments",
        "transcripts",
        "user_kpi_registry",
        "validation_issues",
        "wealth_context_snapshot_history",
        "weekly_packet_items",
        "weekly_packet_runs",
    ]
)
AUTHORITY_TABLES_0259: frozenset[str] = GOVERNED_TABLES_0259 | OPERATIONAL_TABLES_0259
if (
    GOVERNED_TABLES_0259 & OPERATIONAL_TABLES_0259
    or len(AUTHORITY_TABLES_0259) != _AUTHORITY_SCHEMA_TABLE_COUNT
):
    raise RuntimeError("invalid exhaustive 0259 cutover authority registry")


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...

    def hexdigest(self) -> str: ...


class LiveCutoverMergeError(RuntimeError):
    """The candidate cannot be planned or applied without weakening authority."""


class TableColumn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    type: str
    notnull: int
    default: object
    pk: int


class MergeTablePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    table: str
    strategy: str
    primary_key: tuple[str, ...]
    live_row_count: int = Field(ge=0)
    governed_row_count: int = Field(ge=0)
    added_row_count: int = Field(ge=0)
    changed_row_count: int = Field(ge=0)
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    live_rows_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    governed_rows_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_delta_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LiveCutoverMergePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    policy_name: str
    policy_version: str
    live_database: str
    governed_database: str
    live_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    governed_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    alembic_revision: str
    tables: tuple[MergeTablePlan, ...]
    governed_table_count: int = Field(ge=0)
    operational_table_count: int = Field(ge=0)
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AppliedMergeTable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    table: str
    changed_rows: int = Field(ge=0)
    destination_row_count: int = Field(ge=0)
    live_rows_not_preserved: int = Field(ge=0)


class LiveCutoverMergeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    plan: LiveCutoverMergePlan
    destination_database: str
    applied_tables: tuple[AppliedMergeTable, ...]
    quick_check: str
    foreign_key_violations: int = Field(ge=0)
    destination_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def plan_live_cutover_merge(
    live_database: Path,
    governed_database: Path,
) -> LiveCutoverMergePlan:
    """Plan exact live-authoritative deltas over one governed substrate."""
    live_path = live_database.resolve()
    governed_path = governed_database.resolve()
    if live_path == governed_path:
        raise LiveCutoverMergeError("live and governed databases must be distinct")
    live_source_before = _source_snapshot_sha256(live_path)
    governed_source_before = _source_snapshot_sha256(governed_path)
    live = connect_sqlite(live_path, role=SQLiteConnectionRole.READ_ONLY)
    governed = connect_sqlite(governed_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        _require_healthy("live", live)
        _require_healthy("governed", governed)
        revision = _require_same_revision(live, governed)
        live_tables = _require_source_schema_contract(live, governed, revision=revision)
        plans: list[MergeTablePlan] = []
        governed_count = 0
        operational_count = 0
        for table in sorted(live_tables):
            if table in GOVERNED_TABLES_0259:
                governed_count += 1
                continue
            operational_count += 1
            live_count = _row_count(live, table)
            if live_count == 0:
                continue
            live_schema = _table_schema(live, table)
            governed_schema = _table_schema(governed, table)
            if live_schema != governed_schema:
                raise LiveCutoverMergeError(f"schema mismatch for operational table {table}")
            primary_key = tuple(
                column.name
                for column in sorted(live_schema, key=lambda column: column.pk)
                if column.pk
            )
            added_count, changed_count, selected_delta_sha256 = _delta_manifest(
                live,
                governed,
                table=table,
                schema=live_schema,
                primary_key=primary_key,
            )
            if added_count == 0 and changed_count == 0:
                continue
            plans.append(
                MergeTablePlan(
                    table=table,
                    strategy="upsert_live" if primary_key else "append_exact",
                    primary_key=primary_key,
                    live_row_count=live_count,
                    governed_row_count=_row_count(governed, table),
                    added_row_count=added_count,
                    changed_row_count=changed_count,
                    schema_sha256=_canonical_sha(
                        [column.model_dump(mode="json") for column in live_schema]
                    ),
                    live_rows_sha256=_table_rows_sha256(
                        live,
                        table=table,
                        schema=live_schema,
                        primary_key=primary_key,
                    ),
                    governed_rows_sha256=_table_rows_sha256(
                        governed,
                        table=table,
                        schema=governed_schema,
                        primary_key=primary_key,
                    ),
                    selected_delta_sha256=selected_delta_sha256,
                )
            )
        live_source_after = _source_snapshot_sha256(live_path)
        governed_source_after = _source_snapshot_sha256(governed_path)
        if live_source_before != live_source_after:
            raise LiveCutoverMergeError("live source changed while planning")
        if governed_source_before != governed_source_after:
            raise LiveCutoverMergeError("governed source changed while planning")
        commitment_payload = {
            "policy_name": "additive_live_operational_authority_merge",
            "policy_version": "3",
            "live_database": str(live_path),
            "governed_database": str(governed_path),
            "live_source_sha256": live_source_before,
            "governed_source_sha256": governed_source_before,
            "alembic_revision": revision,
            "tables": [plan.model_dump(mode="json") for plan in plans],
            "governed_table_count": governed_count,
            "operational_table_count": operational_count,
        }
        return LiveCutoverMergePlan(
            policy_name="additive_live_operational_authority_merge",
            policy_version="3",
            live_database=str(live_path),
            governed_database=str(governed_path),
            live_source_sha256=live_source_before,
            governed_source_sha256=governed_source_before,
            alembic_revision=revision,
            tables=tuple(plans),
            governed_table_count=governed_count,
            operational_table_count=operational_count,
            plan_sha256=_canonical_sha(commitment_payload),
        )
    finally:
        governed.close()
        live.close()


def apply_live_cutover_merge(
    live_database: Path,
    governed_database: Path,
    destination_database: Path,
    *,
    expected_plan_sha256: str,
) -> LiveCutoverMergeReceipt:
    """Copy the governed DB and atomically merge the committed live deltas."""
    destination = destination_database.resolve()
    source_paths = {live_database.resolve(), governed_database.resolve()}
    if destination in source_paths:
        raise LiveCutoverMergeError("destination must not replace either source database")
    if destination.exists():
        raise LiveCutoverMergeError("destination already exists")
    plan = plan_live_cutover_merge(live_database, governed_database)
    if plan.plan_sha256 != expected_plan_sha256:
        raise LiveCutoverMergeError(
            "plan commitment mismatch; rerun dry-run and review the new authority delta"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    _copy_database(governed_database.resolve(), destination)
    applied: list[AppliedMergeTable] = []
    connection = connect_sqlite(
        destination,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=True,
    )
    live_connection = connect_sqlite(
        live_database.resolve(),
        role=SQLiteConnectionRole.READ_ONLY,
    )
    try:
        with connection:
            for table_plan in plan.tables:
                _stage_live_table(connection, live_connection, table_plan)
                before = connection.total_changes
                _merge_table(connection, table_plan)
                changed_rows = connection.total_changes - before
                missing = _live_rows_not_preserved(connection, table_plan)
                connection.execute("DROP TABLE temp._live_authority_rows")
                if missing:
                    raise LiveCutoverMergeError(
                        f"{table_plan.table} failed live-authority preservation: {missing}"
                    )
                applied.append(
                    AppliedMergeTable(
                        table=table_plan.table,
                        changed_rows=changed_rows,
                        destination_row_count=_row_count(connection, table_plan.table),
                        live_rows_not_preserved=missing,
                    )
                )
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        if quick_check != "ok" or foreign_key_violations:
            raise LiveCutoverMergeError(
                "candidate integrity failed after additive merge: "
                f"quick_check={quick_check}, foreign_keys={foreign_key_violations}"
            )
    except Exception:
        live_connection.close()
        connection.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        live_connection.close()
        connection.close()
    return LiveCutoverMergeReceipt(
        plan=plan,
        destination_database=str(destination),
        applied_tables=tuple(applied),
        quick_check=quick_check,
        foreign_key_violations=foreign_key_violations,
        destination_sha256=_file_sha256(destination),
    )


def _copy_database(source_path: Path, destination_path: Path) -> None:
    source = connect_sqlite(source_path, role=SQLiteConnectionRole.READ_ONLY)
    destination = connect_sqlite(
        destination_path,
        role=SQLiteConnectionRole.SNAPSHOT_DESTINATION,
        schema_preflight=False,
    )
    try:
        source.backup(destination)
    except Exception:
        destination.close()
        source.close()
        destination_path.unlink(missing_ok=True)
        raise
    else:
        destination.close()
        source.close()


def _merge_table(connection: Connection, plan: MergeTablePlan) -> None:
    columns = tuple(
        row["name"] for row in connection.execute(f"PRAGMA table_info({_quote(plan.table)})")
    )
    column_sql = ", ".join(_quote(column) for column in columns)
    source_columns = ", ".join(f"src.{_quote(column)}" for column in columns)
    equality = _row_equality("dst", "src", columns)
    if plan.primary_key:
        pk_sql = ", ".join(_quote(column) for column in plan.primary_key)
        updates = ", ".join(
            f"{_quote(column)} = excluded.{_quote(column)}"
            for column in columns
            if column not in plan.primary_key
        )
        # The generated tokens contain quoted, schema-derived identifiers only.
        conflict_clause = (
            f"DO UPDATE SET {updates}" if updates else "DO NOTHING"  # nosec B608
        )
        connection.execute(
            f"INSERT INTO main.{_quote(plan.table)} ({column_sql}) "  # nosec B608
            f"SELECT {source_columns} FROM temp._live_authority_rows AS src "
            f"WHERE NOT EXISTS ("
            f"SELECT 1 FROM main.{_quote(plan.table)} AS dst WHERE {equality}"
            f") ON CONFLICT ({pk_sql}) {conflict_clause}"
        )
        return
    connection.execute(
        f"INSERT INTO main.{_quote(plan.table)} ({column_sql}) "  # nosec B608
        f"SELECT {source_columns} FROM temp._live_authority_rows AS src "
        f"WHERE NOT EXISTS ("
        f"SELECT 1 FROM main.{_quote(plan.table)} AS dst WHERE {equality}"
        f")"
    )


def _live_rows_not_preserved(connection: Connection, plan: MergeTablePlan) -> int:
    columns = tuple(
        row["name"] for row in connection.execute(f"PRAGMA table_info({_quote(plan.table)})")
    )
    equality = _row_equality("dst", "src", columns)
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM temp._live_authority_rows AS src "  # nosec B608
            f"WHERE NOT EXISTS ("
            f"SELECT 1 FROM main.{_quote(plan.table)} AS dst WHERE {equality}"
            f")"
        ).fetchone()[0]
    )


def _stage_live_table(
    destination: Connection,
    live: Connection,
    plan: MergeTablePlan,
) -> None:
    destination.execute("DROP TABLE IF EXISTS temp._live_authority_rows")
    destination.execute(
        f"CREATE TEMP TABLE _live_authority_rows AS "  # nosec B608
        f"SELECT * FROM main.{_quote(plan.table)} WHERE 0"
    )
    columns = tuple(
        str(row["name"]) for row in live.execute(f"PRAGMA table_info({_quote(plan.table)})")
    )
    placeholders = ", ".join("?" for _ in columns)
    # The placeholder count comes only from the inspected schema.
    insert_sql = f"INSERT INTO temp._live_authority_rows VALUES ({placeholders})"  # nosec B608
    # The table identifier is schema-derived and double-quoted.
    cursor = live.execute(
        f"SELECT * FROM {_quote(plan.table)}"  # nosec B608
    )
    while rows := cursor.fetchmany(1_000):
        destination.executemany(insert_sql, (tuple(row) for row in rows))


def _delta_manifest(
    live: Connection,
    governed: Connection,
    *,
    table: str,
    schema: tuple[TableColumn, ...],
    primary_key: tuple[str, ...],
) -> tuple[int, int, str]:
    columns = tuple(column.name for column in schema)
    governed.execute(
        "ATTACH DATABASE ? AS live_delta",
        (Path(_database_path(live)).resolve().as_uri() + "?mode=ro",),
    )
    try:
        selected_columns = ", ".join(f"src.{_quote(column)}" for column in columns)
        governed.create_function(
            "_cutover_value_key",
            1,
            _value_sort_key,
            deterministic=True,
        )
        order_sql = _deterministic_order_sql("src", columns)
        digest = _new_rows_digest(
            scope="selected_live_delta",
            table=table,
            schema=schema,
            primary_key=primary_key,
        )
        added = 0
        changed = 0
        if primary_key:
            key_match = _row_equality("dst", "src", primary_key)
            row_match = _row_equality("dst", "src", columns)
            cursor = governed.execute(
                f"SELECT {selected_columns}, "  # nosec B608
                f"CASE WHEN EXISTS ("
                f"SELECT 1 FROM main.{_quote(table)} AS dst WHERE {key_match}"
                f") THEN 1 ELSE 0 END AS _is_changed "
                f"FROM live_delta.{_quote(table)} AS src "
                f"WHERE NOT EXISTS ("
                f"SELECT 1 FROM main.{_quote(table)} AS dst WHERE {row_match}"
                f") ORDER BY {order_sql}"
            )
            while rows := cursor.fetchmany(1_000):
                for row in rows:
                    values = tuple(row[:-1])
                    is_changed = bool(row[-1])
                    changed += int(is_changed)
                    added += int(not is_changed)
                    _update_rows_digest(
                        digest,
                        ("changed" if is_changed else "added", *values),
                    )
            return added, changed, digest.hexdigest()
        row_match = _row_equality("dst", "src", columns)
        cursor = governed.execute(
            f"SELECT {selected_columns} FROM live_delta.{_quote(table)} AS src "  # nosec B608
            f"WHERE NOT EXISTS ("
            f"SELECT 1 FROM main.{_quote(table)} AS dst WHERE {row_match}"
            f") ORDER BY {order_sql}"
        )
        while rows := cursor.fetchmany(1_000):
            for row in rows:
                added += 1
                _update_rows_digest(digest, ("added", *tuple(row)))
        return added, 0, digest.hexdigest()
    finally:
        governed.execute("DETACH DATABASE live_delta")


def _table_rows_sha256(
    connection: Connection,
    *,
    table: str,
    schema: tuple[TableColumn, ...],
    primary_key: tuple[str, ...],
) -> str:
    columns = tuple(column.name for column in schema)
    selected_columns = ", ".join(_quote(column) for column in columns)
    connection.create_function(
        "_cutover_value_key",
        1,
        _value_sort_key,
        deterministic=True,
    )
    order_sql = _deterministic_order_sql(None, columns)
    digest = _new_rows_digest(
        scope="complete_table",
        table=table,
        schema=schema,
        primary_key=primary_key,
    )
    cursor = connection.execute(
        f"SELECT {selected_columns} FROM {_quote(table)} ORDER BY {order_sql}"  # nosec B608
    )
    while rows := cursor.fetchmany(1_000):
        for row in rows:
            _update_rows_digest(digest, tuple(row))
    return digest.hexdigest()


def _new_rows_digest(
    *,
    scope: str,
    table: str,
    schema: tuple[TableColumn, ...],
    primary_key: tuple[str, ...],
) -> _Digest:
    digest = hashlib.sha256()
    header = {
        "format": "sqlite_ordered_rows_v1",
        "scope": scope,
        "table": table,
        "schema": [column.model_dump(mode="json") for column in schema],
        "primary_key": primary_key,
    }
    encoded = json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    return digest


def _update_rows_digest(digest: _Digest, row: tuple[object, ...]) -> None:
    digest.update(len(row).to_bytes(8, "big"))
    for value in row:
        tag, encoded = _encoded_sqlite_value(value)
        digest.update(len(tag).to_bytes(2, "big"))
        digest.update(tag)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)


def _encoded_sqlite_value(value: object) -> tuple[bytes, bytes]:
    if value is None:
        return b"null", b""
    if isinstance(value, bytes):
        return b"blob", value
    if isinstance(value, int):
        return b"integer", str(value).encode("ascii")
    if isinstance(value, float):
        return b"real", value.hex().encode("ascii")
    if isinstance(value, str):
        return b"text", value.encode("utf-8")
    raise LiveCutoverMergeError(
        f"unsupported SQLite value type in content commitment: {type(value).__name__}"
    )


def _value_sort_key(value: object) -> bytes:
    tag, encoded = _encoded_sqlite_value(value)
    return len(tag).to_bytes(2, "big") + tag + len(encoded).to_bytes(8, "big") + encoded


def _deterministic_order_sql(alias: str | None, columns: tuple[str, ...]) -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"_cutover_value_key({prefix}{_quote(column)})" for column in columns)


def _database_path(connection: Connection) -> str:
    rows = connection.execute("PRAGMA database_list").fetchall()
    return str(next(row[2] for row in rows if row[1] == "main"))


def _require_healthy(label: str, connection: Connection) -> None:
    quick = connection.execute("PRAGMA quick_check").fetchone()[0]
    if quick != "ok":
        raise LiveCutoverMergeError(f"{label} quick_check failed: {quick}")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise LiveCutoverMergeError(f"{label} has {len(violations)} foreign-key violation(s)")


def _require_same_revision(live: Connection, governed: Connection) -> str:
    live_revision = _revision(live)
    governed_revision = _revision(governed)
    if live_revision != governed_revision:
        raise LiveCutoverMergeError(
            f"Alembic revision mismatch: live={live_revision}, governed={governed_revision}"
        )
    if live_revision != _AUTHORITY_SCHEMA_REVISION:
        raise LiveCutoverMergeError(
            "authority registry revision mismatch: "
            f"expected={_AUTHORITY_SCHEMA_REVISION}, actual={live_revision}"
        )
    return live_revision


def _require_source_schema_contract(
    live: Connection,
    governed: Connection,
    *,
    revision: str,
) -> set[str]:
    if revision != _AUTHORITY_SCHEMA_REVISION:
        raise LiveCutoverMergeError(f"no authority registry exists for Alembic revision {revision}")
    live_tables = _table_names(live)
    governed_tables = _table_names(governed)
    unclassified = (live_tables | governed_tables) - AUTHORITY_TABLES_0259
    if unclassified:
        raise LiveCutoverMergeError(
            "unclassified table(s) outside the closed 0259 authority registry: "
            + ", ".join(sorted(unclassified))
        )
    if live_tables != governed_tables:
        live_only = sorted(live_tables - governed_tables)
        governed_only = sorted(governed_tables - live_tables)
        raise LiveCutoverMergeError(
            "source table-set mismatch: "
            f"live_only={live_only[:10]}, governed_only={governed_only[:10]}"
        )
    if live_tables != set(AUTHORITY_TABLES_0259):
        missing = sorted(AUTHORITY_TABLES_0259 - live_tables)
        raise LiveCutoverMergeError(
            "source table-set does not match the exhaustive 0259 authority registry: "
            f"missing={missing[:10]}"
        )
    for table in sorted(AUTHORITY_TABLES_0259):
        if _table_schema(live, table) != _table_schema(governed, table):
            raise LiveCutoverMergeError(f"source schema mismatch for table {table}")
    return live_tables


def _revision(connection: Connection) -> str:
    rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    if len(rows) != 1:
        raise LiveCutoverMergeError("database must have exactly one Alembic revision")
    return str(rows[0][0])


def _table_names(connection: Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _table_schema(connection: Connection, table: str) -> tuple[TableColumn, ...]:
    return tuple(
        TableColumn(
            name=str(row["name"]),
            type=str(row["type"]),
            notnull=int(row["notnull"]),
            default=row["dflt_value"],
            pk=int(row["pk"]),
        )
        for row in connection.execute(f"PRAGMA table_info({_quote(table)})")
    )


def _row_count(connection: Connection, table: str) -> int:
    # The table identifier is schema-derived and double-quoted.
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {_quote(table)}"  # nosec B608
        ).fetchone()[0]
    )


def _row_equality(left: str, right: str, columns: Iterable[str]) -> str:
    return " AND ".join(
        f"{left}.{_quote(column)} IS {right}.{_quote(column)}" for column in columns
    )


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _canonical_sha(value: object) -> str:
    payload = json.dumps(
        value,
        default=str,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _source_snapshot_sha256(path: Path) -> str:
    """Bind the immutable SQLite main file and any committed WAL bytes."""
    digest = hashlib.sha256()
    digest.update(b"sqlite_source_snapshot_v1")
    for label, component in (
        ("main", path),
        ("wal", Path(f"{path}-wal")),
    ):
        encoded_label = label.encode("ascii")
        digest.update(len(encoded_label).to_bytes(2, "big"))
        digest.update(encoded_label)
        if not component.exists():
            digest.update(b"\x00")
            continue
        digest.update(b"\x01")
        stat_before = component.stat()
        digest.update(stat_before.st_size.to_bytes(8, "big"))
        with component.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        stat_after = component.stat()
        if (
            stat_before.st_size != stat_after.st_size
            or stat_before.st_mtime_ns != stat_after.st_mtime_ns
        ):
            raise LiveCutoverMergeError(f"source snapshot changed while hashing: {component}")
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
