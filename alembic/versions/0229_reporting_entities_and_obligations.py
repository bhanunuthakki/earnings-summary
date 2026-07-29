"""Separate legal issuers, reporting entities, securities, and source duties.

Revision ID: 0229_reporting_entities_and_obligations
Revises: 0228_integrity_audit_sentinel_indexes
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0229_reporting_entities_and_obligations"
down_revision: str | None = "0228_integrity_audit_sentinel_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APPEND_ONLY_TABLES = (
    "reporting_entities",
    "reporting_entity_identifier_assertions",
    "reporting_entity_identifier_resolution_outcomes",
    "security_identifier_assertions",
    "security_identifier_resolution_outcomes",
    "security_reporting_entity_revisions",
    "source_obligation_revisions",
)


def _append_only(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'reporting identity ledger is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'reporting identity ledger is append-only'); END"
    )


def _reason_columns() -> tuple[sa.Column[object], ...]:
    return (
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason_details_json", sa.Text(), nullable=False),
    )


def _clock_columns() -> tuple[sa.Column[object], ...]:
    return (
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
    )


def _identifier_assertion_table(
    *,
    table: str,
    owner_column: str,
    owner_table: str,
    owner_pk: str,
    identifier_types: str,
) -> None:
    op.create_table(
        table,
        sa.Column("assertion_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            owner_column,
            sa.String(128),
            sa.ForeignKey(f"{owner_table}.{owner_pk}"),
            nullable=False,
        ),
        sa.Column("identifier_type", sa.String(32), nullable=False),
        sa.Column("identifier_value", sa.Text(), nullable=False),
        sa.Column("normalized_value", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(32), nullable=False),
        sa.Column(
            "source_observation_id",
            sa.String(128),
            sa.ForeignKey("evidence_source_observations.observation_id"),
            nullable=True,
        ),
        *_clock_columns(),
        sa.CheckConstraint(
            f"identifier_type IN ({identifier_types})",
            name=f"ck_{table}_identifier_type",
        ),
        sa.CheckConstraint(
            "authority IN ('issuer_publisher', 'sec_registry', 'exchange_registry', "
            "'regulator', 'manual', 'imported')",
            name=f"ck_{table}_authority",
        ),
        sa.CheckConstraint(
            "authority = 'manual' OR source_observation_id IS NOT NULL",
            name=f"ck_{table}_evidence",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name=f"ck_{table}_clocks",
        ),
    )
    op.create_index(
        f"ix_{table}_lookup",
        table,
        ["identifier_type", "normalized_value", "knowledge_at"],
    )


def _identifier_resolution_table(*, table: str, assertion_table: str) -> None:
    op.create_table(
        table,
        sa.Column("resolution_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("resolution_key", sa.String(512), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column(
            "selected_assertion_id",
            sa.String(128),
            sa.ForeignKey(f"{assertion_table}.assertion_id"),
            nullable=True,
        ),
        sa.Column("candidate_digest_sha256", sa.String(64), nullable=False),
        sa.Column("policy_name", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("policy_config_sha256", sa.String(64), nullable=False),
        *_reason_columns(),
        sa.Column("material_dissent", sa.Boolean(), nullable=False),
        *_clock_columns(),
        sa.Column(
            "supersedes_resolution_id",
            sa.String(128),
            sa.ForeignKey(f"{table}.resolution_id"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "resolution_key",
            "revision",
            name=f"uq_{table}_key_revision",
        ),
        sa.CheckConstraint("revision > 0", name=f"ck_{table}_revision"),
        sa.CheckConstraint(
            "outcome IN ('selected', 'unresolved', 'rejected')",
            name=f"ck_{table}_outcome",
        ),
        sa.CheckConstraint(
            "(outcome = 'selected' AND selected_assertion_id IS NOT NULL) OR "
            "(outcome IN ('unresolved', 'rejected') AND selected_assertion_id IS NULL)",
            name=f"ck_{table}_selection",
        ),
        sa.CheckConstraint(
            "length(candidate_digest_sha256) = 64 "
            "AND length(policy_config_sha256) = 64",
            name=f"ck_{table}_digests",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name=f"ck_{table}_clocks",
        ),
    )
    op.create_index(
        f"ix_{table}_current",
        table,
        ["resolution_key", "revision"],
    )


def _revision_guards(*, table: str, key_column: str, parent_column: str) -> None:
    stem = f"trg_{table}_revision"
    op.execute(
        f"CREATE TRIGGER {stem}_first BEFORE INSERT ON {table} "
        f"WHEN NEW.revision = 1 AND NEW.{parent_column} IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'first revision cannot supersede'); END"
    )
    op.execute(
        f"CREATE TRIGGER {stem}_parent BEFORE INSERT ON {table} "
        f"WHEN NEW.revision > 1 AND (NEW.{parent_column} IS NULL OR NOT EXISTS ("
        f"SELECT 1 FROM {table} AS prior WHERE prior.{parent_column.replace('supersedes_', '')} "
        f"= NEW.{parent_column} AND prior.{key_column} = NEW.{key_column} "
        "AND prior.revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, 'revision must supersede prior revision'); END"
    )


def upgrade() -> None:
    op.create_table(
        "reporting_entities",
        sa.Column("reporting_entity_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "issuer_id",
            sa.String(128),
            sa.ForeignKey("issuer_entities.issuer_id"),
            nullable=False,
        ),
        sa.Column("reporting_entity_kind", sa.String(32), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "reporting_entity_kind IN "
            "('legal_registrant', 'fund_series', 'foreign_reporting_entity', 'other')",
            name="ck_reporting_entity_kind",
        ),
    )
    op.create_index(
        "ix_reporting_entities_issuer",
        "reporting_entities",
        ["issuer_id", "reporting_entity_kind"],
    )
    _identifier_assertion_table(
        table="reporting_entity_identifier_assertions",
        owner_column="reporting_entity_id",
        owner_table="reporting_entities",
        owner_pk="reporting_entity_id",
        identifier_types=(
            "'sec_cik', 'sec_series_id', 'sedar_profile', 'edinet_code', 'lei'"
        ),
    )
    _identifier_resolution_table(
        table="reporting_entity_identifier_resolution_outcomes",
        assertion_table="reporting_entity_identifier_assertions",
    )
    _identifier_assertion_table(
        table="security_identifier_assertions",
        owner_column="security_id",
        owner_table="securities",
        owner_pk="security_id",
        identifier_types=(
            "'sec_class_contract_id', 'isin', 'cusip', 'figi', 'otc_security_id'"
        ),
    )
    _identifier_resolution_table(
        table="security_identifier_resolution_outcomes",
        assertion_table="security_identifier_assertions",
    )
    op.create_table(
        "security_reporting_entity_revisions",
        sa.Column("relationship_revision_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("relationship_key", sa.String(256), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "security_id",
            sa.String(128),
            sa.ForeignKey("securities.security_id"),
            nullable=False,
        ),
        sa.Column(
            "reporting_entity_id",
            sa.String(128),
            sa.ForeignKey("reporting_entities.reporting_entity_id"),
            nullable=False,
        ),
        sa.Column("relationship_kind", sa.String(32), nullable=False),
        sa.Column("decision_kind", sa.String(16), nullable=False),
        *_reason_columns(),
        *_clock_columns(),
        sa.Column(
            "supersedes_relationship_revision_id",
            sa.String(128),
            sa.ForeignKey(
                "security_reporting_entity_revisions.relationship_revision_id"
            ),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "relationship_key",
            "revision",
            name="uq_security_reporting_entity_revision",
        ),
        sa.CheckConstraint(
            "revision > 0", name="ck_security_reporting_entity_revision"
        ),
        sa.CheckConstraint(
            "relationship_kind IN "
            "('reports_through', 'depositary_receipt_for', 'share_class_of')",
            name="ck_security_reporting_entity_kind",
        ),
        sa.CheckConstraint(
            "decision_kind IN ('deterministic', 'manual', 'imported')",
            name="ck_security_reporting_entity_decision",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_security_reporting_entity_clocks",
        ),
    )
    op.create_index(
        "ix_security_reporting_entity_current",
        "security_reporting_entity_revisions",
        ["relationship_key", "revision"],
    )
    op.create_table(
        "source_obligation_revisions",
        sa.Column("obligation_revision_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("obligation_key", sa.String(256), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
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
        sa.Column("authority_kind", sa.String(32), nullable=False),
        sa.Column("document_family", sa.String(64), nullable=False),
        sa.Column("obligation_state", sa.String(16), nullable=False),
        sa.Column("completeness_rule", sa.String(32), nullable=False),
        sa.Column("active_from", sa.DateTime(), nullable=False),
        sa.Column("active_to", sa.DateTime(), nullable=True),
        sa.Column("decision_kind", sa.String(16), nullable=False),
        *_reason_columns(),
        *_clock_columns(),
        sa.Column(
            "supersedes_obligation_revision_id",
            sa.String(128),
            sa.ForeignKey("source_obligation_revisions.obligation_revision_id"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "obligation_key",
            "revision",
            name="uq_source_obligation_revision",
        ),
        sa.CheckConstraint("revision > 0", name="ck_source_obligation_revision"),
        sa.CheckConstraint(
            "authority_kind IN ('sec_edgar', 'sedar_plus', 'edinet', 'issuer_publisher')",
            name="ck_source_obligation_authority",
        ),
        sa.CheckConstraint(
            "document_family IN ('operating_company_periodic', "
            "'investment_company_periodic', 'continuous_disclosure', "
            "'annual_securities_report', 'issuer_financial_statements', "
            "'issuer_presentations', 'issuer_earnings_materials')",
            name="ck_source_obligation_document_family",
        ),
        sa.CheckConstraint(
            "obligation_state IN ('required', 'optional', 'not_applicable')",
            name="ck_source_obligation_state",
        ),
        sa.CheckConstraint(
            "completeness_rule IN "
            "('regulator_inventory', 'publisher_surface_exhaustion', 'manual_exception')",
            name="ck_source_obligation_completeness",
        ),
        sa.CheckConstraint(
            "active_to IS NULL OR active_to > active_from",
            name="ck_source_obligation_active_window",
        ),
        sa.CheckConstraint(
            "decision_kind IN ('deterministic', 'manual', 'imported')",
            name="ck_source_obligation_decision",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_source_obligation_clocks",
        ),
    )
    op.create_index(
        "ix_source_obligation_current",
        "source_obligation_revisions",
        ["issuer_id", "obligation_key", "revision"],
    )

    for table in (
        "reporting_entity_identifier_resolution_outcomes",
        "security_identifier_resolution_outcomes",
    ):
        _revision_guards(
            table=table,
            key_column="resolution_key",
            parent_column="supersedes_resolution_id",
        )
    _revision_guards(
        table="security_reporting_entity_revisions",
        key_column="relationship_key",
        parent_column="supersedes_relationship_revision_id",
    )
    _revision_guards(
        table="source_obligation_revisions",
        key_column="obligation_key",
        parent_column="supersedes_obligation_revision_id",
    )
    op.execute(
        "CREATE TRIGGER trg_reporting_entity_identifier_resolution_scope "
        "BEFORE INSERT ON reporting_entity_identifier_resolution_outcomes "
        "WHEN NEW.selected_assertion_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM reporting_entity_identifier_assertions AS assertion "
        "WHERE assertion.assertion_id = NEW.selected_assertion_id "
        "AND NEW.resolution_key = assertion.identifier_type || ':' || "
        "assertion.normalized_value) "
        "BEGIN SELECT RAISE(ABORT, 'selected reporting identifier must match key'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_security_identifier_resolution_scope "
        "BEFORE INSERT ON security_identifier_resolution_outcomes "
        "WHEN NEW.selected_assertion_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM security_identifier_assertions AS assertion "
        "WHERE assertion.assertion_id = NEW.selected_assertion_id "
        "AND NEW.resolution_key = assertion.identifier_type || ':' || "
        "assertion.normalized_value) "
        "BEGIN SELECT RAISE(ABORT, 'selected security identifier must match key'); END"
    )
    for table in _APPEND_ONLY_TABLES:
        _append_only(table)

    op.execute(
        "CREATE VIEW v_reporting_entity_identifier_resolutions_current AS "
        "SELECT resolution.* "
        "FROM reporting_entity_identifier_resolution_outcomes AS resolution "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM reporting_entity_identifier_resolution_outcomes AS newer "
        "WHERE newer.resolution_key = resolution.resolution_key "
        "AND newer.revision > resolution.revision)"
    )
    op.execute(
        "CREATE VIEW v_reporting_entity_identifiers_canonical AS "
        "SELECT entity.issuer_id, assertion.*, resolution.resolution_id, "
        "resolution.material_dissent "
        "FROM v_reporting_entity_identifier_resolutions_current AS resolution "
        "JOIN reporting_entity_identifier_assertions AS assertion "
        "ON assertion.assertion_id = resolution.selected_assertion_id "
        "JOIN reporting_entities AS entity "
        "ON entity.reporting_entity_id = assertion.reporting_entity_id "
        "WHERE resolution.outcome = 'selected'"
    )
    op.execute(
        "CREATE VIEW v_security_identifier_resolutions_current AS "
        "SELECT resolution.* "
        "FROM security_identifier_resolution_outcomes AS resolution "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM security_identifier_resolution_outcomes AS newer "
        "WHERE newer.resolution_key = resolution.resolution_key "
        "AND newer.revision > resolution.revision)"
    )
    op.execute(
        "CREATE VIEW v_security_identifiers_canonical AS "
        "SELECT security.issuer_id, assertion.*, resolution.resolution_id, "
        "resolution.material_dissent "
        "FROM v_security_identifier_resolutions_current AS resolution "
        "JOIN security_identifier_assertions AS assertion "
        "ON assertion.assertion_id = resolution.selected_assertion_id "
        "JOIN securities AS security ON security.security_id = assertion.security_id "
        "WHERE resolution.outcome = 'selected'"
    )
    op.execute(
        "CREATE VIEW v_security_reporting_entities_current AS "
        "SELECT relation.* FROM security_reporting_entity_revisions AS relation "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM security_reporting_entity_revisions AS newer "
        "WHERE newer.relationship_key = relation.relationship_key "
        "AND newer.revision > relation.revision)"
    )
    op.execute(
        "CREATE VIEW v_source_obligations_current AS "
        "SELECT obligation.* FROM source_obligation_revisions AS obligation "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM source_obligation_revisions AS newer "
        "WHERE newer.obligation_key = obligation.obligation_key "
        "AND newer.revision > obligation.revision)"
    )


def downgrade() -> None:
    for view in (
        "v_source_obligations_current",
        "v_security_reporting_entities_current",
        "v_security_identifiers_canonical",
        "v_security_identifier_resolutions_current",
        "v_reporting_entity_identifiers_canonical",
        "v_reporting_entity_identifier_resolutions_current",
    ):
        op.execute(f"DROP VIEW IF EXISTS {view}")
    for trigger in (
        "trg_reporting_entity_identifier_resolution_scope",
        "trg_security_identifier_resolution_scope",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table, parent in (
        (
            "reporting_entity_identifier_resolution_outcomes",
            "supersedes_resolution_id",
        ),
        ("security_identifier_resolution_outcomes", "supersedes_resolution_id"),
        (
            "security_reporting_entity_revisions",
            "supersedes_relationship_revision_id",
        ),
        ("source_obligation_revisions", "supersedes_obligation_revision_id"),
    ):
        stem = f"trg_{table}_revision"
        op.execute(f"DROP TRIGGER IF EXISTS {stem}_first")
        op.execute(f"DROP TRIGGER IF EXISTS {stem}_parent")
        del parent
    for table in _APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
    for table in reversed(_APPEND_ONLY_TABLES):
        op.drop_table(table)
