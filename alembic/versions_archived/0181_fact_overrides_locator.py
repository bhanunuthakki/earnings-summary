"""fact_overrides gains a locator column — Phase C of the provenance click-through
program (docs/design/provenance_clickthrough.md §1.3/§3.2's 8-K row).

Additive, nullable ``TEXT`` column carrying a ``FactLocator`` JSON payload (the same
shape/contract as ``financial_facts.locator`` / ``kpi_facts.locator``, alembic 0075) —
NOT a new locator shape. This lets an 8-K-derived override (``execution/
extract_8k_overrides.py``) carry a real, click-through-able ``html_span`` locator
(verified anchor quote into the fetched EX-99.1 exhibit text) the same way a fact row
does, closing the one remaining "no locator column at all" gap
``directives/data_provenance.md`` §7 flagged for this writer.

Not to be confused with ``fact_overrides.source_exhibit`` (an exhibit filename string)
or the ``locator`` KEY that ``provenance.overrides.override_provenance()`` currently
sets on a ``CellSource`` dict (mapped from ``source_exhibit`` today, for display only
before this column existed) — that dict-key behavior is left as a fallback for
overrides written before this column landed; a row that carries a real
``FactLocator`` JSON here is preferred (see ``overrides.override_provenance`` for the
precedence).

Revision ID: 0181_fact_overrides_locator
Revises: 0180_sector_benchmark_proposal_budget
Create Date: 2026-07-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0181_fact_overrides_locator"
down_revision: str | Sequence[str] | None = "0180_sector_benchmark_proposal_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "fact_overrides" not in insp.get_table_names():
        return  # table itself absent (pre-0111 DB); nothing to add a column to
    existing_cols = {c["name"] for c in insp.get_columns("fact_overrides")}
    if "locator" in existing_cols:
        return  # idempotent
    # Plain ADD COLUMN (SQLite supports this natively for a nullable column with
    # no default) -- no batch_alter_table rebuild needed, and this table carries
    # no triggers to disturb (the FTS-trigger footgun this repo's guidance warns
    # about does not apply here).
    op.add_column("fact_overrides", sa.Column("locator", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "fact_overrides" not in insp.get_table_names():
        return
    existing_cols = {c["name"] for c in insp.get_columns("fact_overrides")}
    if "locator" not in existing_cols:
        return
    with op.batch_alter_table("fact_overrides") as batch_op:
        batch_op.drop_column("locator")  # SQLite DROP COLUMN needs the batch rebuild
