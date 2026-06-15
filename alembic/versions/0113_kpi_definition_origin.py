"""Add ``kpi_definitions.definition_origin`` — who authored the definition.

The "capture every reported number" program (`directives/capture_every_number_program.md`)
widens the numeric extractors to mint a ``kpi_definitions`` row for ANY reported
figure, not just the curated holdings ``tier_*_kpis`` watchlist. Once the long
tail lands alongside the analyst-authored watchlist, a consumer needs to tell the
two apart — badge/filter captured metrics in the DIY picker, scope a coverage
audit to the long tail — without conflating them.

``definition_origin`` is that authoritative per-definition marker:
``'analyst'`` (the curated registry — the 0007 seed + holdings tier KPIs — and the
DEFAULT every existing row backfills to) vs ``'capture'`` (auto-minted by the
capture-all extractors). Set once at row creation; a re-encounter never rewrites
it. See ``models.kpis.DefinitionOrigin``.

The column is ``TEXT NOT NULL DEFAULT 'analyst'`` with a named CHECK matching the
``DefinitionOrigin`` StrEnum. Upgrade uses a plain ``ALTER TABLE ... ADD COLUMN``
(SQLite admits a NOT-NULL column with a constant default plus a named column-level
CHECK) rather than a ``batch_alter_table`` recreation — ``kpi_definitions`` is
referenced by ``kpi_facts.kpi_definition_id`` and a table rebuild on the common
path is both unnecessary and FK-risky here. This mirrors 0072 exactly (the
reporting_cadence column). Downgrade drops the column via batch (SQLite's DROP
COLUMN restrictions), dropping the named CHECK in the same block.

Revision ID: 0113_kpi_definition_origin
Revises: 0112_global_dcf_assumptions
Create Date: 2026-06-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0113_kpi_definition_origin"
down_revision: str | Sequence[str] | None = "0112_global_dcf_assumptions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep in lockstep with models.kpis.DefinitionOrigin.
_ORIGIN_VOCAB = "('analyst', 'capture')"


def _has_table(inspector: sa.Inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _has_column(inspector: sa.Inspector, table: str, column: str) -> bool:
    """True iff `table` exists AND carries `column`. Table-absence-tolerant: many
    test fixtures run `upgrade head` against a partial DB that never created
    kpi_definitions, mirroring 0072's `_has_column` guard — a bare get_columns on a
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
        insp, "kpi_definitions", "definition_origin"
    ):
        op.execute(
            "ALTER TABLE kpi_definitions ADD COLUMN definition_origin TEXT NOT NULL "
            "DEFAULT 'analyst' CONSTRAINT ck_kpi_definitions_definition_origin "
            f"CHECK (definition_origin IN {_ORIGIN_VOCAB})"
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_column(insp, "kpi_definitions", "definition_origin"):
        # The column carries a named CHECK referencing it; the batch recreate must
        # drop the constraint in the same block, else the rebuilt table would keep
        # a CHECK on a now-absent column (mirrors 0072's downgrade).
        with op.batch_alter_table("kpi_definitions") as batch:
            batch.drop_constraint("ck_kpi_definitions_definition_origin", type_="check")
            batch.drop_column("definition_origin")
