"""kpi_facts_restatement — relax uq_kpi_facts_logical so the restatement chain can populate.

Why this exists
---------------
PR #152 (P0-1) wired the restatement detector into `financial_facts`: when a
later filing (e.g. a 10-K restating a value first reported in a 10-Q) lands
on an existing logical key, the new row is written with `supersedes_id`
pointing at the incumbent. Both rows survive and the tier+id-aware loader
in `src/timeseries/loaders.py` picks the newer value by default while
`--as-of-date` lets a caller reproduce the historical view.

`kpi_facts` got the audit columns in 0054 (confidence + extracted_by +
supersedes_id) but the chain has been dormant because `uq_kpi_facts_logical`
— added in 0030 to collapse same-logical-key duplicates after a renderer
bug double-counted them — forbids multiple rows for the same
(ticker, period_end, fiscal_period_type, kpi_definition_id) tuple. This
migration is the kpi-side symmetry of the financial-facts treatment: drop
the narrow unique index and recreate the wider provenance index that
includes `source_doc_id`, mirroring `uq_financial_facts_provenance`. Two
rows for the same logical key can now coexist as long as they come from
different documents — exactly the shape the restatement chain needs.

What about the 0030 bug it was fixing?
--------------------------------------
0030 fixed a double-counting bug in `src/compute/thesis_evaluator.py`'s
`_fetch_kpi_history` which was reading both source-doc rows and emitting
each observation twice. That code path now goes through the tier-aware
loader (`load_kpi_series`), whose correlated-subquery ranking by
(source_quality_tier, id) DESC collapses multiple rows per logical key
to a single winner before the renderer ever sees them. So the original
double-counting motivation is handled at the loader layer, not at the
constraint layer — relaxing the constraint here doesn't re-open the bug.

Schema-tolerance note
---------------------
`uq_kpi_facts_logical` was created as a regular INDEX in 0030 (not a
UNIQUE constraint embedded in CREATE TABLE), so `op.drop_index` works
directly under SQLite. No table rebuild required.

Revision ID: 0059_kpi_facts_restatement
Revises: 0058_prompt_calibration_scores
Create Date: 2026-05-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0059_kpi_facts_restatement"
down_revision: str | Sequence[str] | None = "0058_prompt_calibration_scores"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {idx["name"] for idx in inspector.get_indexes("kpi_facts")}

    if "uq_kpi_facts_logical" in existing:
        op.drop_index("uq_kpi_facts_logical", table_name="kpi_facts")

    if "uq_kpi_facts_provenance" not in existing:
        op.create_index(
            "uq_kpi_facts_provenance",
            "kpi_facts",
            ["ticker", "period_end", "fiscal_period_type", "kpi_definition_id", "source_doc_id"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {idx["name"] for idx in inspector.get_indexes("kpi_facts")}

    # Downgrade rebuilds the post-0030 narrow constraint. If any same-logical-
    # key duplicates were inserted between upgrade() and downgrade() (e.g. a
    # restatement chain), the CREATE UNIQUE INDEX will fail — call the same
    # purge helper 0030 used. Imported lazily so the migration file stays
    # importable when src/ is not on sys.path.
    if "uq_kpi_facts_provenance" in existing:
        op.drop_index("uq_kpi_facts_provenance", table_name="kpi_facts")

    if "uq_kpi_facts_logical" not in existing:
        from pipeline.kpi_persistence import purge_duplicate_kpi_facts

        purge_duplicate_kpi_facts(bind.connection)
        op.create_index(
            "uq_kpi_facts_logical",
            "kpi_facts",
            ["ticker", "period_end", "fiscal_period_type", "kpi_definition_id"],
            unique=True,
        )
