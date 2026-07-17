"""kpi_facts.formula_id / formula_version -- tag a row produced by the
bottoms-up metrics engine (docs/design/bottoms_up_metrics_engine.md
section 5). Both nullable additive columns via plain ADD COLUMN (no
batch_alter_table needed -- SQLite supports ADD COLUMN natively); existing
`fmp_derived_kpis`-authored rows keep both columns NULL until/unless that
module is migrated onto the registry (a separate, explicit, reviewed step
per the design doc's Phase-1 migration-from-existing-code note).

No REFERENCES clause on `formula_id` -- the repo-wide FK-poisoning
invariant ([[reference-platform-invariants]]): `open_conn` runs `PRAGMA
foreign_keys=ON`, so a real FK fails every child insert when a test
fixture stamps at an earlier alembic revision.

Revision ID: 0162_kpi_facts_formula_columns
Revises: 0161_metric_computation_attempts
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0162_kpi_facts_formula_columns"
down_revision: str | Sequence[str] | None = "0161_metric_computation_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "kpi_facts"


def _has_column(insp: sa.Inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "kpi_facts" not in insp.get_table_names():
        return
    if not _has_column(insp, _TABLE, "formula_id"):
        op.add_column(_TABLE, sa.Column("formula_id", sa.Integer(), nullable=True))
    if not _has_column(insp, _TABLE, "formula_version"):
        op.add_column(_TABLE, sa.Column("formula_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "kpi_facts" not in insp.get_table_names():
        return
    with op.batch_alter_table(_TABLE) as batch:
        if _has_column(insp, _TABLE, "formula_version"):
            batch.drop_column("formula_version")
        if _has_column(insp, _TABLE, "formula_id"):
            batch.drop_column("formula_id")
