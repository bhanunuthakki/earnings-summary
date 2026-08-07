"""Add canonical issuer identity, authority surfaces, and reporting scope.

Revision ID: 0227_issuer_reporting_registry
Revises: 0226_fact_cutover_performance_indexes
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0227_issuer_reporting_registry"
down_revision: str | Sequence[str] | None = "0226_fact_cutover_performance_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APPEND_ONLY_TABLES = (
    "issuer_entities",
    "issuer_profile_revisions",
    "issuer_identifier_assertions",
    "issuer_identifier_resolution_outcomes",
    "securities",
    "security_listing_assertions",
    "security_listing_resolution_outcomes",
    "issuer_authority_surface_revisions",
    "issuer_reporting_scope_revisions",
    "legacy_issuer_binding_revisions",
)


def _append_only(table_name: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table_name}_append_only BEFORE UPDATE ON {table_name} "
        "BEGIN SELECT RAISE(ABORT, 'issuer registry is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table_name}_append_only_delete BEFORE DELETE ON {table_name} "
        "BEGIN SELECT RAISE(ABORT, 'issuer registry is append-only'); END"
    )


def upgrade() -> None:
    op.create_table(
        "issuer_entities",
        sa.Column("issuer_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("entity_kind", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "entity_kind IN ('operating_company', 'fund', 'partnership', 'other')",
            name="ck_issuer_entity_kind",
        ),
    )
    op.create_table(
        "issuer_profile_revisions",
        sa.Column("profile_revision_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "issuer_id",
            sa.String(128),
            sa.ForeignKey("issuer_entities.issuer_id"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("legal_name", sa.Text(), nullable=False),
        sa.Column("domicile_country", sa.String(2), nullable=True),
        sa.Column("filing_regime", sa.String(32), nullable=True),
        sa.Column("fiscal_year_end", sa.String(5), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("decision_kind", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason_details_json", sa.Text(), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column(
            "supersedes_profile_revision_id",
            sa.String(128),
            sa.ForeignKey("issuer_profile_revisions.profile_revision_id"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "issuer_id", "revision", name="uq_issuer_profile_revision"
        ),
        sa.CheckConstraint("revision > 0", name="ck_issuer_profile_revision"),
        sa.CheckConstraint(
            "status IN ('active', 'inactive', 'merged', 'dissolved')",
            name="ck_issuer_profile_status",
        ),
        sa.CheckConstraint(
            "decision_kind IN ('deterministic', 'manual', 'imported')",
            name="ck_issuer_profile_decision_kind",
        ),
        sa.CheckConstraint(
            "domicile_country IS NULL OR length(domicile_country) = 2",
            name="ck_issuer_profile_country",
        ),
        sa.CheckConstraint(
            "fiscal_year_end IS NULL OR fiscal_year_end GLOB '[0-1][0-9]-[0-3][0-9]'",
            name="ck_issuer_profile_fye",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_issuer_profile_clocks",
        ),
    )
    op.create_index(
        "ix_issuer_profile_current",
        "issuer_profile_revisions",
        ["issuer_id", "revision"],
    )
    op.create_table(
        "issuer_identifier_assertions",
        sa.Column("assertion_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "issuer_id",
            sa.String(128),
            sa.ForeignKey("issuer_entities.issuer_id"),
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
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "identifier_type IN ('sec_cik', 'lei', 'isin', 'figi', "
            "'sedar_profile', 'companies_house')",
            name="ck_issuer_identifier_type",
        ),
        sa.CheckConstraint(
            "authority IN ('issuer_publisher', 'sec_registry', 'exchange_registry', "
            "'regulator', 'manual', 'imported')",
            name="ck_issuer_identifier_authority",
        ),
        sa.CheckConstraint(
            "authority = 'manual' OR source_observation_id IS NOT NULL",
            name="ck_issuer_identifier_evidence",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_issuer_identifier_clocks",
        ),
    )
    op.create_index(
        "ix_issuer_identifier_candidate",
        "issuer_identifier_assertions",
        ["identifier_type", "normalized_value", "recorded_at"],
    )
    op.create_index(
        "ix_issuer_identifier_entity",
        "issuer_identifier_assertions",
        ["issuer_id", "identifier_type", "recorded_at"],
    )
    op.create_table(
        "issuer_identifier_resolution_outcomes",
        sa.Column("resolution_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("resolution_key", sa.String(512), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column(
            "selected_assertion_id",
            sa.String(128),
            sa.ForeignKey("issuer_identifier_assertions.assertion_id"),
            nullable=True,
        ),
        sa.Column("candidate_digest_sha256", sa.String(64), nullable=False),
        sa.Column("policy_name", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("policy_config_sha256", sa.String(64), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason_details_json", sa.Text(), nullable=False),
        sa.Column("material_dissent", sa.Boolean(), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column(
            "supersedes_resolution_id",
            sa.String(128),
            sa.ForeignKey("issuer_identifier_resolution_outcomes.resolution_id"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "resolution_key", "revision", name="uq_issuer_identifier_resolution_revision"
        ),
        sa.CheckConstraint("revision > 0", name="ck_issuer_identifier_resolution_revision"),
        sa.CheckConstraint(
            "outcome IN ('selected', 'unresolved', 'rejected')",
            name="ck_issuer_identifier_resolution_outcome",
        ),
        sa.CheckConstraint(
            "(outcome = 'selected' AND selected_assertion_id IS NOT NULL) OR "
            "(outcome IN ('unresolved', 'rejected') AND selected_assertion_id IS NULL)",
            name="ck_issuer_identifier_resolution_selection",
        ),
        sa.CheckConstraint(
            "length(candidate_digest_sha256) = 64 AND "
            "length(policy_config_sha256) = 64",
            name="ck_issuer_identifier_resolution_hashes",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_issuer_identifier_resolution_clocks",
        ),
    )
    op.create_index(
        "ix_issuer_identifier_resolution_current",
        "issuer_identifier_resolution_outcomes",
        ["resolution_key", "revision"],
    )
    op.create_table(
        "securities",
        sa.Column("security_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "issuer_id",
            sa.String(128),
            sa.ForeignKey("issuer_entities.issuer_id"),
            nullable=False,
        ),
        sa.Column("security_kind", sa.String(32), nullable=False),
        sa.Column("share_class", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "security_kind IN ('common_stock', 'preferred_stock', 'adr', 'fund_share', "
            "'partnership_unit', 'debt', 'other')",
            name="ck_security_kind",
        ),
    )
    op.create_index("ix_security_issuer", "securities", ["issuer_id", "security_id"])
    op.create_table(
        "security_listing_assertions",
        sa.Column("assertion_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "security_id",
            sa.String(128),
            sa.ForeignKey("securities.security_id"),
            nullable=False,
        ),
        sa.Column("market_mic", sa.String(8), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("normalized_ticker", sa.Text(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("authority", sa.String(32), nullable=False),
        sa.Column(
            "source_observation_id",
            sa.String(128),
            sa.ForeignKey("evidence_source_observations.observation_id"),
            nullable=True,
        ),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(market_mic) BETWEEN 4 AND 8",
            name="ck_security_listing_mic",
        ),
        sa.CheckConstraint(
            "length(currency) = 3",
            name="ck_security_listing_currency",
        ),
        sa.CheckConstraint(
            "status IN ('listed', 'delisted', 'suspended')",
            name="ck_security_listing_status",
        ),
        sa.CheckConstraint(
            "authority IN ('issuer_publisher', 'sec_registry', 'exchange_registry', "
            "'regulator', 'manual', 'imported')",
            name="ck_security_listing_authority",
        ),
        sa.CheckConstraint(
            "authority = 'manual' OR source_observation_id IS NOT NULL",
            name="ck_security_listing_evidence",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_security_listing_clocks",
        ),
    )
    op.create_index(
        "ix_security_listing_candidate",
        "security_listing_assertions",
        ["market_mic", "normalized_ticker", "recorded_at"],
    )
    op.create_index(
        "ix_security_listing_security",
        "security_listing_assertions",
        ["security_id", "recorded_at"],
    )
    op.create_table(
        "security_listing_resolution_outcomes",
        sa.Column("resolution_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("resolution_key", sa.String(512), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column(
            "selected_assertion_id",
            sa.String(128),
            sa.ForeignKey("security_listing_assertions.assertion_id"),
            nullable=True,
        ),
        sa.Column("candidate_digest_sha256", sa.String(64), nullable=False),
        sa.Column("policy_name", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("policy_config_sha256", sa.String(64), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason_details_json", sa.Text(), nullable=False),
        sa.Column("material_dissent", sa.Boolean(), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column(
            "supersedes_resolution_id",
            sa.String(128),
            sa.ForeignKey("security_listing_resolution_outcomes.resolution_id"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "resolution_key",
            "revision",
            name="uq_security_listing_resolution_revision",
        ),
        sa.CheckConstraint("revision > 0", name="ck_security_listing_resolution_revision"),
        sa.CheckConstraint(
            "outcome IN ('selected', 'unresolved', 'rejected')",
            name="ck_security_listing_resolution_outcome",
        ),
        sa.CheckConstraint(
            "(outcome = 'selected' AND selected_assertion_id IS NOT NULL) OR "
            "(outcome IN ('unresolved', 'rejected') AND selected_assertion_id IS NULL)",
            name="ck_security_listing_resolution_selection",
        ),
        sa.CheckConstraint(
            "length(candidate_digest_sha256) = 64 AND "
            "length(policy_config_sha256) = 64",
            name="ck_security_listing_resolution_hashes",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_security_listing_resolution_clocks",
        ),
    )
    op.create_index(
        "ix_security_listing_resolution_current",
        "security_listing_resolution_outcomes",
        ["resolution_key", "revision"],
    )
    op.create_table(
        "issuer_authority_surface_revisions",
        sa.Column("surface_revision_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "issuer_id",
            sa.String(128),
            sa.ForeignKey("issuer_entities.issuer_id"),
            nullable=False,
        ),
        sa.Column("surface_key", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("surface_kind", sa.String(32), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("authority_level", sa.String(16), nullable=False),
        sa.Column(
            "source_observation_id",
            sa.String(128),
            sa.ForeignKey("evidence_source_observations.observation_id"),
            nullable=True,
        ),
        sa.Column("verification_method", sa.String(128), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column(
            "supersedes_surface_revision_id",
            sa.String(128),
            sa.ForeignKey("issuer_authority_surface_revisions.surface_revision_id"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "issuer_id",
            "surface_key",
            "revision",
            name="uq_issuer_authority_surface_revision",
        ),
        sa.CheckConstraint("revision > 0", name="ck_issuer_authority_surface_revision"),
        sa.CheckConstraint(
            "surface_kind IN ('sec_submissions', 'sec_companyfacts', 'ir_home', "
            "'ir_archive', 'ir_events', 'ir_presentations', 'ir_financials', "
            "'ir_sec_filings', 'earnings_feed', 'other')",
            name="ck_issuer_authority_surface_kind",
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'verified', 'retired', 'unavailable')",
            name="ck_issuer_authority_surface_status",
        ),
        sa.CheckConstraint(
            "authority_level IN ('regulator', 'publisher', 'third_party')",
            name="ck_issuer_authority_surface_level",
        ),
        sa.CheckConstraint(
            "status <> 'verified' OR source_observation_id IS NOT NULL",
            name="ck_issuer_authority_surface_evidence",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_issuer_authority_surface_clocks",
        ),
    )
    op.create_index(
        "ix_issuer_authority_surface_current",
        "issuer_authority_surface_revisions",
        ["issuer_id", "surface_key", "revision"],
    )
    op.create_table(
        "issuer_reporting_scope_revisions",
        sa.Column("scope_revision_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("scope_key", sa.String(128), nullable=False),
        sa.Column(
            "issuer_id",
            sa.String(128),
            sa.ForeignKey("issuer_entities.issuer_id"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("inclusion_state", sa.String(16), nullable=False),
        sa.Column("history_policy", sa.String(32), nullable=False),
        sa.Column("history_start", sa.DateTime(), nullable=True),
        sa.Column("latest_years", sa.Integer(), nullable=True),
        sa.Column("require_sec", sa.Boolean(), nullable=False),
        sa.Column("require_ir", sa.Boolean(), nullable=False),
        sa.Column("require_earnings", sa.Boolean(), nullable=False),
        sa.Column("decision_kind", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason_details_json", sa.Text(), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column(
            "supersedes_scope_revision_id",
            sa.String(128),
            sa.ForeignKey("issuer_reporting_scope_revisions.scope_revision_id"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "scope_key",
            "issuer_id",
            "revision",
            name="uq_issuer_reporting_scope_revision",
        ),
        sa.CheckConstraint("revision > 0", name="ck_issuer_reporting_scope_revision"),
        sa.CheckConstraint(
            "inclusion_state IN ('core', 'monitored', 'discovery', 'excluded')",
            name="ck_issuer_reporting_scope_inclusion",
        ),
        sa.CheckConstraint(
            "history_policy IN ('all_available', 'since_date', 'latest_n_years')",
            name="ck_issuer_reporting_scope_history_policy",
        ),
        sa.CheckConstraint(
            "(history_policy = 'all_available' AND history_start IS NULL "
            "AND latest_years IS NULL) OR "
            "(history_policy = 'since_date' AND history_start IS NOT NULL "
            "AND latest_years IS NULL) OR "
            "(history_policy = 'latest_n_years' AND history_start IS NULL "
            "AND latest_years > 0)",
            name="ck_issuer_reporting_scope_history",
        ),
        sa.CheckConstraint(
            "decision_kind IN ('deterministic', 'manual', 'imported')",
            name="ck_issuer_reporting_scope_decision_kind",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_issuer_reporting_scope_clocks",
        ),
    )
    op.create_index(
        "ix_issuer_reporting_scope_current",
        "issuer_reporting_scope_revisions",
        ["scope_key", "issuer_id", "revision"],
    )
    op.create_table(
        "legacy_issuer_binding_revisions",
        sa.Column("binding_revision_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("recorded_issuer_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "issuer_id",
            sa.String(128),
            sa.ForeignKey("issuer_entities.issuer_id"),
            nullable=True,
        ),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("decision_kind", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reason_details_json", sa.Text(), nullable=False),
        sa.Column("material_dissent", sa.Boolean(), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column(
            "supersedes_binding_revision_id",
            sa.String(128),
            sa.ForeignKey("legacy_issuer_binding_revisions.binding_revision_id"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "recorded_issuer_id",
            "revision",
            name="uq_legacy_issuer_binding_revision",
        ),
        sa.CheckConstraint("revision > 0", name="ck_legacy_issuer_binding_revision"),
        sa.CheckConstraint(
            "outcome IN ('selected', 'unresolved', 'retired')",
            name="ck_legacy_issuer_binding_outcome",
        ),
        sa.CheckConstraint(
            "(outcome = 'selected' AND issuer_id IS NOT NULL) OR "
            "(outcome IN ('unresolved', 'retired') AND issuer_id IS NULL)",
            name="ck_legacy_issuer_binding_selection",
        ),
        sa.CheckConstraint(
            "decision_kind IN ('deterministic', 'manual', 'imported')",
            name="ck_legacy_issuer_binding_decision_kind",
        ),
        sa.CheckConstraint(
            "knowledge_at >= effective_at AND recorded_at >= knowledge_at",
            name="ck_legacy_issuer_binding_clocks",
        ),
    )
    op.create_index(
        "ix_legacy_issuer_binding_current",
        "legacy_issuer_binding_revisions",
        ["recorded_issuer_id", "revision"],
    )

    op.execute(
        "CREATE TRIGGER trg_issuer_profile_revisions_revision_first "
        "BEFORE INSERT ON issuer_profile_revisions "
        "WHEN NEW.revision = 1 AND NEW.supersedes_profile_revision_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'first issuer profile cannot supersede'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_issuer_profile_revisions_revision_parent "
        "BEFORE INSERT ON issuer_profile_revisions "
        "WHEN NEW.revision > 1 AND (NEW.supersedes_profile_revision_id IS NULL OR "
        "NOT EXISTS (SELECT 1 FROM issuer_profile_revisions AS prior "
        "WHERE prior.profile_revision_id = NEW.supersedes_profile_revision_id "
        "AND prior.issuer_id = NEW.issuer_id AND prior.revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, 'issuer profile must supersede prior revision'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_issuer_identifier_resolution_revision_first "
        "BEFORE INSERT ON issuer_identifier_resolution_outcomes "
        "WHEN NEW.revision = 1 AND NEW.supersedes_resolution_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'first identifier resolution cannot supersede'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_issuer_identifier_resolution_revision_parent "
        "BEFORE INSERT ON issuer_identifier_resolution_outcomes "
        "WHEN NEW.revision > 1 AND (NEW.supersedes_resolution_id IS NULL OR "
        "NOT EXISTS (SELECT 1 FROM issuer_identifier_resolution_outcomes AS prior "
        "WHERE prior.resolution_id = NEW.supersedes_resolution_id "
        "AND prior.resolution_key = NEW.resolution_key "
        "AND prior.revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, 'identifier resolution must supersede prior revision'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_issuer_identifier_resolution_scope "
        "BEFORE INSERT ON issuer_identifier_resolution_outcomes "
        "WHEN NEW.selected_assertion_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM issuer_identifier_assertions AS assertion "
        "WHERE assertion.assertion_id = NEW.selected_assertion_id "
        "AND NEW.resolution_key = assertion.identifier_type || ':' || "
        "assertion.normalized_value) "
        "BEGIN SELECT RAISE(ABORT, 'selected identifier assertion must match resolution key'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_security_listing_resolution_revision_first "
        "BEFORE INSERT ON security_listing_resolution_outcomes "
        "WHEN NEW.revision = 1 AND NEW.supersedes_resolution_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'first listing resolution cannot supersede'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_security_listing_resolution_revision_parent "
        "BEFORE INSERT ON security_listing_resolution_outcomes "
        "WHEN NEW.revision > 1 AND (NEW.supersedes_resolution_id IS NULL OR "
        "NOT EXISTS (SELECT 1 FROM security_listing_resolution_outcomes AS prior "
        "WHERE prior.resolution_id = NEW.supersedes_resolution_id "
        "AND prior.resolution_key = NEW.resolution_key "
        "AND prior.revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, 'listing resolution must supersede prior revision'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_security_listing_resolution_scope "
        "BEFORE INSERT ON security_listing_resolution_outcomes "
        "WHEN NEW.selected_assertion_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM security_listing_assertions AS assertion "
        "WHERE assertion.assertion_id = NEW.selected_assertion_id "
        "AND NEW.resolution_key = 'listing:' || assertion.market_mic || ':' || "
        "assertion.normalized_ticker) "
        "BEGIN SELECT RAISE(ABORT, 'selected listing assertion must match resolution key'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_issuer_authority_surface_revision_first "
        "BEFORE INSERT ON issuer_authority_surface_revisions "
        "WHEN NEW.revision = 1 AND NEW.supersedes_surface_revision_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'first authority surface cannot supersede'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_issuer_authority_surface_revision_parent "
        "BEFORE INSERT ON issuer_authority_surface_revisions "
        "WHEN NEW.revision > 1 AND (NEW.supersedes_surface_revision_id IS NULL OR "
        "NOT EXISTS (SELECT 1 FROM issuer_authority_surface_revisions AS prior "
        "WHERE prior.surface_revision_id = NEW.supersedes_surface_revision_id "
        "AND prior.issuer_id = NEW.issuer_id AND prior.surface_key = NEW.surface_key "
        "AND prior.revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, 'authority surface must supersede prior revision'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_issuer_reporting_scope_revision_first "
        "BEFORE INSERT ON issuer_reporting_scope_revisions "
        "WHEN NEW.revision = 1 AND NEW.supersedes_scope_revision_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'first reporting scope cannot supersede'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_issuer_reporting_scope_revision_parent "
        "BEFORE INSERT ON issuer_reporting_scope_revisions "
        "WHEN NEW.revision > 1 AND (NEW.supersedes_scope_revision_id IS NULL OR "
        "NOT EXISTS (SELECT 1 FROM issuer_reporting_scope_revisions AS prior "
        "WHERE prior.scope_revision_id = NEW.supersedes_scope_revision_id "
        "AND prior.scope_key = NEW.scope_key AND prior.issuer_id = NEW.issuer_id "
        "AND prior.revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, 'reporting scope must supersede prior revision'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_legacy_issuer_binding_revision_first "
        "BEFORE INSERT ON legacy_issuer_binding_revisions "
        "WHEN NEW.revision = 1 AND NEW.supersedes_binding_revision_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'first legacy binding cannot supersede'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_legacy_issuer_binding_revision_parent "
        "BEFORE INSERT ON legacy_issuer_binding_revisions "
        "WHEN NEW.revision > 1 AND (NEW.supersedes_binding_revision_id IS NULL OR "
        "NOT EXISTS (SELECT 1 FROM legacy_issuer_binding_revisions AS prior "
        "WHERE prior.binding_revision_id = NEW.supersedes_binding_revision_id "
        "AND prior.recorded_issuer_id = NEW.recorded_issuer_id "
        "AND prior.revision = NEW.revision - 1)) "
        "BEGIN SELECT RAISE(ABORT, 'legacy binding must supersede prior revision'); END"
    )
    for table_name in _APPEND_ONLY_TABLES:
        _append_only(table_name)

    op.execute(
        "CREATE VIEW v_issuer_profiles_current AS "
        "SELECT profile.* FROM issuer_profile_revisions AS profile "
        "WHERE NOT EXISTS (SELECT 1 FROM issuer_profile_revisions AS newer "
        "WHERE newer.issuer_id = profile.issuer_id AND newer.revision > profile.revision)"
    )
    op.execute(
        "CREATE VIEW v_issuer_identifier_resolutions_current AS "
        "SELECT resolution.* FROM issuer_identifier_resolution_outcomes AS resolution "
        "WHERE NOT EXISTS (SELECT 1 FROM issuer_identifier_resolution_outcomes AS newer "
        "WHERE newer.resolution_key = resolution.resolution_key "
        "AND newer.revision > resolution.revision)"
    )
    op.execute(
        "CREATE VIEW v_issuer_identifiers_canonical AS "
        "SELECT assertion.*, resolution.resolution_id, resolution.revision AS resolution_revision, "
        "resolution.material_dissent, resolution.reason_code AS resolution_reason_code "
        "FROM v_issuer_identifier_resolutions_current AS resolution "
        "JOIN issuer_identifier_assertions AS assertion "
        "ON assertion.assertion_id = resolution.selected_assertion_id "
        "WHERE resolution.outcome = 'selected'"
    )
    op.execute(
        "CREATE VIEW v_issuer_authority_surfaces_current AS "
        "SELECT surface.* FROM issuer_authority_surface_revisions AS surface "
        "WHERE NOT EXISTS (SELECT 1 FROM issuer_authority_surface_revisions AS newer "
        "WHERE newer.issuer_id = surface.issuer_id "
        "AND newer.surface_key = surface.surface_key "
        "AND newer.revision > surface.revision)"
    )
    op.execute(
        "CREATE VIEW v_security_listing_resolutions_current AS "
        "SELECT resolution.* FROM security_listing_resolution_outcomes AS resolution "
        "WHERE NOT EXISTS (SELECT 1 FROM security_listing_resolution_outcomes AS newer "
        "WHERE newer.resolution_key = resolution.resolution_key "
        "AND newer.revision > resolution.revision)"
    )
    op.execute(
        "CREATE VIEW v_security_listings_canonical AS "
        "SELECT security.issuer_id, assertion.*, resolution.resolution_id, "
        "resolution.revision AS resolution_revision, resolution.material_dissent, "
        "resolution.reason_code AS resolution_reason_code "
        "FROM v_security_listing_resolutions_current AS resolution "
        "JOIN security_listing_assertions AS assertion "
        "ON assertion.assertion_id = resolution.selected_assertion_id "
        "JOIN securities AS security ON security.security_id = assertion.security_id "
        "WHERE resolution.outcome = 'selected'"
    )
    op.execute(
        "CREATE VIEW v_issuer_reporting_scope_current AS "
        "SELECT scope.* FROM issuer_reporting_scope_revisions AS scope "
        "WHERE NOT EXISTS (SELECT 1 FROM issuer_reporting_scope_revisions AS newer "
        "WHERE newer.scope_key = scope.scope_key AND newer.issuer_id = scope.issuer_id "
        "AND newer.revision > scope.revision)"
    )
    op.execute(
        "CREATE VIEW v_legacy_issuer_bindings_current AS "
        "SELECT binding.recorded_issuer_id, binding.issuer_id AS canonical_issuer_id, "
        "binding.binding_revision_id, binding.revision, binding.outcome, "
        "binding.decision_kind, binding.reason_code, binding.material_dissent, "
        "binding.effective_at, binding.knowledge_at, binding.recorded_at "
        "FROM legacy_issuer_binding_revisions AS binding "
        "WHERE NOT EXISTS (SELECT 1 FROM legacy_issuer_binding_revisions AS newer "
        "WHERE newer.recorded_issuer_id = binding.recorded_issuer_id "
        "AND newer.revision > binding.revision)"
    )
    op.execute(
        "CREATE VIEW v_evidence_document_versions_canonical AS "
        "SELECT "
        "document.document_version_id, document.document_key, "
        "document.version_sequence, document.observation_id, document.blob_sha256, "
        "COALESCE(canonical.issuer_id, binding.canonical_issuer_id, document.issuer_id) "
        "AS issuer_id, "
        "document.issuer_id AS recorded_issuer_id, document.ticker, "
        "document.document_type, document.form_type, document.accession_number, "
        "document.exhibit_id, document.period_start, document.period_end, "
        "document.as_of_at, document.language, document.replaces_document_version_id, "
        "document.legacy_document_id, document.recorded_at "
        "FROM evidence_document_versions AS document "
        "LEFT JOIN issuer_entities AS canonical "
        "ON canonical.issuer_id = document.issuer_id "
        "LEFT JOIN v_legacy_issuer_bindings_current AS binding "
        "ON binding.recorded_issuer_id = document.issuer_id "
        "AND binding.outcome = 'selected'"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_source_coverage_document_scope")
    op.execute(
        "CREATE TRIGGER trg_source_coverage_document_scope "
        "BEFORE INSERT ON source_coverage_assessments "
        "WHEN NEW.document_version_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM v_evidence_document_versions_canonical AS document "
        "JOIN expected_documents AS expected "
        "ON expected.expected_document_id = NEW.expected_document_id "
        "WHERE document.document_version_id = NEW.document_version_id "
        "AND document.issuer_id = expected.issuer_id) "
        "BEGIN SELECT RAISE(ABORT, 'coverage document must match canonical expected issuer'); END"
    )


def downgrade() -> None:
    for view_name in (
        "v_evidence_document_versions_canonical",
        "v_issuer_reporting_scope_current",
        "v_legacy_issuer_bindings_current",
        "v_security_listings_canonical",
        "v_security_listing_resolutions_current",
        "v_issuer_authority_surfaces_current",
        "v_issuer_identifiers_canonical",
        "v_issuer_identifier_resolutions_current",
        "v_issuer_profiles_current",
    ):
        op.execute(f"DROP VIEW IF EXISTS {view_name}")
    op.execute("DROP TRIGGER IF EXISTS trg_source_coverage_document_scope")
    for trigger_name in (
        "trg_issuer_profile_revisions_revision_first",
        "trg_issuer_profile_revisions_revision_parent",
        "trg_issuer_identifier_resolution_revision_first",
        "trg_issuer_identifier_resolution_revision_parent",
        "trg_issuer_identifier_resolution_scope",
        "trg_security_listing_resolution_revision_first",
        "trg_security_listing_resolution_revision_parent",
        "trg_security_listing_resolution_scope",
        "trg_issuer_authority_surface_revision_first",
        "trg_issuer_authority_surface_revision_parent",
        "trg_issuer_reporting_scope_revision_first",
        "trg_issuer_reporting_scope_revision_parent",
        "trg_legacy_issuer_binding_revision_first",
        "trg_legacy_issuer_binding_revision_parent",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
    for table_name in reversed(_APPEND_ONLY_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only_delete")
    op.drop_index(
        "ix_legacy_issuer_binding_current",
        table_name="legacy_issuer_binding_revisions",
    )
    op.drop_table("legacy_issuer_binding_revisions")
    op.drop_index(
        "ix_issuer_reporting_scope_current",
        table_name="issuer_reporting_scope_revisions",
    )
    op.drop_table("issuer_reporting_scope_revisions")
    op.drop_index(
        "ix_issuer_authority_surface_current",
        table_name="issuer_authority_surface_revisions",
    )
    op.drop_table("issuer_authority_surface_revisions")
    op.drop_index(
        "ix_security_listing_resolution_current",
        table_name="security_listing_resolution_outcomes",
    )
    op.drop_table("security_listing_resolution_outcomes")
    op.drop_index(
        "ix_security_listing_security",
        table_name="security_listing_assertions",
    )
    op.drop_index(
        "ix_security_listing_candidate",
        table_name="security_listing_assertions",
    )
    op.drop_table("security_listing_assertions")
    op.drop_index("ix_security_issuer", table_name="securities")
    op.drop_table("securities")
    op.drop_index(
        "ix_issuer_identifier_resolution_current",
        table_name="issuer_identifier_resolution_outcomes",
    )
    op.drop_table("issuer_identifier_resolution_outcomes")
    op.drop_index(
        "ix_issuer_identifier_entity",
        table_name="issuer_identifier_assertions",
    )
    op.drop_index(
        "ix_issuer_identifier_candidate",
        table_name="issuer_identifier_assertions",
    )
    op.drop_table("issuer_identifier_assertions")
    op.drop_index("ix_issuer_profile_current", table_name="issuer_profile_revisions")
    op.drop_table("issuer_profile_revisions")
    op.drop_table("issuer_entities")
    op.execute(
        "CREATE TRIGGER trg_source_coverage_document_scope "
        "BEFORE INSERT ON source_coverage_assessments "
        "WHEN NEW.document_version_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM evidence_document_versions AS document "
        "JOIN expected_documents AS expected "
        "ON expected.expected_document_id = NEW.expected_document_id "
        "WHERE document.document_version_id = NEW.document_version_id "
        "AND document.issuer_id = expected.issuer_id) "
        "BEGIN SELECT RAISE(ABORT, 'coverage document must match expected issuer'); END"
    )
