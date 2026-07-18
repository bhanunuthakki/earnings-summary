"""segment_quarterly_coverage -- new table: per (ticker, period, segment)
coverage/gap matrix (docs/design/segment_quarterly_framework.md §4.3, Phase 2).

Rationale (from the design doc): a new DB table rather than extending
``pipeline/capture_coverage.py``'s JSON log -- that log suits *run-level*
tallies but isn't queryable at per-(ticker, quarter, segment) cell
granularity, which is exactly the "per-ticker x per-quarter coverage matrix"
the task calls for. It also makes ``not_disclosed``/``not_applicable`` a
first-class recorded outcome without loosening ``segment_dimensions.value``'s
NOT NULL constraint (which would force a ``batch_alter_table`` rebuild and
the 0057-trigger-drop risk 0165/0166 both went out of their way to avoid).

Deviation from the design doc's literal DDL sketch: the doc's §4.3 sketch
writes ``source_doc_id INTEGER REFERENCES documents(id)`` (a real FK). This
migration uses a plain ``INTEGER`` with NO database-level foreign key --
per this repo's FK-poisoning invariant (reference_platform_invariants.md)
and this framework's own Phase 2 coordination note ("plain INTEGER + code
validation, no FKs"), matching ``segment_dimensions.supersedes_id``'s
existing code-validated-not-DB-FK precedent from migration 0165. Readers
that need referential validation do so in Python, the same way
``segment_dimensions.supersedes_id`` consumers already do.

Columns (verbatim from §4.3's schema table):
  * ticker VARCHAR(16) NOT NULL
  * period_end DATETIME NOT NULL
  * fiscal_period_type VARCHAR(8) NOT NULL
  * dim_type VARCHAR(16) NULL -- NULL = whole-filing-level gap
  * dim_name VARCHAR(128) NULL -- NULL = whole-filing-level gap
  * status VARCHAR(24) NOT NULL -- reported | derived | not_disclosed |
      not_applicable | unresolved_period_axis | source_missing |
      tolerance_breach | not_computable
  * reason_code VARCHAR(64) NULL
  * source_doc_id INTEGER NULL -- code-validated pointer to documents.id, see
      deviation note above
  * method_version VARCHAR(32) NULL
  * checked_at DATETIME NOT NULL

UNIQUE (ticker, period_end, fiscal_period_type, dim_type, dim_name,
method_version) is the upsert key (§4.3) -- re-running the audit for the
same cell updates status/reason_code in place. This is a brand-new table
(``op.create_table``, not ``op.add_column``/``batch_alter_table``), so the
0165/0166 trigger-preservation concern does not apply here.

Revision ID: 0168_segment_quarterly_coverage
Revises: 0167_segment_10q_disambiguate_budget
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0168_segment_quarterly_coverage"
down_revision: str | Sequence[str] | None = "0167_segment_10q_disambiguate_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "segment_quarterly_coverage"
_UQ_NAME = "uq_segment_qcov_key"
_IX_NAME = "ix_segment_qcov_ticker_period"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE in insp.get_table_names():
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("fiscal_period_type", sa.String(length=8), nullable=False),
        sa.Column("dim_type", sa.String(length=16), nullable=True),
        sa.Column("dim_name", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        # Code-validated pointer to documents.id -- NOT a DB-level FK. See the
        # module docstring's deviation note.
        sa.Column("source_doc_id", sa.Integer(), nullable=True),
        sa.Column("method_version", sa.String(length=32), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "ticker",
            "period_end",
            "fiscal_period_type",
            "dim_type",
            "dim_name",
            "method_version",
            name=_UQ_NAME,
        ),
    )
    op.create_index(_IX_NAME, _TABLE, ["ticker", "period_end"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    op.drop_index(_IX_NAME, table_name=_TABLE)
    op.drop_table(_TABLE)
