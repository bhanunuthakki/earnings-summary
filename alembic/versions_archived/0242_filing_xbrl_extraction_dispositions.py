"""Seal every normalized filing-XBRL entry and its final disposition.

Revision ID: 0242_filing_xbrl_extraction_dispositions
Revises: 0241_source_fact_publication_ledger
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0242_filing_xbrl_extraction_dispositions"
down_revision: str | Sequence[str] | None = (
    "0241_source_fact_publication_ledger"
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DISPOSITIONS = "filing_xbrl_extraction_dispositions"
_SEALS = "filing_xbrl_extraction_disposition_seals"


def _hex_check(column: str) -> str:
    return (
        f"length({column}) = 64 AND "
        f"{column} NOT GLOB '*[^0-9a-f]*'"
    )


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
        "evidence_extraction_runs",
        "fact_extraction_run_completeness_seals_v2",
        "fact_observations_v2",
        "fact_reported_observation_anchors_v2",
        "source_fact_publications",
        "source_fact_publication_members",
        "source_fact_publication_seals",
    }
    missing = sorted(required - set(sa.inspect(bind).get_table_names()))
    if missing:
        raise RuntimeError(
            "filing-XBRL disposition ledger requires the sealed source-fact "
            "plane: " + ", ".join(missing)
        )

    op.create_table(
        _DISPOSITIONS,
        sa.Column("disposition_id", sa.String(128), primary_key=True),
        sa.Column(
            "idempotency_key",
            sa.String(256),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "extraction_run_id",
            sa.String(128),
            sa.ForeignKey("evidence_extraction_runs.extraction_run_id"),
            nullable=False,
        ),
        sa.Column("input_ordinal", sa.Integer(), nullable=False),
        sa.Column("canonical_normalized_entry_json", sa.Text(), nullable=False),
        sa.Column("normalized_entry_sha256", sa.String(64), nullable=False),
        sa.Column(
            "normalized_entry_identity_sha256",
            sa.String(64),
            nullable=False,
        ),
        sa.Column("source_entry_sha256", sa.String(64), nullable=False),
        sa.Column("source_locator_sha256", sa.String(64), nullable=False),
        sa.Column("disposition", sa.String(16), nullable=False),
        sa.Column(
            "observation_id",
            sa.String(128),
            sa.ForeignKey("fact_observations_v2.observation_id"),
            nullable=True,
        ),
        sa.Column("primary_input_ordinal", sa.Integer(), nullable=True),
        sa.Column("quarantine_reason_code", sa.String(128), nullable=True),
        sa.Column("quarantine_reason_details_json", sa.Text(), nullable=True),
        sa.Column(
            "quarantine_reason_details_sha256",
            sa.String(64),
            nullable=True,
        ),
        sa.Column("canonical_disposition_json", sa.Text(), nullable=False),
        sa.Column("disposition_sha256", sa.String(64), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "extraction_run_id",
            "input_ordinal",
            name="uq_filing_xbrl_disposition_run_ordinal",
        ),
        sa.CheckConstraint(
            "input_ordinal >= 0",
            name="ck_filing_xbrl_disposition_ordinal",
        ),
        sa.CheckConstraint(
            "disposition IN ('published','duplicate','quarantined')",
            name="ck_filing_xbrl_disposition_kind",
        ),
        sa.CheckConstraint(
            "(disposition = 'published' "
            "AND observation_id IS NOT NULL "
            "AND quarantine_reason_code IS NULL "
            "AND quarantine_reason_details_json IS NULL "
            "AND quarantine_reason_details_sha256 IS NULL "
            "AND primary_input_ordinal IS NULL) "
            "OR (disposition = 'duplicate' "
            "AND observation_id IS NOT NULL "
            "AND primary_input_ordinal >= 0 "
            "AND primary_input_ordinal < input_ordinal "
            "AND quarantine_reason_code IS NULL "
            "AND quarantine_reason_details_json IS NULL "
            "AND quarantine_reason_details_sha256 IS NULL) "
            "OR (disposition = 'quarantined' "
            "AND observation_id IS NULL "
            "AND primary_input_ordinal IS NULL "
            "AND length(trim(quarantine_reason_code)) > 0 "
            "AND quarantine_reason_details_json IS NOT NULL "
            "AND quarantine_reason_details_sha256 IS NOT NULL)",
            name="ck_filing_xbrl_disposition_shape",
        ),
        sa.CheckConstraint(
            "json_valid(canonical_normalized_entry_json) "
            "AND json_type(canonical_normalized_entry_json) = 'object' "
            "AND json_valid(canonical_disposition_json) "
            "AND json_type(canonical_disposition_json) = 'object' "
            "AND (quarantine_reason_details_json IS NULL OR "
            "(json_valid(quarantine_reason_details_json) "
            "AND json_type(quarantine_reason_details_json) = 'object'))",
            name="ck_filing_xbrl_disposition_json",
        ),
        sa.CheckConstraint(
            _hex_check("normalized_entry_sha256")
            + " AND "
            + _hex_check("normalized_entry_identity_sha256")
            + " AND "
            + _hex_check("source_entry_sha256")
            + " AND "
            + _hex_check("source_locator_sha256")
            + " AND "
            + _hex_check("disposition_sha256")
            + " AND (quarantine_reason_details_sha256 IS NULL OR "
            + _hex_check("quarantine_reason_details_sha256")
            + ")",
            name="ck_filing_xbrl_disposition_hashes",
        ),
        sa.CheckConstraint(
            "recorded_at >= knowledge_at",
            name="ck_filing_xbrl_disposition_clocks",
        ),
    )
    op.create_index(
        "ix_filing_xbrl_dispositions_run_order",
        _DISPOSITIONS,
        ["extraction_run_id", "input_ordinal"],
        unique=True,
    )
    op.create_index(
        "uq_filing_xbrl_dispositions_published_observation",
        _DISPOSITIONS,
        ["extraction_run_id", "observation_id"],
        unique=True,
        sqlite_where=sa.text("disposition = 'published'"),
    )

    op.create_table(
        _SEALS,
        sa.Column("disposition_seal_id", sa.String(128), primary_key=True),
        sa.Column(
            "idempotency_key",
            sa.String(256),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "extraction_run_id",
            sa.String(128),
            sa.ForeignKey("evidence_extraction_runs.extraction_run_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "publication_id",
            sa.String(128),
            sa.ForeignKey("source_fact_publications.publication_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "normalized_output_schema_name",
            sa.String(128),
            nullable=False,
        ),
        sa.Column(
            "normalized_output_schema_version",
            sa.String(64),
            nullable=False,
        ),
        sa.Column("extraction_output_sha256", sa.String(64), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("published_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("quarantined_count", sa.Integer(), nullable=False),
        sa.Column("canonical_disposition_set_json", sa.Text(), nullable=False),
        sa.Column("disposition_set_sha256", sa.String(64), nullable=False),
        sa.Column("completeness_policy_sha256", sa.String(64), nullable=False),
        sa.Column("knowledge_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "normalized_output_schema_name = 'filing_xbrl_normalized_output' "
            "AND normalized_output_schema_version = 'v1'",
            name="ck_filing_xbrl_disposition_seal_schema",
        ),
        sa.CheckConstraint(
            "entry_count >= 0 AND published_count >= 0 "
            "AND duplicate_count >= 0 AND quarantined_count >= 0 "
            "AND entry_count = published_count + duplicate_count "
            "+ quarantined_count",
            name="ck_filing_xbrl_disposition_seal_counts",
        ),
        sa.CheckConstraint(
            "json_valid(canonical_disposition_set_json) "
            "AND json_type(canonical_disposition_set_json) = 'array'",
            name="ck_filing_xbrl_disposition_seal_json",
        ),
        sa.CheckConstraint(
            _hex_check("extraction_output_sha256")
            + " AND "
            + _hex_check("disposition_set_sha256")
            + " AND "
            + _hex_check("completeness_policy_sha256"),
            name="ck_filing_xbrl_disposition_seal_hashes",
        ),
        sa.CheckConstraint(
            "recorded_at >= knowledge_at",
            name="ck_filing_xbrl_disposition_seal_clocks",
        ),
    )

    op.execute(
        "CREATE TRIGGER trg_filing_xbrl_dispositions_unsealed "
        f"BEFORE INSERT ON {_DISPOSITIONS} WHEN EXISTS (SELECT 1 "
        f"FROM {_SEALS} AS seal "
        "WHERE seal.extraction_run_id = NEW.extraction_run_id) "
        "BEGIN SELECT RAISE(ABORT, "
        "'filing-XBRL extraction dispositions are already sealed'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_filing_xbrl_dispositions_exact "
        f"BEFORE INSERT ON {_DISPOSITIONS} WHEN "
        "NEW.normalized_entry_sha256 <> "
        "fact_sha256(NEW.canonical_normalized_entry_json) "
        "OR (NEW.quarantine_reason_details_json IS NOT NULL AND "
        "NEW.quarantine_reason_details_sha256 <> "
        "fact_sha256(NEW.quarantine_reason_details_json)) "
        "OR NEW.canonical_disposition_json <> json_object("
        "'disposition', NEW.disposition, "
        "'normalized_entry_identity_sha256', "
        "NEW.normalized_entry_identity_sha256, "
        "'normalized_entry_sha256', NEW.normalized_entry_sha256, "
        "'observation_id', NEW.observation_id, "
        "'ordinal', NEW.input_ordinal, "
        "'primary_input_ordinal', NEW.primary_input_ordinal, "
        "'quarantine_reason_code', NEW.quarantine_reason_code, "
        "'quarantine_reason_details_sha256', "
        "NEW.quarantine_reason_details_sha256, "
        "'source_entry_sha256', NEW.source_entry_sha256, "
        "'source_locator_sha256', NEW.source_locator_sha256) "
        "OR NEW.disposition_sha256 <> "
        "fact_sha256(NEW.canonical_disposition_json) "
        "BEGIN SELECT RAISE(ABORT, "
        "'filing-XBRL disposition commitment mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_filing_xbrl_dispositions_published_anchor "
        f"BEFORE INSERT ON {_DISPOSITIONS} "
        "WHEN NEW.disposition IN ('published','duplicate') AND NOT EXISTS ("
        "SELECT 1 FROM fact_reported_observation_anchors_v2 AS anchor "
        "WHERE anchor.observation_id = NEW.observation_id "
        "AND anchor.extraction_run_id = NEW.extraction_run_id "
        "AND anchor.raw_entry_sha256 = NEW.source_entry_sha256 "
        "AND json_extract(anchor.anchor_payload_json, "
        "'$.source_locator_sha256') = NEW.source_locator_sha256) "
        "BEGIN SELECT RAISE(ABORT, "
        "'published filing-XBRL disposition lacks its exact run anchor'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_filing_xbrl_dispositions_duplicate_primary "
        f"BEFORE INSERT ON {_DISPOSITIONS} "
        "WHEN NEW.disposition = 'duplicate' AND NOT EXISTS ("
        f"SELECT 1 FROM {_DISPOSITIONS} AS primary_entry "
        "WHERE primary_entry.extraction_run_id = NEW.extraction_run_id "
        "AND primary_entry.input_ordinal = NEW.primary_input_ordinal "
        "AND primary_entry.disposition = 'published' "
        "AND primary_entry.observation_id = NEW.observation_id "
        "AND primary_entry.source_entry_sha256 = NEW.source_entry_sha256 "
        "AND primary_entry.normalized_entry_identity_sha256 = "
        "NEW.normalized_entry_identity_sha256) "
        "BEGIN SELECT RAISE(ABORT, "
        "'duplicate filing-XBRL disposition lacks its exact primary'); END"
    )

    op.execute(
        "CREATE TRIGGER trg_filing_xbrl_disposition_seals_exact "
        f"BEFORE INSERT ON {_SEALS} WHEN "
        "NOT EXISTS (SELECT 1 "
        "FROM fact_extraction_run_completeness_seals_v2 AS completeness "
        "JOIN source_fact_publication_members AS extraction_member "
        "ON extraction_member.record_kind = 'extraction_seal' "
        "AND extraction_member.record_id = completeness.extraction_seal_id "
        "JOIN source_fact_publication_seals AS publication_seal "
        "ON publication_seal.publication_id = "
        "extraction_member.publication_id "
        "WHERE completeness.extraction_run_id = NEW.extraction_run_id "
        "AND completeness.extraction_output_sha256 = "
        "NEW.extraction_output_sha256 "
        "AND completeness.completeness_policy_sha256 = "
        "NEW.completeness_policy_sha256 "
        "AND completeness.reported_fact_count = NEW.published_count "
        "AND extraction_member.publication_id = NEW.publication_id) "
        "OR NEW.entry_count <> (SELECT COUNT(*) "
        f"FROM {_DISPOSITIONS} AS disposition "
        "WHERE disposition.extraction_run_id = NEW.extraction_run_id) "
        "OR NEW.published_count <> (SELECT COUNT(*) "
        f"FROM {_DISPOSITIONS} AS disposition "
        "WHERE disposition.extraction_run_id = NEW.extraction_run_id "
        "AND disposition.disposition = 'published') "
        "OR NEW.duplicate_count <> (SELECT COUNT(*) "
        f"FROM {_DISPOSITIONS} AS disposition "
        "WHERE disposition.extraction_run_id = NEW.extraction_run_id "
        "AND disposition.disposition = 'duplicate') "
        "OR NEW.quarantined_count <> (SELECT COUNT(*) "
        f"FROM {_DISPOSITIONS} AS disposition "
        "WHERE disposition.extraction_run_id = NEW.extraction_run_id "
        "AND disposition.disposition = 'quarantined') "
        "OR (NEW.entry_count > 0 AND ("
        f"(SELECT MIN(input_ordinal) FROM {_DISPOSITIONS} "
        "WHERE extraction_run_id = NEW.extraction_run_id) <> 0 OR "
        f"(SELECT MAX(input_ordinal) FROM {_DISPOSITIONS} "
        "WHERE extraction_run_id = NEW.extraction_run_id) "
        "<> NEW.entry_count - 1)) "
        "OR EXISTS (SELECT 1 "
        f"FROM {_DISPOSITIONS} AS disposition "
        "WHERE disposition.extraction_run_id = NEW.extraction_run_id "
        "AND disposition.disposition IN ('published','duplicate') "
        "AND NOT EXISTS (SELECT 1 "
        "FROM source_fact_publication_members AS member "
        "WHERE member.publication_id = NEW.publication_id "
        "AND member.record_kind = 'fact_observation' "
        "AND member.record_id = disposition.observation_id)) "
        "OR EXISTS (SELECT 1 "
        "FROM source_fact_publication_members AS member "
        "WHERE member.publication_id = NEW.publication_id "
        "AND member.record_kind = 'fact_observation' "
        "AND (NOT EXISTS (SELECT 1 "
        "FROM fact_reported_observation_anchors_v2 AS anchor "
        "WHERE anchor.observation_id = member.record_id "
        "AND anchor.extraction_run_id = NEW.extraction_run_id) "
        "OR NOT EXISTS (SELECT 1 "
        f"FROM {_DISPOSITIONS} AS disposition "
        "WHERE disposition.extraction_run_id = NEW.extraction_run_id "
        "AND disposition.disposition IN ('published','duplicate') "
        "AND disposition.observation_id = member.record_id))) "
        "OR NEW.canonical_disposition_set_json <> COALESCE((SELECT "
        "json_group_array(json(ordered.canonical_disposition_json)) FROM ("
        "SELECT disposition.canonical_disposition_json "
        f"FROM {_DISPOSITIONS} AS disposition "
        "WHERE disposition.extraction_run_id = NEW.extraction_run_id "
        "ORDER BY disposition.input_ordinal) AS ordered), '[]') "
        "OR NEW.disposition_set_sha256 <> "
        "fact_sha256(NEW.canonical_disposition_set_json) "
        "BEGIN SELECT RAISE(ABORT, "
        "'filing-XBRL extraction disposition seal mismatch'); END"
    )

    _append_only(_DISPOSITIONS, "filing-XBRL extraction disposition")
    _append_only(_SEALS, "filing-XBRL extraction disposition seal")

    op.execute(
        "CREATE VIEW v_filing_xbrl_extractions_disposition_sealed AS "
        "SELECT seal.*, publication.publication_seal_id "
        f"FROM {_SEALS} AS seal "
        "JOIN source_fact_publication_seals AS publication "
        "ON publication.publication_id = seal.publication_id"
    )


def downgrade() -> None:
    op.execute(
        "DROP VIEW IF EXISTS v_filing_xbrl_extractions_disposition_sealed"
    )
    for trigger in (
        "trg_filing_xbrl_disposition_seals_exact",
        "trg_filing_xbrl_dispositions_duplicate_primary",
        "trg_filing_xbrl_dispositions_published_anchor",
        "trg_filing_xbrl_dispositions_exact",
        "trg_filing_xbrl_dispositions_unsealed",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in (_SEALS, _DISPOSITIONS):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
        op.drop_table(table)
