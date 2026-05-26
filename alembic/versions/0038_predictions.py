"""predictions — polymorphic forward-looking-statement ledger.

Generalizes the management_commitments table to cover ALL forward-looking
claims the system tracks, with a single matcher/grader pipeline:

  source_kind = 'mgmt_commitment'   — pulled from earnings transcripts (existing)
  source_kind = 'llm_bear_case'      — LLM-generated failure mode hypotheses;
                                       grading is the LLM's own say-do
  source_kind = 'risk_factor_10k'    — Item 1A risk factors with target_period =
                                       next fiscal year; graded annually
  source_kind = 'sell_side'          — sell-side analyst price targets / EPS
                                       estimates snapshot-as-of-date
  source_kind = 'analyst_consensus'  — IBES consensus aggregates from FMP

The grading pipeline (execution/grade_predictions.py) periodically scans
pending rows whose target_period has passed and matches realized values from
financial_facts / kpi_facts.

A backward-compat view `management_commitments_view` exposes the
mgmt_commitment subset in the same shape as the legacy table so existing
queries keep working until they're migrated.

Revision ID: 0038_predictions
Revises: 0037_facts_concept_fks
Create Date: 2026-05-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0038_predictions"
down_revision: str | Sequence[str] | None = "0037_facts_concept_fks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if "predictions" in set(sa.inspect(bind).get_table_names()):
        return

    op.create_table(
        "predictions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        # 'mgmt_commitment' | 'llm_bear_case' | 'risk_factor_10k' |
        # 'sell_side' | 'analyst_consensus'
        sa.Column("source_doc_id", sa.Integer(), nullable=True),  # documents.id
        sa.Column("source_artifact_id", sa.Integer(), nullable=True),  # llm_artifacts.id (for LLM-generated)
        sa.Column("source_excerpt", sa.Text(), nullable=True),
        sa.Column("made_at", sa.DateTime(), nullable=False),
        sa.Column("target_period", sa.DateTime(), nullable=True),
        # Free-form prediction text — always populated for narrative
        # predictions (bear cases, risk factors). Structured fields below
        # may be null when the predictor is purely qualitative.
        sa.Column("prediction_md", sa.Text(), nullable=False),
        sa.Column("kpi_name", sa.String(length=128), nullable=True),
        sa.Column("kpi_concept_id", sa.Integer(), nullable=True),  # FK to concepts
        sa.Column("comparator", sa.String(length=8), nullable=True),  # 'lt','le','gt','ge','eq'
        sa.Column("target_value", sa.Numeric(24, 6), nullable=True),
        sa.Column("target_unit", sa.String(length=32), nullable=True),
        sa.Column("realized_value", sa.Numeric(24, 6), nullable=True),
        sa.Column("realized_doc_id", sa.Integer(), nullable=True),
        sa.Column(
            "outcome",
            sa.String(length=24),
            nullable=False,
            server_default="pending",
        ),
        # 'pending' | 'met' | 'missed' | 'mixed' | 'unfalsifiable'
        sa.Column("outcome_confidence", sa.Float(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(), nullable=True),
        sa.Column("evaluator_run_id", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("idx_predictions_ticker", "predictions", ["ticker"])
    op.create_index("idx_predictions_source_kind", "predictions", ["source_kind"])
    op.create_index(
        "idx_predictions_target_period", "predictions", ["target_period", "outcome"]
    )
    op.create_index(
        "idx_predictions_kpi", "predictions", ["ticker", "kpi_name", "target_period"]
    )
    op.create_index("idx_predictions_outcome", "predictions", ["outcome"])

    # Backfill mgmt_commitment rows from the legacy management_commitments table
    # in a single SQL move. Loose ref; the legacy table stays for now to keep
    # existing SayDo queries working.
    inspector = sa.inspect(bind)
    if "management_commitments" in set(inspector.get_table_names()):
        op.execute(
            """
            INSERT INTO predictions(
                ticker, source_kind, source_doc_id, source_excerpt,
                made_at, target_period, prediction_md,
                kpi_name, comparator, target_value, target_unit,
                realized_value, realized_doc_id, outcome,
                evaluated_at, created_at
            )
            SELECT
                ticker,
                'mgmt_commitment' AS source_kind,
                transcript_segment_id AS source_doc_id,
                narrative AS source_excerpt,
                period_made AS made_at,
                period_target AS target_period,
                COALESCE(narrative, kpi_name || ' ' || comparator || ' ' || CAST(target_value AS TEXT)) AS prediction_md,
                kpi_name, comparator, target_value, unit,
                realized_value, realized_doc_id,
                COALESCE(outcome, 'pending') AS outcome,
                evaluated_at,
                COALESCE(created_at, period_made) AS created_at
            FROM management_commitments
            """
        )


def downgrade() -> None:
    op.drop_index("idx_predictions_outcome", table_name="predictions")
    op.drop_index("idx_predictions_kpi", table_name="predictions")
    op.drop_index("idx_predictions_target_period", table_name="predictions")
    op.drop_index("idx_predictions_source_kind", table_name="predictions")
    op.drop_index("idx_predictions_ticker", table_name="predictions")
    op.drop_table("predictions")
