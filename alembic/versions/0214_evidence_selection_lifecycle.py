"""Preserve transcript and filing evidence while selecting one active version.

The former replacement paths destroyed losing transcript documents/segments and
filing-section partitions. This migration adds lifecycle metadata, converts the
two full uniqueness rules into active-only partial uniqueness, and keeps row IDs
stable while rebuilding ``filing_sections`` for SQLite.

Revision ID: 0214_evidence_selection_lifecycle
Revises: 0213_evidence_ledger_foundation
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0214_evidence_selection_lifecycle"
down_revision: str | Sequence[str] | None = "0213_evidence_ledger_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRANSCRIPTS = "transcripts"
_FILING_SECTIONS = "filing_sections"
_TRANSCRIPT_PERIOD_INDEX = "uq_transcripts_ticker_period_type_end"
_ACTIVE_TRANSCRIPT_PERIOD_INDEX = "uq_transcripts_active_ticker_period_type_end"
_ACTIVE_FILING_SECTION_INDEX = "uq_filing_sections_active_key"
_FILING_NATURAL_KEY = "source, source_ref, section_key_raw, ordinal"
_TRANSCRIPT_PERIOD_COLUMNS = {"ticker", "fiscal_period_type", "period_end"}


def _columns(bind: sa.Connection, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _index_names(bind: sa.Connection, table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def _dependent_views(bind: sa.Connection, table: str) -> list[str]:
    if bind.dialect.name != "sqlite":
        return []
    rows = bind.execute(
        sa.text(
            "SELECT name FROM sqlite_master WHERE type = 'view' "
            "AND lower(sql) LIKE :needle"
        ),
        {"needle": f"%{table.lower()}%"},
    )
    return [str(row[0]) for row in rows]


def _assert_no_dependent_views(bind: sa.Connection, table: str) -> None:
    views = _dependent_views(bind, table)
    if views:
        raise RuntimeError(
            f"Cannot rebuild {table} while dependent SQLite view(s) exist: {views}. "
            "Preserve and recreate those views in a coordinated migration first."
        )


def _assert_downgrade_has_no_history(bind: sa.Connection, table: str) -> None:
    row = bind.execute(
        sa.text(
            f"SELECT 1 FROM {table} WHERE is_active = 0 OR superseded_by_id IS NOT NULL "
            "OR superseded_at IS NOT NULL LIMIT 1"
        )
    ).first()
    if row is not None:
        raise RuntimeError(
            f"Cannot downgrade 0214: {table} contains preserved lifecycle history. "
            "Downgrading would destroy evidence versions."
        )


def _filing_sections_without_natural_unique(bind: sa.Connection) -> sa.Table:
    """Reflect the source table while removing its SQLite-only embedded unique key."""
    table = sa.Table(_FILING_SECTIONS, sa.MetaData(), autoload_with=bind)
    natural_key = tuple(_FILING_NATURAL_KEY.split(", "))
    for constraint in tuple(table.constraints):
        if isinstance(constraint, sa.UniqueConstraint) and tuple(constraint.columns.keys()) == natural_key:
            table.constraints.remove(constraint)
            break
    else:
        raise RuntimeError("filing_sections natural-key unique constraint is missing")
    return table


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if _TRANSCRIPTS in existing:
        transcript_columns = _columns(bind, _TRANSCRIPTS)
        if "is_active" not in transcript_columns:
            op.add_column(
                _TRANSCRIPTS,
                sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            )
        if "superseded_by_id" not in transcript_columns:
            op.add_column(_TRANSCRIPTS, sa.Column("superseded_by_id", sa.Integer(), nullable=True))
        if "superseded_at" not in transcript_columns:
            op.add_column(_TRANSCRIPTS, sa.Column("superseded_at", sa.DateTime(), nullable=True))
        if "selection_reason" not in transcript_columns:
            op.add_column(_TRANSCRIPTS, sa.Column("selection_reason", sa.Text(), nullable=True))

        transcript_indexes = _index_names(bind, _TRANSCRIPTS)
        if _TRANSCRIPT_PERIOD_COLUMNS <= transcript_columns:
            if _TRANSCRIPT_PERIOD_INDEX in transcript_indexes:
                op.drop_index(_TRANSCRIPT_PERIOD_INDEX, table_name=_TRANSCRIPTS)
            if _ACTIVE_TRANSCRIPT_PERIOD_INDEX not in transcript_indexes:
                op.execute(
                    "CREATE UNIQUE INDEX uq_transcripts_active_ticker_period_type_end "
                    "ON transcripts (ticker, fiscal_period_type, period_end) WHERE is_active = 1"
                )
        op.execute("DROP VIEW IF EXISTS v_active_transcripts")
        op.execute("CREATE VIEW v_active_transcripts AS SELECT * FROM transcripts WHERE is_active = 1")

    if _FILING_SECTIONS in existing:
        filing_columns = _columns(bind, _FILING_SECTIONS)
        lifecycle_columns = {
            "is_active",
            "superseded_by_id",
            "superseded_at",
            "retirement_reason",
        }
        if not lifecycle_columns <= filing_columns:
            _assert_no_dependent_views(bind, _FILING_SECTIONS)
            source_table = _filing_sections_without_natural_unique(bind)
            with op.batch_alter_table(
                _FILING_SECTIONS, recreate="always", copy_from=source_table
            ) as batch:
                if "is_active" not in filing_columns:
                    batch.add_column(
                        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true())
                    )
                if "superseded_by_id" not in filing_columns:
                    batch.add_column(sa.Column("superseded_by_id", sa.Integer(), nullable=True))
                if "superseded_at" not in filing_columns:
                    batch.add_column(sa.Column("superseded_at", sa.DateTime(), nullable=True))
                if "retirement_reason" not in filing_columns:
                    batch.add_column(sa.Column("retirement_reason", sa.Text(), nullable=True))

        filing_indexes = _index_names(bind, _FILING_SECTIONS)
        if _ACTIVE_FILING_SECTION_INDEX not in filing_indexes:
            op.execute(
                "CREATE UNIQUE INDEX uq_filing_sections_active_key "
                f"ON filing_sections ({_FILING_NATURAL_KEY}) WHERE is_active = 1"
            )
        op.execute("DROP VIEW IF EXISTS v_active_filing_sections")
        op.execute(
            "CREATE VIEW v_active_filing_sections AS "
            "SELECT * FROM filing_sections WHERE is_active = 1"
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    op.execute("DROP VIEW IF EXISTS v_active_filing_sections")
    op.execute("DROP VIEW IF EXISTS v_active_transcripts")

    if _FILING_SECTIONS in existing:
        filing_columns = _columns(bind, _FILING_SECTIONS)
        if "is_active" in filing_columns:
            _assert_downgrade_has_no_history(bind, _FILING_SECTIONS)
            _assert_no_dependent_views(bind, _FILING_SECTIONS)
            filing_indexes = _index_names(bind, _FILING_SECTIONS)
            if _ACTIVE_FILING_SECTION_INDEX in filing_indexes:
                op.drop_index(_ACTIVE_FILING_SECTION_INDEX, table_name=_FILING_SECTIONS)
            with op.batch_alter_table(_FILING_SECTIONS, recreate="always") as batch:
                batch.drop_column("retirement_reason")
                batch.drop_column("superseded_at")
                batch.drop_column("superseded_by_id")
                batch.drop_column("is_active")
                batch.create_unique_constraint("uq_filing_sections_key", _FILING_NATURAL_KEY.split(", "))

    if _TRANSCRIPTS in existing:
        transcript_columns = _columns(bind, _TRANSCRIPTS)
        if "is_active" in transcript_columns:
            _assert_downgrade_has_no_history(bind, _TRANSCRIPTS)
            transcript_indexes = _index_names(bind, _TRANSCRIPTS)
            if _ACTIVE_TRANSCRIPT_PERIOD_INDEX in transcript_indexes:
                op.drop_index(_ACTIVE_TRANSCRIPT_PERIOD_INDEX, table_name=_TRANSCRIPTS)
            with op.batch_alter_table(_TRANSCRIPTS, recreate="always") as batch:
                batch.drop_column("selection_reason")
                batch.drop_column("superseded_at")
                batch.drop_column("superseded_by_id")
                batch.drop_column("is_active")
            if _TRANSCRIPT_PERIOD_COLUMNS <= transcript_columns:
                op.create_index(
                    _TRANSCRIPT_PERIOD_INDEX,
                    _TRANSCRIPTS,
                    ["ticker", "fiscal_period_type", "period_end"],
                    unique=True,
                )
