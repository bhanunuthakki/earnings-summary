"""Allow an append-only correction sourced from the same document.

Revision ID: 0032_allow_source_reviewed_kpi_supersessions
Revises: 0031_add_management_indicator_observations
"""

from __future__ import annotations

from alembic import op

revision = "0032_allow_source_reviewed_kpi_supersessions"
down_revision = "0031_add_management_indicator_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_kpi_facts_provenance")
    op.execute(
        "CREATE UNIQUE INDEX uq_kpi_facts_provenance ON kpi_facts "
        "(ticker,period_end,fiscal_period_type,kpi_definition_id,source_doc_id,"
        "COALESCE(supersedes_id,0))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_kpi_facts_provenance")
    op.execute(
        "CREATE UNIQUE INDEX uq_kpi_facts_provenance ON kpi_facts "
        "(ticker,period_end,fiscal_period_type,kpi_definition_id,source_doc_id)"
    )
