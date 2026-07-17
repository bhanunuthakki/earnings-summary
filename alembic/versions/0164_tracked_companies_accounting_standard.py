"""tracked_companies.accounting_standard -- which line-item vocabulary a
ticker's financial_facts rows follow (docs/design/
bottoms_up_metrics_engine.md section 5). Drives
`compute.metrics_engine.inputs.resolve_concept`.

Same ADD-COLUMN-with-inline-CHECK-via-raw-SQL mechanics as 0163 (see that
migration's docstring for the full rationale) -- avoids batch_alter_table's
reflection recreate dropping tracked_companies' inline-anonymous list_type
CHECK.

Seeded explicitly per the design doc (NOT inferred from filing_regime -- a
20-F filer is not reliably IFRS, some file US-GAAP-reconciled): NU, BN,
ASML, NVO -> ifrs. Extend the seed list as other IFRS names are onboarded
(directive reference_onboarding_and_quirks.md's per-company quirks list).

Revision ID: 0164_tracked_companies_accounting_standard
Revises: 0163_tracked_companies_business_model_class
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0164_tracked_companies_accounting_standard"
down_revision: str | Sequence[str] | None = "0163_tracked_companies_business_model_class"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "tracked_companies"
_COLUMN = "accounting_standard"

# ticker -> accounting_standard seed (IFRS roster names known at Phase 1).
_IFRS_TICKERS: tuple[str, ...] = ("NU", "BN", "ASML", "NVO")


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
            "DEFAULT 'us_gaap' "
            f"CHECK({_COLUMN} IN ('us_gaap', 'ifrs'))"
        )
    if _IFRS_TICKERS:
        placeholders = ", ".join(f"'{t}'" for t in _IFRS_TICKERS)
        op.execute(f"UPDATE {_TABLE} SET {_COLUMN} = 'ifrs' WHERE ticker IN ({placeholders})")


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    if _has_column(insp, _TABLE, _COLUMN):
        # Raw SQL, NOT op.batch_alter_table -- see 0163's downgrade comment
        # for the full rationale (batch mode chose a full reflection
        # recreate here, tripping the tracked_companies trigger/CHECK
        # landmine; this SQLite version's native DROP COLUMN does not).
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {_COLUMN}")
