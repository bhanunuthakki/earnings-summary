"""Add ``kpi_definitions.reporting_cadence`` — a KPI's native disclosure frequency.

Most tracked KPIs are quarterly. Some are disclosed ONLY annually (bank
regulatory capital ratios — NU's Basel III CET1/CAR in the 20-F — and other
10-K/20-F-only figures). Before this column the system assumed every KPI was
quarterly and shoehorned annual values onto ``fiscal_period_type='Q4'``, which
renders a gappy quarterly chart and mis-counts "N consecutive periods" for a
break rule. ``reporting_cadence`` is the authoritative per-definition declaration
that downstream rendering, break-rule period-counting, and DCF reads key off; the
facts themselves are tagged ``fiscal_period_type='FY'`` for the annual case (the
``FY`` enum value already exists — no enum migration needed).

The column is ``TEXT NOT NULL DEFAULT 'quarterly'`` with a named CHECK matching
the ``ReportingCadence`` StrEnum (``src/models/kpis.py``). Every existing row
backfills to ``'quarterly'``; the few annual definitions are flipped afterward by
``execution/mark_kpi_cadence.py``.

Upgrade uses a plain ``ALTER TABLE ... ADD COLUMN`` (SQLite admits a NOT-NULL
column with a constant default plus a named column-level CHECK) rather than a
``batch_alter_table`` recreation — ``kpi_definitions`` is referenced by
``kpi_facts.kpi_definition_id`` and a table rebuild on the common path is both
unnecessary and FK-risky here. Downgrade drops the column via batch (SQLite's
DROP COLUMN restrictions).

Revision ID: 0072_kpi_reporting_cadence
Revises: 0071_new_table_check_constraints
Create Date: 2026-06-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0072_kpi_reporting_cadence"
down_revision: str | Sequence[str] | None = "0071_new_table_check_constraints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep in lockstep with models.kpis.ReportingCadence.
_CADENCE_VOCAB = "('quarterly', 'annual', 'ttm')"


def _has_table(inspector: sa.Inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _has_column(inspector: sa.Inspector, table: str, column: str) -> bool:
    """True iff `table` exists AND carries `column`. Table-absence-tolerant: many
    test fixtures run `upgrade head` against a partial DB that never created
    kpi_definitions, mirroring 0071's `_has_table` guard — a bare get_columns on a
    missing table raises NoSuchTableError, so the existence check comes first."""
    if not _has_table(inspector, table):
        return False
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    # No-op when kpi_definitions is absent (partial test schemas) or the column
    # already exists (idempotent re-run).
    if _has_table(insp, "kpi_definitions") and not _has_column(
        insp, "kpi_definitions", "reporting_cadence"
    ):
        op.execute(
            "ALTER TABLE kpi_definitions ADD COLUMN reporting_cadence TEXT NOT NULL "
            "DEFAULT 'quarterly' CONSTRAINT ck_kpi_definitions_reporting_cadence "
            f"CHECK (reporting_cadence IN {_CADENCE_VOCAB})"
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_column(insp, "kpi_definitions", "reporting_cadence"):
        # The column carries a named CHECK referencing it; the batch recreate must
        # drop the constraint in the same block, else the rebuilt table would keep
        # a CHECK on a now-absent column. drop_constraint is tolerant of the named
        # check whether it was added inline (upgrade here) or via batch elsewhere.
        with op.batch_alter_table("kpi_definitions") as batch:
            batch.drop_constraint("ck_kpi_definitions_reporting_cadence", type_="check")
            batch.drop_column("reporting_cadence")
