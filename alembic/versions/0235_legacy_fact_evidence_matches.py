"""Add revisioned proof records for legacy-fact to evidence matches.

Revision ID: 0235_legacy_fact_evidence_matches
Revises: 0234_image_ocr_governance

This migration records matcher decisions only.  It deliberately does not add
or alter fact-observation write guards; writer ordering remains unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0235_legacy_fact_evidence_matches"
down_revision: str | Sequence[str] | None = "0234_image_ocr_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "legacy_fact_evidence_match_revisions"
_CURRENT_VIEW = "v_legacy_fact_evidence_matches_current"
_ACCEPTED_VIEW = "v_legacy_fact_evidence_matches_accepted_current"
_FACT_TABLES = ("financial_facts", "kpi_facts")
_CHECK_COLUMNS = (
    "issuer_check",
    "context_check",
    "unit_check",
    "sign_check",
    "fiscal_period_check",
    "value_check",
)


def _hex_check(column: str, *, nullable: bool = False) -> str:
    required = (
        f"length({column}) = 64 "
        f"AND {column} NOT GLOB '*[^0-9a-f]*'"
    )
    return f"({column} IS NULL OR ({required}))" if nullable else f"({required})"


def _append_only() -> None:
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_append_only BEFORE UPDATE ON {_TABLE} "
        "BEGIN SELECT RAISE(ABORT, 'legacy fact evidence match is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_append_only_delete BEFORE DELETE ON {_TABLE} "
        "BEGIN SELECT RAISE(ABORT, 'legacy fact evidence match is append-only'); END"
    )


def _payload_match(fact_table: str) -> str:
    common = (
        "json_extract(NEW.fact_payload_json, '$.fact_row_id') = fact.id "
        f"AND json_extract(NEW.fact_payload_json, '$.fact_table') = '{fact_table}' "
        "AND json_extract(NEW.fact_payload_json, '$.ticker') IS fact.ticker "
        "AND json_extract(NEW.fact_payload_json, '$.period_end') IS CAST(fact.period_end AS TEXT) "
        "AND json_extract(NEW.fact_payload_json, '$.fiscal_period_type') "
        "IS fact.fiscal_period_type "
        "AND json_extract(NEW.fact_payload_json, '$.value') IS CAST(fact.value AS TEXT) "
        "AND json_extract(NEW.fact_payload_json, '$.unit') IS fact.unit "
        "AND json_extract(NEW.fact_payload_json, '$.source_doc_id') = fact.source_doc_id "
        "AND json_extract(NEW.fact_payload_json, '$.extracted_by') IS fact.extracted_by "
        "AND ((fact.locator IS NULL "
        "AND json_type(NEW.fact_payload_json, '$.locator') = 'null') "
        "OR (fact.locator IS NOT NULL "
        "AND json(json_extract(NEW.fact_payload_json, '$.locator')) = json(fact.locator))) "
    )
    if fact_table == "financial_facts":
        return (
            common
            + "AND json_extract(NEW.fact_payload_json, '$.schema_version') "
            "= 'financial_fact_payload.v1' "
            "AND json_extract(NEW.fact_payload_json, '$.line_item') IS fact.line_item "
            "AND json_extract(NEW.fact_payload_json, '$.currency') IS fact.currency"
        )
    return (
        common
        + "AND json_extract(NEW.fact_payload_json, '$.schema_version') "
        "= 'kpi_fact_payload.v1' "
        "AND json_extract(NEW.fact_payload_json, '$.kpi_definition_id') "
        "= fact.kpi_definition_id "
        "AND json_extract(NEW.fact_payload_json, '$.source_excerpt') "
        "IS fact.source_excerpt "
        "AND json_extract(NEW.fact_payload_json, '$.computed_from') "
        "IS fact.computed_from "
        "AND json_extract(NEW.fact_payload_json, '$.formula_id') IS fact.formula_id "
        "AND json_extract(NEW.fact_payload_json, '$.formula_version') "
        "IS fact.formula_version"
    )


def _fact_scope_trigger(fact_table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_{fact_table}_scope "
        f"BEFORE INSERT ON {_TABLE} WHEN NEW.fact_table = '{fact_table}' "
        "AND NOT EXISTS ("
        f"SELECT 1 FROM {fact_table} AS fact "
        "JOIN legacy_document_evidence_binding_revisions AS binding "
        "ON binding.legacy_document_id = fact.source_doc_id "
        "WHERE fact.id = NEW.fact_row_id "
        "AND binding.binding_revision_id = NEW.legacy_binding_revision_id "
        "AND binding.revision = NEW.legacy_binding_revision "
        "AND binding.scope_content_sha256 = NEW.binding_scope_content_sha256 "
        "AND binding.evidence_node_id = NEW.evidence_node_id "
        f"AND {_payload_match(fact_table)}) "
        "BEGIN SELECT RAISE(ABORT, "
        "'legacy fact match requires exact fact payload and source binding'); END"
    )


def _accepted_fact_guards(fact_table: str) -> None:
    semantic_columns = (
        "ticker, period_end, fiscal_period_type, line_item, value, currency, unit, "
        "source_doc_id, extracted_by, locator"
        if fact_table == "financial_facts"
        else "ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, "
        "source_doc_id, extracted_by, locator, source_excerpt, computed_from, "
        "formula_id, formula_version"
    )
    for operation, clause in (
        (
            "update",
            f"BEFORE UPDATE OF {semantic_columns} ON {fact_table}",
        ),
        ("delete", f"BEFORE DELETE ON {fact_table}"),
    ):
        op.execute(
            f"CREATE TRIGGER trg_{_TABLE}_{fact_table}_accepted_{operation} "
            f"{clause} WHEN EXISTS (SELECT 1 FROM {_ACCEPTED_VIEW} AS match "
            f"WHERE match.fact_table = '{fact_table}' "
            "AND match.fact_row_id = OLD.id) "
            "BEGIN SELECT RAISE(ABORT, "
            "'accepted legacy fact evidence match freezes semantic fact fields'); END"
        )


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    legacy_fact_tables = tuple(table for table in _FACT_TABLES if table in existing)

    op.create_table(
        _TABLE,
        sa.Column("match_revision_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("fact_table", sa.String(32), nullable=False),
        sa.Column("fact_row_id", sa.Integer(), nullable=False),
        sa.Column("issuer_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("fact_payload_json", sa.Text(), nullable=False),
        sa.Column("fact_payload_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column("original_locator_json", sa.Text(), nullable=True),
        sa.Column("original_locator_sha256", sa.String(64), nullable=True),
        sa.Column("relocated_locator_json", sa.Text(), nullable=True),
        sa.Column("relocated_locator_sha256", sa.String(64), nullable=True),
        sa.Column(
            "legacy_binding_revision_id",
            sa.String(128),
            sa.ForeignKey(
                "legacy_document_evidence_binding_revisions.binding_revision_id"
            ),
            nullable=False,
        ),
        sa.Column("legacy_binding_revision", sa.Integer(), nullable=False),
        sa.Column("binding_scope_content_sha256", sa.String(64), nullable=False),
        sa.Column(
            "evidence_node_id",
            sa.String(128),
            sa.ForeignKey("evidence_nodes.node_id"),
            nullable=False,
        ),
        sa.Column("matched_entry_sha256", sa.String(64), nullable=True),
        sa.Column("candidate_manifest_json", sa.Text(), nullable=False),
        sa.Column("candidate_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("matched_candidate_count", sa.Integer(), nullable=False),
        *(sa.Column(column, sa.String(16), nullable=False) for column in _CHECK_COLUMNS),
        sa.Column("matcher_name", sa.String(128), nullable=False),
        sa.Column("matcher_version", sa.String(64), nullable=False),
        sa.Column("matcher_config_sha256", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason_details_json", sa.Text(), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column(
            "supersedes_match_revision_id",
            sa.String(128),
            sa.ForeignKey(f"{_TABLE}.match_revision_id"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["issuer_id"],
            ["issuer_entities.issuer_id"],
            name="fk_legacy_fact_evidence_match_issuer",
        ),
        sa.UniqueConstraint(
            "fact_table",
            "fact_row_id",
            "revision",
            name="uq_legacy_fact_evidence_match_revision",
        ),
        sa.CheckConstraint(
            "fact_table IN ('financial_facts', 'kpi_facts')",
            name="ck_legacy_fact_evidence_match_fact_table",
        ),
        sa.CheckConstraint(
            "fact_row_id > 0 AND revision > 0 AND legacy_binding_revision > 0",
            name="ck_legacy_fact_evidence_match_positive_ids",
        ),
        sa.CheckConstraint(
            "json_valid(fact_payload_json) AND json_type(fact_payload_json) = 'object' "
            "AND json_valid(reason_details_json) "
            "AND json_type(reason_details_json) = 'object'",
            name="ck_legacy_fact_evidence_match_json",
        ),
        sa.CheckConstraint(
            "("
            "fact_table = 'financial_facts' "
            "AND json_extract(fact_payload_json, '$.schema_version') "
            "= 'financial_fact_payload.v1' "
            "AND json_extract(fact_payload_json, '$.fact_table') = fact_table "
            "AND json_type(fact_payload_json, '$.fact_row_id') = 'integer' "
            "AND json_type(fact_payload_json, '$.ticker') = 'text' "
            "AND json_type(fact_payload_json, '$.period_end') = 'text' "
            "AND json_type(fact_payload_json, '$.fiscal_period_type') = 'text' "
            "AND json_type(fact_payload_json, '$.line_item') = 'text' "
            "AND json_type(fact_payload_json, '$.value') = 'text' "
            "AND json_type(fact_payload_json, '$.unit') = 'text' "
            "AND json_type(fact_payload_json, '$.source_doc_id') = 'integer'"
            ") OR ("
            "fact_table = 'kpi_facts' "
            "AND json_extract(fact_payload_json, '$.schema_version') "
            "= 'kpi_fact_payload.v1' "
            "AND json_extract(fact_payload_json, '$.fact_table') = fact_table "
            "AND json_type(fact_payload_json, '$.fact_row_id') = 'integer' "
            "AND json_type(fact_payload_json, '$.ticker') = 'text' "
            "AND json_type(fact_payload_json, '$.period_end') = 'text' "
            "AND json_type(fact_payload_json, '$.fiscal_period_type') = 'text' "
            "AND json_type(fact_payload_json, '$.kpi_definition_id') = 'integer' "
            "AND json_type(fact_payload_json, '$.value') = 'text' "
            "AND json_type(fact_payload_json, '$.unit') = 'text' "
            "AND json_type(fact_payload_json, '$.source_doc_id') = 'integer'"
            ")",
            name="ck_legacy_fact_evidence_match_payload_v1",
        ),
        sa.CheckConstraint(
            "(original_locator_json IS NULL AND original_locator_sha256 IS NULL) OR "
            "(original_locator_json IS NOT NULL AND json_valid(original_locator_json) "
            "AND json_type(original_locator_json) = 'object' "
            "AND length(original_locator_sha256) = 64)",
            name="ck_legacy_fact_evidence_match_original_locator",
        ),
        sa.CheckConstraint(
            "(relocated_locator_json IS NULL AND relocated_locator_sha256 IS NULL) OR "
            "(relocated_locator_json IS NOT NULL AND json_valid(relocated_locator_json) "
            "AND json_type(relocated_locator_json) = 'object' "
            "AND json_type(relocated_locator_json, '$.accession_number') = 'text' "
            "AND json_type(relocated_locator_json, '$.namespace') = 'text' "
            "AND json_type(relocated_locator_json, '$.concept') = 'text' "
            "AND json_type(relocated_locator_json, '$.unit') = 'text' "
            "AND json_type(relocated_locator_json, '$.entry_index') = 'integer' "
            "AND json_extract(relocated_locator_json, '$.entry_index') >= 0 "
            "AND json_type(relocated_locator_json, '$.json_path') = 'text' "
            "AND json_extract(relocated_locator_json, '$.json_path') = "
            "'facts.' || json_extract(relocated_locator_json, '$.namespace') || "
            "'.' || json_extract(relocated_locator_json, '$.concept') || "
            "'.units.' || json_extract(relocated_locator_json, '$.unit') || "
            "'[' || json_extract(relocated_locator_json, '$.entry_index') || ']' "
            "AND length(relocated_locator_sha256) = 64)",
            name="ck_legacy_fact_evidence_match_relocated_locator",
        ),
        sa.CheckConstraint(
            " AND ".join(
                (
                    _hex_check("fact_payload_fingerprint_sha256"),
                    _hex_check("original_locator_sha256", nullable=True),
                    _hex_check("relocated_locator_sha256", nullable=True),
                    _hex_check("binding_scope_content_sha256"),
                    _hex_check("matched_entry_sha256", nullable=True),
                    _hex_check("candidate_manifest_sha256"),
                    _hex_check("matcher_config_sha256"),
                )
            ),
            name="ck_legacy_fact_evidence_match_hashes",
        ),
        sa.CheckConstraint(
            "json_valid(candidate_manifest_json) "
            "AND json_type(candidate_manifest_json) = 'object' "
            "AND json_extract(candidate_manifest_json, '$.schema_version') "
            "= 'companyfacts_candidate_manifest.v1' "
            "AND json_type(candidate_manifest_json, '$.candidates') = 'array' "
            "AND candidate_count = "
            "json_array_length(candidate_manifest_json, '$.candidates') "
            "AND candidate_count >= 0 "
            "AND matched_candidate_count >= 0 "
            "AND matched_candidate_count <= candidate_count",
            name="ck_legacy_fact_evidence_match_candidates",
        ),
        *(
            sa.CheckConstraint(
                f"{column} IN ('pass', 'fail', 'not_evaluated')",
                name=f"ck_legacy_fact_evidence_match_{column}",
            )
            for column in _CHECK_COLUMNS
        ),
        sa.CheckConstraint(
            "outcome IN ('accepted', 'retryable', 'terminal')",
            name="ck_legacy_fact_evidence_match_outcome",
        ),
        sa.CheckConstraint(
            "(outcome = 'accepted' "
            + "".join(f"AND {column} = 'pass' " for column in _CHECK_COLUMNS)
            + "AND relocated_locator_json IS NOT NULL "
            "AND relocated_locator_sha256 IS NOT NULL "
            "AND matched_entry_sha256 IS NOT NULL "
            "AND candidate_count > 0 "
            "AND matched_candidate_count = 1) "
            "OR (outcome IN ('retryable', 'terminal') "
            + "AND ("
            + " OR ".join(f"{column} <> 'pass'" for column in _CHECK_COLUMNS)
            + " OR matched_candidate_count <> 1))",
            name="ck_legacy_fact_evidence_match_acceptance",
        ),
        sa.CheckConstraint(
            "outcome <> 'accepted' OR fact_table <> 'kpi_facts' OR ("
            "json_extract(fact_payload_json, '$.computed_from') IS NULL "
            "AND json_extract(fact_payload_json, '$.formula_id') IS NULL "
            "AND json_extract(fact_payload_json, '$.formula_version') IS NULL "
            "AND LOWER(COALESCE("
            "json_extract(fact_payload_json, '$.extracted_by'), '')) "
            "NOT LIKE '%derived%')",
            name="ck_legacy_fact_evidence_match_reported_kpi",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_legacy_fact_evidence_match_clocks",
        ),
    )
    op.create_index(
        "ix_legacy_fact_evidence_match_binding",
        _TABLE,
        ["legacy_binding_revision_id", "evidence_node_id"],
    )
    op.create_index(
        "ix_legacy_fact_evidence_match_issuer_outcome",
        _TABLE,
        ["issuer_id", "outcome", "recorded_at"],
    )
    op.create_index(
        "ix_legacy_fact_evidence_match_retry",
        _TABLE,
        ["recorded_at", "fact_table", "fact_row_id"],
        sqlite_where=sa.text("outcome = 'retryable'"),
    )

    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_revision_first BEFORE INSERT ON {_TABLE} "
        "WHEN NEW.revision = 1 AND NEW.supersedes_match_revision_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'first legacy fact match cannot supersede'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_revision_parent BEFORE INSERT ON {_TABLE} "
        "WHEN NEW.revision > 1 AND (NEW.supersedes_match_revision_id IS NULL OR "
        f"NOT EXISTS (SELECT 1 FROM {_TABLE} AS prior "
        "WHERE prior.match_revision_id = NEW.supersedes_match_revision_id "
        "AND prior.fact_table = NEW.fact_table "
        "AND prior.fact_row_id = NEW.fact_row_id "
        "AND prior.revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'legacy fact match must supersede prior revision'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_binding_current BEFORE INSERT ON {_TABLE} "
        "WHEN NEW.outcome = 'accepted' AND NOT EXISTS ("
        "SELECT 1 FROM v_legacy_document_evidence_bindings_current AS binding "
        "WHERE binding.binding_revision_id = NEW.legacy_binding_revision_id "
        "AND binding.revision = NEW.legacy_binding_revision "
        "AND binding.scope_content_sha256 = NEW.binding_scope_content_sha256 "
        "AND binding.evidence_node_id = NEW.evidence_node_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'legacy fact match requires the current exact evidence binding'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_node_issuer BEFORE INSERT ON {_TABLE} "
        "WHEN NOT EXISTS ("
        "SELECT 1 FROM legacy_document_evidence_binding_revisions AS binding "
        "JOIN v_evidence_document_versions_canonical AS document "
        "ON document.document_version_id = binding.document_version_id "
        "JOIN evidence_nodes AS node ON node.node_id = binding.evidence_node_id "
        "WHERE binding.binding_revision_id = NEW.legacy_binding_revision_id "
        "AND binding.evidence_node_id = NEW.evidence_node_id "
        "AND document.issuer_id = NEW.issuer_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'legacy fact match binding, node, and issuer must agree'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_knowledge_clock BEFORE INSERT ON {_TABLE} "
        "WHEN NOT EXISTS ("
        "SELECT 1 FROM legacy_document_evidence_binding_revisions AS binding "
        "JOIN evidence_document_versions AS document "
        "ON document.document_version_id = binding.document_version_id "
        "JOIN evidence_source_observations AS source "
        "ON source.observation_id = document.observation_id "
        "WHERE binding.binding_revision_id = NEW.legacy_binding_revision_id "
        "AND NEW.knowledge_at >= binding.knowledge_at "
        "AND NEW.knowledge_at >= source.retrieved_at) "
        "BEGIN SELECT RAISE(ABORT, "
        "'legacy fact match knowledge predates binding or retrieval'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_accepted_candidate BEFORE INSERT ON {_TABLE} "
        "WHEN NEW.outcome = 'accepted' AND ("
        "SELECT COUNT(*) FROM json_each("
        "NEW.candidate_manifest_json, '$.candidates') AS candidate "
        "WHERE json_extract(candidate.value, '$.entry_sha256') "
        "= NEW.matched_entry_sha256 "
        "AND json(json_extract(candidate.value, '$.relocated_locator')) "
        "= json(NEW.relocated_locator_json)) <> 1 "
        "BEGIN SELECT RAISE(ABORT, "
        "'accepted legacy fact match requires one exact manifest candidate'); END"
    )
    for fact_table in legacy_fact_tables:
        _fact_scope_trigger(fact_table)
    _append_only()

    op.execute(
        f"CREATE VIEW {_CURRENT_VIEW} AS SELECT match.* FROM {_TABLE} AS match "
        f"WHERE NOT EXISTS (SELECT 1 FROM {_TABLE} AS newer "
        "WHERE newer.fact_table = match.fact_table "
        "AND newer.fact_row_id = match.fact_row_id "
        "AND newer.revision > match.revision)"
    )
    op.execute(
        f"CREATE VIEW {_ACCEPTED_VIEW} AS "
        f"SELECT match.* FROM {_CURRENT_VIEW} AS match "
        "JOIN v_legacy_document_evidence_bindings_current AS binding "
        "ON binding.binding_revision_id = match.legacy_binding_revision_id "
        "AND binding.revision = match.legacy_binding_revision "
        "AND binding.scope_content_sha256 = match.binding_scope_content_sha256 "
        "AND binding.evidence_node_id = match.evidence_node_id "
        "WHERE match.outcome = 'accepted'"
    )
    for fact_table in legacy_fact_tables:
        _accepted_fact_guards(fact_table)


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {_ACCEPTED_VIEW}")
    op.execute(f"DROP VIEW IF EXISTS {_CURRENT_VIEW}")
    for trigger in (
        f"trg_{_TABLE}_append_only",
        f"trg_{_TABLE}_append_only_delete",
        f"trg_{_TABLE}_revision_first",
        f"trg_{_TABLE}_revision_parent",
        f"trg_{_TABLE}_binding_current",
        f"trg_{_TABLE}_node_issuer",
        f"trg_{_TABLE}_knowledge_clock",
        f"trg_{_TABLE}_accepted_candidate",
        *(f"trg_{_TABLE}_{fact_table}_scope" for fact_table in _FACT_TABLES),
        *(
            f"trg_{_TABLE}_{fact_table}_accepted_{operation}"
            for fact_table in _FACT_TABLES
            for operation in ("update", "delete")
        ),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for index in (
        "ix_legacy_fact_evidence_match_retry",
        "ix_legacy_fact_evidence_match_issuer_outcome",
        "ix_legacy_fact_evidence_match_binding",
        # Compatibility with the pre-hardening draft, which was exercised
        # only on isolated validation databases.
        "ix_legacy_fact_evidence_match_current",
    ):
        op.execute(f"DROP INDEX IF EXISTS {index}")
    op.drop_table(_TABLE)
