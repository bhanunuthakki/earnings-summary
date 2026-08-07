"""CHECK: dcf_runs.over_under_pct must be self-consistent with the row's own
live_price / npv_per_share under the documented decimal-ratio convention
(migration 0024): over_under_pct = (live_price - npv_per_share) / npv_per_share.

Closes the substrate gap behind the #368 incident: four bespoke builders
hand-rolled the column as percent UPSIDE ((fair/price - 1) * 100) — wrong sign
and scale — so the analytical dashboard's trigger ladder (> 0.20 -> 'sell')
classified deeply under-valued names as 'sell'. The Python chokepoint now
derives the value centrally (src/dcf/persist.derive_over_under); this CHECK
makes the same guarantee hold for ANY writer, including raw SQL.

The 0.005 absolute tolerance admits write-time rounding while rejecting
percent-scale or sign-flipped values. NULL arms keep legacy shapes legal:
rows without a live price / fair value, and the #291 non-positive-fair-value
case, store NULL over_under_pct. `1.0 *` forces REAL division (NUMERIC-affinity
columns could otherwise hit SQLite integer division).

SQLite cannot ALTER TABLE ADD CONSTRAINT, so the CHECK rides on a
batch_alter_table recreation (rename -> create-with-constraint -> copy ->
drop), same as 0068. All existing rows satisfy the predicate (the five
violating rows were repaired in place on 2026-06-10, pre-#368-merge).

Revision ID: 0076_dcf_runs_over_under_check
Revises: 0075_locator_schema
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0076_dcf_runs_over_under_check"
down_revision: str | Sequence[str] | None = "0075_locator_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep in lockstep with src/dcf/persist.derive_over_under (the Python producer)
# and src/dcf/valuation.over_under_pct (the convention's definition).
_RATIO_CHECK = (
    "over_under_pct IS NULL"
    " OR live_price IS NULL OR npv_per_share IS NULL"
    " OR live_price <= 0 OR npv_per_share <= 0"
    " OR ABS(over_under_pct - (1.0 * live_price / npv_per_share - 1.0)) <= 0.005"
)


def _has_table(inspector: sa.Inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "dcf_runs"):
        return
    with op.batch_alter_table("dcf_runs") as batch:
        batch.create_check_constraint("ck_dcf_runs_over_under_ratio", _RATIO_CHECK)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "dcf_runs"):
        return
    with op.batch_alter_table("dcf_runs") as batch:
        batch.drop_constraint("ck_dcf_runs_over_under_ratio", type_="check")
