"""segment_dimensions -- additive provenance columns bringing the table to
kpi_facts parity (docs/design/segment_quarterly_framework.md §4.1, Phase 1).

`segment_dimensions` (migration 0055, extended 0057) never got the same
provenance treatment kpi_facts/financial_facts already carry (confidence
0054, extracted_by/locator 0075, computed_from 0087, supersedes_id
restatement chaining). This migration is backfill-to-parity, not invention
of new provenance concepts -- every column here mirrors an existing,
already-shipped kpi_facts column column-for-column.

Plain ``op.add_column`` only -- NEVER ``batch_alter_table`` on this table.
segment_dimensions already carries two non-FTS triggers from 0057
(``trg_dirty_segment_dimensions_ins``, ``trg_artifacts_dirty_on_segment_
dimensions_insert``); SQLite's batch-mode rebuild (CREATE TABLE ... AS
SELECT + rename) drops EVERY trigger on the original table as a side
effect, FTS or not (the 0128 gotcha's general lesson, not an FTS-specific
one). ``op.add_column`` is a native ``ALTER TABLE ... ADD COLUMN`` -- no
table rebuild, triggers untouched.

Columns:
  * disclosure_status TEXT NOT NULL DEFAULT 'reported'
      'reported' | 'derived'. Existing rows are reported-as-filed.
  * method_version TEXT NULL
      One-time backfill (this migration's data step) tags every PRE-EXISTING
      row with the writer that produced it, so a NULL going forward means
      "genuinely unknown," not "predates this migration": FMP 1-axis rows
      (compute/segments.py) -> 'fmp_segment_v1'; everything else (10-K
      segment-OI heuristic, LLM cross-tabs, NU's bespoke extractor) ->
      'legacy_pre_framework' (a coarser catch-all -- the writer isn't
      reliably distinguishable per-row from the DB alone, and the point of
      backfilling is only to make "NULL = unknown" true going forward, not
      to retroactively reconstruct exact per-row lineage that was never
      recorded).
  * confidence REAL NOT NULL DEFAULT 1.0
      Mirrors financial_facts/kpi_facts.confidence. Existing rows are
      deterministic reported-as-filed parses -> 1.0, consistent with those
      tables' own historical default.
  * extracted_by TEXT NULL -- deterministic tag or "llm:<model>".
  * locator TEXT NULL -- FactLocator-shaped JSON (section, json_path).
  * derived_from TEXT NULL -- kpi_facts.computed_from-shaped JSON.
  * supersedes_id INTEGER NULL -- recast chain link, self-referencing
      segment_dimensions.id. Code-validated, NOT a DB-level FK (the
      FK-poisoning invariant -- reference_platform_invariants.md).

Revision ID: 0165_segment_dimensions_provenance
Revises: 0164_tracked_companies_accounting_standard
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0165_segment_dimensions_provenance"
down_revision: str | Sequence[str] | None = "0164_tracked_companies_accounting_standard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "segment_dimensions"


def _has_column(insp: sa.Inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return

    if not _has_column(insp, _TABLE, "disclosure_status"):
        op.add_column(
            _TABLE,
            sa.Column(
                "disclosure_status", sa.String(length=16), nullable=False, server_default="reported"
            ),
        )
    if not _has_column(insp, _TABLE, "method_version"):
        op.add_column(_TABLE, sa.Column("method_version", sa.String(length=32), nullable=True))
    if not _has_column(insp, _TABLE, "confidence"):
        op.add_column(
            _TABLE, sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0")
        )
    if not _has_column(insp, _TABLE, "extracted_by"):
        op.add_column(_TABLE, sa.Column("extracted_by", sa.String(length=64), nullable=True))
    if not _has_column(insp, _TABLE, "locator"):
        op.add_column(_TABLE, sa.Column("locator", sa.Text(), nullable=True))
    if not _has_column(insp, _TABLE, "derived_from"):
        op.add_column(_TABLE, sa.Column("derived_from", sa.Text(), nullable=True))
    if not _has_column(insp, _TABLE, "supersedes_id"):
        op.add_column(_TABLE, sa.Column("supersedes_id", sa.Integer(), nullable=True))

    # One-time backfill: distinguish "predates this migration" from
    # "genuinely unknown" for method_version. Only touches rows this
    # migration itself just defaulted to NULL.
    op.execute(
        """
        UPDATE segment_dimensions
        SET method_version = CASE
            WHEN metric IN ('revenue_by_product', 'revenue_by_geography')
                THEN 'fmp_segment_v1'
            ELSE 'legacy_pre_framework'
        END
        WHERE method_version IS NULL
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    for column in (
        "disclosure_status",
        "method_version",
        "confidence",
        "extracted_by",
        "locator",
        "derived_from",
        "supersedes_id",
    ):
        if _has_column(insp, _TABLE, column):
            # Raw SQL, NOT op.batch_alter_table -- see the module docstring;
            # batch mode would drop segment_dimensions' 0057 triggers.
            op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {column}")
