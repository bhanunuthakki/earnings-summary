"""facts_concept_fks — bind existing fact tables to the new entity/concept spine.

Adds nullable FK columns:
  kpi_facts.concept_id              → concepts(id)
  kpi_facts.source_excerpt_entity   → entity_aliases / entity_mentions (loose ref)
  segment_facts.segment_entity_id   → entities(id) where kind='segment'
  management_commitments.kpi_concept_id → concepts(id)
  financial_facts.line_item_concept_id → concepts(id)

All columns nullable so backfill can be incremental + the existing pipeline
keeps working off the original string columns until backfill completes. New
writers populate the FK at insert time; old rows get backfilled by a one-shot
script.

Revision ID: 0037_facts_concept_fks
Revises: 0036_entity_concept_spine
Create Date: 2026-05-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0037_facts_concept_fks"
down_revision: str | Sequence[str] | None = "0036_entity_concept_spine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_if_missing(table: str, col: sa.Column[object]) -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns(table)}
    if col.name not in cols:
        op.add_column(table, col)


def upgrade() -> None:
    # No FK constraint enforcement at the SQLite level — these are LOOSE
    # references because alembic's add_column on SQLite uses ALTER TABLE which
    # can't add a foreign key. The columns are documented as FKs in code and
    # in tests; integrity is maintained at the writer layer.
    _add_if_missing("kpi_facts", sa.Column("concept_id", sa.Integer(), nullable=True))
    _add_if_missing(
        "segment_facts", sa.Column("segment_entity_id", sa.Integer(), nullable=True)
    )
    _add_if_missing(
        "management_commitments",
        sa.Column("kpi_concept_id", sa.Integer(), nullable=True),
    )
    _add_if_missing(
        "financial_facts",
        sa.Column("line_item_concept_id", sa.Integer(), nullable=True),
    )

    # Indexes for the new join columns
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    def _index_if_missing(name: str, table: str, cols: list[str]) -> None:
        existing = {idx["name"] for idx in inspector.get_indexes(table)}
        if name not in existing:
            op.create_index(name, table, cols)

    _index_if_missing("idx_kpi_facts_concept", "kpi_facts", ["concept_id"])
    _index_if_missing("idx_segment_facts_entity", "segment_facts", ["segment_entity_id"])
    _index_if_missing(
        "idx_commitments_concept", "management_commitments", ["kpi_concept_id"]
    )
    _index_if_missing(
        "idx_financial_facts_concept", "financial_facts", ["line_item_concept_id"]
    )


def downgrade() -> None:
    for index, _table in [
        ("idx_financial_facts_concept", "financial_facts"),
        ("idx_commitments_concept", "management_commitments"),
        ("idx_segment_facts_entity", "segment_facts"),
        ("idx_kpi_facts_concept", "kpi_facts"),
    ]:
        op.execute(f"DROP INDEX IF EXISTS {index}")
    for table, col in [
        ("financial_facts", "line_item_concept_id"),
        ("management_commitments", "kpi_concept_id"),
        ("segment_facts", "segment_entity_id"),
        ("kpi_facts", "concept_id"),
    ]:
        with op.batch_alter_table(table) as batch:
            batch.drop_column(col)
