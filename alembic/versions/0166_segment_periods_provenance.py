"""segment_periods -- additive provenance columns (docs/design/
segment_quarterly_framework.md §4.2, Phase 1).

Sibling to 0165's segment_dimensions additions. Plain ``op.add_column``
only -- see 0165's docstring for the batch_alter_table / trigger-drop
rationale (segment_periods itself carries no known triggers today, but the
project-wide rule from 0165 is "never batch_alter_table this schema family,
period" so this migration follows the same mechanics for consistency).

Columns:
  * period_basis TEXT NOT NULL DEFAULT 'discrete'
      'discrete' | 'cumulative' | 'derived'. Every existing row today is
      either a reported-discrete quarter or an FY-annual row -- there are NO
      existing cumulative rows (H1/H2/9M cumulative columns were never
      persisted before this framework), so the 'discrete' default carries no
      backfill ambiguity: it only ever lands on genuinely-discrete legacy
      rows.
  * raw_period_label TEXT NULL
      The exact as-filed column header (e.g. "Six Months Ended June 30,
      2025") -- audit trail back to the literal filing text, independent of
      the parsed period_end/fiscal_period_type. NULL for every pre-framework
      row (the label was never captured).
  * method_version TEXT NULL
      Resolver version tag, e.g. "period_axis_v1". NULL for pre-framework
      rows -- "genuinely unknown," matching 0165's column-for-column
      convention (no coarse backfill label here since segment_periods
      never distinguished a period-resolution method before this framework).

Revision ID: 0166_segment_periods_provenance
Revises: 0165_segment_dimensions_provenance
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0166_segment_periods_provenance"
down_revision: str | Sequence[str] | None = "0165_segment_dimensions_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "segment_periods"


def _has_column(insp: sa.Inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return

    if not _has_column(insp, _TABLE, "period_basis"):
        op.add_column(
            _TABLE,
            sa.Column(
                "period_basis", sa.String(length=16), nullable=False, server_default="discrete"
            ),
        )
    if not _has_column(insp, _TABLE, "raw_period_label"):
        op.add_column(_TABLE, sa.Column("raw_period_label", sa.Text(), nullable=True))
    if not _has_column(insp, _TABLE, "method_version"):
        op.add_column(_TABLE, sa.Column("method_version", sa.String(length=32), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    for column in ("period_basis", "raw_period_label", "method_version"):
        if _has_column(insp, _TABLE, column):
            op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {column}")
