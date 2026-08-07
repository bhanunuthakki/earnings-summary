"""runs_and_validation — ingestion_runs, stage_transitions, validation_issues.

Pipeline accounting per directives/data_pipeline_dag.md. Every script execution
writes one ingestion_runs row plus N stage_transitions rows. Validation issues
quarantine rows that fail range/currency/period checks before they reach
PERSIST.

Revision ID: 0006_runs_and_validation
Revises: 0005_transcripts_tables
Create Date: 2026-05-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_runs_and_validation"
down_revision: str | Sequence[str] | None = "0005_transcripts_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("directive", sa.String(), nullable=False),
        sa.Column("ticker_scope", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.UniqueConstraint("run_id", name="uq_ingestion_runs_run_id"),
    )

    op.create_table(
        "stage_transitions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=True),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("error_msg", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_stage_transitions_run_ticker_stage",
        "stage_transitions",
        ["run_id", "ticker", "stage"],
    )

    op.create_table(
        "validation_issues",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("source_doc_id", sa.Integer(), sa.ForeignKey("documents.id"), nullable=True),
        sa.Column("ticker", sa.String(), nullable=True),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("rule", sa.String(), nullable=False),
        sa.Column("raw_value", sa.Text(), nullable=True),
        sa.Column("expected", sa.Text(), nullable=True),
        sa.Column("raised_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_validation_issues_severity_resolved",
        "validation_issues",
        ["severity", "resolved_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_validation_issues_severity_resolved", table_name="validation_issues")
    op.drop_table("validation_issues")
    op.drop_index("ix_stage_transitions_run_ticker_stage", table_name="stage_transitions")
    op.drop_table("stage_transitions")
    op.drop_table("ingestion_runs")
