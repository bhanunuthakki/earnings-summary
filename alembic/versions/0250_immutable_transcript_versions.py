"""Preserve immutable transcript versions with one current period winner.

Revision ID: 0250_immutable_transcript_versions
Revises: 0249_embedding_runtime_artifact_binding
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0250_immutable_transcript_versions"
down_revision: str | Sequence[str] | None = "0249_embedding_runtime_artifact_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "transcripts"
_LEGACY_UNIQUE = "uq_transcripts_ticker_period_type_end"
_CURRENT_UNIQUE = "uq_transcripts_current_ticker_period_type_end"
_LIFECYCLE_INDEX = "ix_transcripts_period_version"
_TRIGGERS = (
    "trg_transcripts_lifecycle_insert",
    "trg_transcripts_lifecycle_update",
    "trg_transcripts_immutable_evidence",
    "trg_transcripts_source_once",
    "trg_transcripts_no_delete",
    "trg_transcript_segments_immutable",
    "trg_transcript_segments_no_delete",
    "trg_transcript_documents_immutable",
    "trg_transcript_documents_no_delete",
)


def _columns(inspector: sa.Inspector, table: str) -> set[str]:
    return {str(column["name"]) for column in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if _TABLE not in tables:
        return

    indexes = {str(index["name"]) for index in inspector.get_indexes(_TABLE)}
    if _LEGACY_UNIQUE in indexes:
        op.drop_index(_LEGACY_UNIQUE, table_name=_TABLE)

    columns = _columns(inspector, _TABLE)
    additions: tuple[tuple[str, sa.Column[object]], ...] = (
        (
            "version_number",
            sa.Column("version_number", sa.Integer(), nullable=False, server_default="1"),
        ),
        (
            "is_current",
            sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        ),
        (
            "recorded_at",
            sa.Column("recorded_at", sa.DateTime(), nullable=True),
        ),
        (
            "superseded_at",
            sa.Column("superseded_at", sa.DateTime(), nullable=True),
        ),
        (
            "superseded_by_transcript_id",
            sa.Column("superseded_by_transcript_id", sa.Integer(), nullable=True),
        ),
    )
    for name, column in additions:
        if name not in columns:
            op.add_column(_TABLE, column)

    op.execute(
        "UPDATE transcripts SET recorded_at = COALESCE("
        "(SELECT fetched_at FROM documents WHERE documents.id = transcripts.document_id), "
        "CURRENT_TIMESTAMP) WHERE recorded_at IS NULL"
    )

    # Normalize any rows from a partially migrated or pre-0209 database before
    # enforcing one current winner. The oldest observed evidence is version 1;
    # the newest (with id as the deterministic tie-breaker) is current.
    rows = bind.execute(
        sa.text(
            "SELECT id, ticker, fiscal_period_type, period_end, recorded_at "
            "FROM transcripts ORDER BY ticker, fiscal_period_type, period_end, "
            "recorded_at, id"
        )
    ).fetchall()
    groups: dict[tuple[object, object, object], list[tuple[int, str]]] = {}
    for row in rows:
        key = (row[1], row[2], row[3])
        groups.setdefault(key, []).append((int(row[0]), str(row[4] or "")))
    for members in groups.values():
        members.sort(key=lambda item: (item[1], item[0]))
        winner_id = members[-1][0]
        for version_number, (transcript_id, _recorded_at) in enumerate(members, start=1):
            is_current = transcript_id == winner_id
            bind.execute(
                sa.text(
                    "UPDATE transcripts SET version_number = :version_number, "
                    "is_current = :is_current, "
                    "superseded_at = CASE WHEN :is_current = 1 THEN NULL "
                    "ELSE CURRENT_TIMESTAMP END, "
                    "superseded_by_transcript_id = CASE WHEN :is_current = 1 THEN NULL "
                    "ELSE :winner_id END WHERE id = :transcript_id"
                ),
                {
                    "version_number": version_number,
                    "is_current": int(is_current),
                    "winner_id": winner_id,
                    "transcript_id": transcript_id,
                },
            )

    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_CURRENT_UNIQUE} "
        "ON transcripts(ticker, fiscal_period_type, period_end) "
        "WHERE is_current = 1"
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_LIFECYCLE_INDEX} "
        "ON transcripts(ticker, fiscal_period_type, period_end, version_number)"
    )

    lifecycle_invalid = (
        "(NEW.is_current NOT IN (0, 1)) "
        "OR (NEW.is_current = 1 AND "
        "(NEW.superseded_at IS NOT NULL OR NEW.superseded_by_transcript_id IS NOT NULL)) "
        "OR (NEW.is_current = 0 AND "
        "(NEW.superseded_at IS NULL OR NEW.superseded_by_transcript_id IS NULL)) "
        "OR (NEW.superseded_by_transcript_id = NEW.id) "
        "OR (NEW.superseded_by_transcript_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM transcripts winner "
        "WHERE winner.id = NEW.superseded_by_transcript_id "
        "AND winner.ticker = NEW.ticker "
        "AND winner.fiscal_period_type IS NEW.fiscal_period_type "
        "AND winner.period_end IS NEW.period_end))"
    )
    op.execute(
        "CREATE TRIGGER trg_transcripts_lifecycle_insert "
        "BEFORE INSERT ON transcripts "
        f"WHEN {lifecycle_invalid} "
        "BEGIN SELECT RAISE(ABORT, 'invalid transcript lifecycle'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_transcripts_lifecycle_update "
        "BEFORE UPDATE OF is_current, superseded_at, superseded_by_transcript_id ON transcripts "
        f"WHEN {lifecycle_invalid} "
        "BEGIN SELECT RAISE(ABORT, 'invalid transcript lifecycle'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_transcripts_immutable_evidence "
        "BEFORE UPDATE OF document_id, ticker, call_date, fiscal_period_type, period_end, "
        "source_url, has_qa_section, version_number, recorded_at ON transcripts "
        "BEGIN SELECT RAISE(ABORT, 'transcript evidence versions are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_transcripts_source_once "
        "BEFORE UPDATE OF source ON transcripts "
        "WHEN OLD.source IS NOT NULL OR NEW.source IS NULL "
        "BEGIN SELECT RAISE(ABORT, 'transcript source provenance is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_transcripts_no_delete BEFORE DELETE ON transcripts "
        "BEGIN SELECT RAISE(ABORT, 'transcript evidence versions cannot be deleted'); END"
    )
    if "transcript_segments" in tables:
        op.execute(
            "CREATE TRIGGER trg_transcript_segments_immutable "
            "BEFORE UPDATE ON transcript_segments "
            "BEGIN SELECT RAISE(ABORT, 'transcript segments are immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_transcript_segments_no_delete "
            "BEFORE DELETE ON transcript_segments "
            "BEGIN SELECT RAISE(ABORT, 'transcript segments cannot be deleted'); END"
        )
    if "documents" in tables:
        op.execute(
            "CREATE TRIGGER trg_transcript_documents_immutable "
            "BEFORE UPDATE OF file_path, sha256 ON documents "
            "WHEN EXISTS (SELECT 1 FROM transcripts WHERE document_id = OLD.id) "
            "BEGIN SELECT RAISE(ABORT, 'transcript document evidence is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_transcript_documents_no_delete BEFORE DELETE ON documents "
            "WHEN EXISTS (SELECT 1 FROM transcripts WHERE document_id = OLD.id) "
            "BEGIN SELECT RAISE(ABORT, 'transcript documents cannot be deleted'); END"
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return

    for trigger in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    indexes = {str(index["name"]) for index in inspector.get_indexes(_TABLE)}
    for index in (_LIFECYCLE_INDEX, _CURRENT_UNIQUE):
        if index in indexes:
            op.drop_index(index, table_name=_TABLE)

    # Historical rows deliberately remain. Restoring the pre-0250 strict
    # period-unique index would require deleting those evidence versions.
    columns = _columns(sa.inspect(bind), _TABLE)
    for name in (
        "superseded_by_transcript_id",
        "superseded_at",
        "recorded_at",
        "is_current",
        "version_number",
    ):
        if name in columns:
            op.drop_column(_TABLE, name)
