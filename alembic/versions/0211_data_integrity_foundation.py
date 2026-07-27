"""Add durable pipeline identity, validation lifecycle, and DCF input lineage.

All changes are additive. ``ingestion_runs``/``stage_transitions`` remain the
compatibility projection while new writes additionally land in FK-backed
``pipeline_*`` tables. Existing ``bhanu`` data is backfilled with explicit
legacy provenance rather than pretending we know hashes that were not stored.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0211_data_integrity_foundation"
down_revision: str | Sequence[str] | None = "0210_llm_call_transport_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(inspector: sa.Inspector, table: str) -> set[str]:
    return {str(column["name"]) for column in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "pipeline_runs" not in tables:
        op.create_table(
            "pipeline_runs",
            sa.Column("pipeline_key", sa.String(length=64), primary_key=True),
            sa.Column("directive", sa.String(), nullable=False),
            sa.Column("ticker_scope", sa.Text(), nullable=False),
            sa.Column("first_started_at", sa.DateTime(), nullable=False),
        )
    if "pipeline_attempts" not in tables:
        op.create_table(
            "pipeline_attempts",
            sa.Column("attempt_id", sa.String(length=128), primary_key=True),
            sa.Column(
                "pipeline_key",
                sa.String(length=64),
                sa.ForeignKey("pipeline_runs.pipeline_key"),
                nullable=False,
            ),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("ended_at", sa.DateTime(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("error_summary", sa.Text(), nullable=True),
        )
        op.create_index(
            "ix_pipeline_attempts_pipeline_started",
            "pipeline_attempts",
            ["pipeline_key", "started_at"],
        )
    if "pipeline_stage_transitions" not in tables:
        op.create_table(
            "pipeline_stage_transitions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "attempt_id",
                sa.String(length=128),
                sa.ForeignKey("pipeline_attempts.attempt_id"),
                nullable=False,
            ),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            sa.Column("period_end", sa.DateTime(), nullable=True),
            sa.Column("stage", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("ended_at", sa.DateTime(), nullable=True),
            sa.Column("error_msg", sa.Text(), nullable=True),
        )
        op.create_index(
            "ix_pipeline_stage_attempt",
            "pipeline_stage_transitions",
            ["attempt_id", "ticker", "stage"],
        )

    if "ingestion_runs" in tables:
        columns = _columns(inspector, "ingestion_runs")
        if "pipeline_key" not in columns:
            op.add_column(
                "ingestion_runs", sa.Column("pipeline_key", sa.String(length=64), nullable=True)
            )
        if "attempt_id" not in columns:
            op.add_column(
                "ingestion_runs", sa.Column("attempt_id", sa.String(length=128), nullable=True)
            )
        op.execute(
            "UPDATE ingestion_runs SET pipeline_key = 'legacy:' || directive || ':' || ticker_scope "
            "WHERE pipeline_key IS NULL"
        )
        op.execute("UPDATE ingestion_runs SET attempt_id = run_id WHERE attempt_id IS NULL")
        op.execute(
            "INSERT OR IGNORE INTO pipeline_runs (pipeline_key, directive, ticker_scope, first_started_at) "
            "SELECT pipeline_key, directive, ticker_scope, MIN(started_at) FROM ingestion_runs "
            "GROUP BY pipeline_key, directive, ticker_scope"
        )
        op.execute(
            "INSERT OR IGNORE INTO pipeline_attempts "
            "(attempt_id, pipeline_key, started_at, ended_at, status, error_summary) "
            "SELECT attempt_id, pipeline_key, started_at, ended_at, status, error_summary FROM ingestion_runs"
        )
        existing_indexes = {str(index["name"]) for index in inspector.get_indexes("ingestion_runs")}
        if "ix_ingestion_runs_pipeline_key" not in existing_indexes:
            op.create_index("ix_ingestion_runs_pipeline_key", "ingestion_runs", ["pipeline_key"])

    if "validation_issues" in tables:
        columns = _columns(inspector, "validation_issues")
        for name, column in (
            ("fingerprint", sa.Column("fingerprint", sa.String(length=64), nullable=True)),
            ("first_seen_at", sa.Column("first_seen_at", sa.DateTime(), nullable=True)),
            ("last_seen_at", sa.Column("last_seen_at", sa.DateTime(), nullable=True)),
            ("occurrence_count", sa.Column("occurrence_count", sa.Integer(), nullable=True)),
        ):
            if name not in columns:
                op.add_column("validation_issues", column)
        # Legacy records deliberately keep NULL fingerprints: reconstructing a
        # canonical hash in SQLite would make audit history look more certain
        # than it is. New writes own the deterministic lifecycle.
        op.execute(
            "UPDATE validation_issues SET first_seen_at=raised_at, last_seen_at=raised_at, "
            "occurrence_count=1 WHERE first_seen_at IS NULL"
        )
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_validation_issues_fingerprint "
            "ON validation_issues(fingerprint) WHERE fingerprint IS NOT NULL"
        )

    if "dcf_runs" in tables:
        columns = _columns(inspector, "dcf_runs")
        for name, column in (
            ("input_sha256", sa.Column("input_sha256", sa.String(length=64), nullable=True)),
            ("workbook_sha256", sa.Column("workbook_sha256", sa.String(length=64), nullable=True)),
            ("engine_version", sa.Column("engine_version", sa.String(length=128), nullable=True)),
            ("inputs_as_of", sa.Column("inputs_as_of", sa.DateTime(), nullable=True)),
            ("provenance_json", sa.Column("provenance_json", sa.Text(), nullable=True)),
        ):
            if name not in columns:
                op.add_column("dcf_runs", column)
        op.execute(
            "UPDATE dcf_runs SET engine_version='legacy_pre_0211', inputs_as_of=valuation_date, "
            'provenance_json=\'{"backfill":"bhanu_compatible_legacy"}\' '
            "WHERE engine_version IS NULL"
        )

    if "transcripts" in tables:
        # A transcript's document is its immutable raw-evidence anchor. Moving
        # it to a different document would make every speaker/time-code row
        # appear to come from bytes it was never extracted from.
        op.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_transcripts_document_immutable "
            "BEFORE UPDATE OF document_id ON transcripts "
            "WHEN OLD.document_id <> NEW.document_id "
            "BEGIN SELECT RAISE(ABORT, 'transcript document provenance is immutable'); END"
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "transcripts" in tables:
        op.execute("DROP TRIGGER IF EXISTS trg_transcripts_document_immutable")

    if "ingestion_runs" in tables:
        columns = _columns(inspector, "ingestion_runs")
        indexes = {str(index["name"]) for index in inspector.get_indexes("ingestion_runs")}
        if "ix_ingestion_runs_pipeline_key" in indexes:
            op.drop_index("ix_ingestion_runs_pipeline_key", table_name="ingestion_runs")
        removable = [name for name in ("attempt_id", "pipeline_key") if name in columns]
        for name in removable:
            op.drop_column("ingestion_runs", name)

    if "validation_issues" in tables:
        columns = _columns(inspector, "validation_issues")
        indexes = {str(index["name"]) for index in inspector.get_indexes("validation_issues")}
        if "uq_validation_issues_fingerprint" in indexes:
            op.drop_index("uq_validation_issues_fingerprint", table_name="validation_issues")
        removable = [
            name
            for name in (
                "occurrence_count",
                "last_seen_at",
                "first_seen_at",
                "fingerprint",
            )
            if name in columns
        ]
        for name in removable:
            op.drop_column("validation_issues", name)

    if "dcf_runs" in tables:
        columns = _columns(inspector, "dcf_runs")
        removable = [
            name
            for name in (
                "provenance_json",
                "inputs_as_of",
                "engine_version",
                "workbook_sha256",
                "input_sha256",
            )
            if name in columns
        ]
        for name in removable:
            op.drop_column("dcf_runs", name)

    for table in (
        "pipeline_stage_transitions",
        "pipeline_attempts",
        "pipeline_runs",
    ):
        if table in tables:
            op.drop_table(table)
