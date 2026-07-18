"""tracked_companies.business_model_class -- the metrics engine's applicability
gate (docs/design/bottoms_up_metrics_engine.md section 5). Without it, Phase 1
would compute nonsense like "Gross Margin" for NU (a bank).

Mechanics: a bare ``ALTER TABLE ... ADD COLUMN ... CHECK(...)`` via raw SQL
(``op.execute``), NOT ``op.add_column`` with an attached CheckConstraint and
NOT ``op.batch_alter_table`` -- SQLite natively supports a column-level CHECK
in ADD COLUMN without a full table rebuild, and `tracked_companies` carries an
INLINE-ANONYMOUS ``list_type`` CHECK that `batch_alter_table`'s reflection-based
recreate silently drops (the 0073 landmine, [[reference-platform-invariants]]).
Verified directly against a live sqlite3 connection before writing this
migration: a bare ADD COLUMN with an inline CHECK leaves every pre-existing
constraint (including the anonymous one) untouched.

Seeds the known non-default roster names per the design doc: NU -> bank,
BN -> holdco. Extend the seed list (not the enum) as other bank/insurance/
REIT names enter the book.

Revision ID: 0163_tracked_companies_business_model_class
Revises: 0162_kpi_facts_formula_columns
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0163_tracked_companies_business_model_class"
down_revision: str | Sequence[str] | None = "0162_kpi_facts_formula_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "tracked_companies"
_COLUMN = "business_model_class"

# ticker -> business_model_class seed, per docs/design/
# bottoms_up_metrics_engine.md section 5's migration plan.
_SEED: dict[str, str] = {
    "NU": "bank",
    "BN": "holdco",
}


def _has_column(insp: sa.Inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    if not _has_column(insp, _TABLE, _COLUMN):
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} TEXT NOT NULL "
            "DEFAULT 'operating_company' "
            f"CHECK({_COLUMN} IN ('operating_company', 'bank', 'insurance', 'holdco'))"
        )
    for ticker, business_model in _SEED.items():
        op.execute(
            sa.text(f"UPDATE {_TABLE} SET {_COLUMN} = :bm WHERE ticker = :ticker").bindparams(
                bm=business_model, ticker=ticker
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    if _has_column(insp, _TABLE, _COLUMN):
        # Raw SQL, NOT op.batch_alter_table: verified directly (both via a
        # standalone sqlite3 connection AND by running this migration's
        # upgrade/downgrade round-trip) that Alembic's batch machinery
        # chooses a full reflection-based recreate here regardless of
        # recreate="auto" -- which trips 0073's landmine (a trigger
        # referencing tracked_companies by name fails mid-RENAME) AND would
        # silently drop the anonymous list_type CHECK. This SQLite version
        # (3.35+) supports native ALTER TABLE DROP COLUMN, which does
        # neither.
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {_COLUMN}")
