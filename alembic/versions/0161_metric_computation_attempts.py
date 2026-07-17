"""metric_computation_attempts -- the "why is there no value here" ledger.

Part of the bottoms-up metrics engine Phase 1 (docs/design/
bottoms_up_metrics_engine.md section 2). One logical row per (ticker,
period_end, fiscal_period_type, formula_id); a re-run UPSERTs this row
(`compute.metrics_engine.io._persist_attempt`'s `ON CONFLICT ... DO UPDATE`)
rather than accumulating history -- the *value* history (restatements) is
still fully preserved on the kpi_facts side via supersedes_id. Doubles as
the idempotency/recompute-skip ledger via `input_fingerprint`.

`formula_id`/`kpi_fact_id` are plain INTEGER, no REFERENCES clause -- the
repo-wide FK-poisoning invariant ([[reference-platform-invariants]]):
`open_conn` runs `PRAGMA foreign_keys=ON`, so a real FK fails every child
insert when a test fixture stamps at an earlier alembic revision (recurs in
migrations 0086/0093/0095). Validated at the code layer instead
(`io.py` only ever writes ids it just created/looked up itself).

Revision ID: 0161_metric_computation_attempts
Revises: 0160_formula_definitions
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0161_metric_computation_attempts"
down_revision: str | Sequence[str] | None = "0160_formula_definitions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "metric_computation_attempts"


def upgrade() -> None:
    bind = op.get_bind()
    names = set(sa.inspect(bind).get_table_names())
    if _TABLE not in names:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("ticker", sa.String(16), nullable=False),
            sa.Column("period_end", sa.DateTime(), nullable=False),
            sa.Column("fiscal_period_type", sa.String(8), nullable=False),
            # Plain INTEGER -- see module docstring (FK-poisoning invariant).
            sa.Column("formula_id", sa.Integer(), nullable=False),
            # NAMED CHECK (via CheckConstraint below) -- round-trips batch
            # reflection fine, unlike tracked_companies' inline-anonymous
            # CHECK (0073's landmine).
            sa.Column("status", sa.String(16), nullable=False),
            sa.CheckConstraint(
                "status IN ('ok', 'not_computable')", name="ck_metric_computation_attempts_status"
            ),
            sa.Column("reason_code", sa.String(48), nullable=True),
            sa.Column("reason_detail", sa.Text(), nullable=True),
            sa.Column("kpi_fact_id", sa.Integer(), nullable=True),
            sa.Column("input_fingerprint", sa.String(64), nullable=False),
            sa.Column("engine_version", sa.String(64), nullable=False),
            sa.Column("computed_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "uq_metric_computation_attempts_logical",
            _TABLE,
            ["ticker", "period_end", "fiscal_period_type", "formula_id"],
            unique=True,
        )
        op.create_index("ix_metric_computation_attempts_ticker", _TABLE, ["ticker"])
        op.create_index("ix_metric_computation_attempts_status", _TABLE, ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    names = set(sa.inspect(bind).get_table_names())
    if _TABLE in names:
        op.drop_table(_TABLE)
