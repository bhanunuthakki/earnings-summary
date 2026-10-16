"""Retain reviewed source precision on immutable KPI context revisions.

Revision ID: 0052_kpi_source_precision
Revises: 0051_kpi_legacy_disposition_capture
"""

from __future__ import annotations

from alembic import op

revision = "0052_kpi_source_precision"
down_revision = "0051_kpi_legacy_disposition_capture"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical NULL means precision has not been reviewed. Never backfill
    # exactness from a scalar or reinterpret a source footnote during migration.
    op.execute(
        "ALTER TABLE kpi_fact_semantic_contexts ADD COLUMN source_precision_json TEXT "
        "CHECK(source_precision_json IS NULL OR json_valid(source_precision_json))"
    )
    op.execute("ALTER TABLE kpi_fact_semantic_contexts ADD COLUMN reported_period_start TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE kpi_fact_semantic_contexts DROP COLUMN reported_period_start")
    op.execute("ALTER TABLE kpi_fact_semantic_contexts DROP COLUMN source_precision_json")
