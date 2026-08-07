"""Drop dead unread latest_governed tables and zero-ref legacy tables.

Revision ID: 0002_drop_dead_tables
Revises: 0001_initial_schema
Create Date: 2026-08-07 00:01:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "0002_drop_dead_tables"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None

DEAD_TABLES = [
    "latest_governed_document_entries",
    "latest_governed_fact_entries",
    "latest_governed_narrative_entries",
    "latest_governed_narrative_fts",
    "latest_governed_narrative_fts_config",
    "latest_governed_narrative_fts_data",
    "latest_governed_narrative_fts_docsize",
    "latest_governed_narrative_fts_idx",
    "latest_governed_population",
    "latest_governed_population_operation_ledger",
    "latest_governed_population_operation_ledger_v2",
    "latest_governed_refresh_changes",
    "latest_governed_refresh_receipts",
    "latest_governed_refresh_runs",
    "latest_governed_refresh_stage",
    "latest_governed_scope_heads",
    "capital_actions",
    "litigation_matters",
    "numerical_claims",
    "critical_accounting_estimates",
    "fx_rates",
    "kpi_aliases",
    "segment_aliases",
    "exec_holdings",
    "tracked_companies_new",
]


def upgrade() -> None:
    for table in DEAD_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")


def downgrade() -> None:
    pass
