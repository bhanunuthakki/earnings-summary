"""formula_definitions -- DB mirror of the metrics-engine formula registry.

Part of the bottoms-up metrics engine Phase 1 (docs/design/
bottoms_up_metrics_engine.md section 2/5). One row per (formula_key,
version) committed in `compute.metrics_engine.registry.REGISTRY`, upserted
at engine-startup via `compute.metrics_engine.io.upsert_formula_definitions`
-- append-only, mirroring `find_or_create_kpi_definition`'s idempotency.

No FK on any future referencing column (repo-wide FK-poisoning invariant,
[[reference-platform-invariants]]): `open_conn` runs `PRAGMA
foreign_keys=ON`, so a real FK fails every child insert when a test
fixture stamps at an earlier alembic revision. `metric_computation_attempts.
formula_id` and `kpi_facts.formula_id` (0154/0155) are plain INTEGER with
code-level validation instead.

Revision ID: 0153_formula_definitions
Revises: 0152_v_thesis_status_stub_substring
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0153_formula_definitions"
down_revision: str | Sequence[str] | None = "0152_v_thesis_status_stub_substring"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "formula_definitions"


def upgrade() -> None:
    bind = op.get_bind()
    names = set(sa.inspect(bind).get_table_names())
    if _TABLE not in names:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("formula_key", sa.String(64), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("category", sa.String(32), nullable=False),
            sa.Column("display_formula", sa.Text(), nullable=False),
            sa.Column("method_notes", sa.Text(), nullable=False),
            sa.Column("period_grid", sa.String(16), nullable=False),
            sa.Column("unit", sa.String(16), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("formula_key", "version", name="uq_formula_definitions_key_version"),
        )
        op.create_index("ix_formula_definitions_formula_key", _TABLE, ["formula_key"])


def downgrade() -> None:
    bind = op.get_bind()
    names = set(sa.inspect(bind).get_table_names())
    if _TABLE in names:
        op.drop_table(_TABLE)
