"""Check exact lexical coverage with one grouped FTS scan.

The prior guard rescans an UNINDEXED FTS column for every corpus chunk.
Replace only that guard, atomically, without changing retained rows or seals.
"""

from __future__ import annotations

from alembic import op

revision = "0061_bulk_lexical_projection_coverage"
down_revision = "0060_owner_governed_proposals"
branch_labels = None
depends_on = None

_TRIGGER = "trg_search_projection_seals_lexical_coverage"
_LEGACY_SQL = (
    f"CREATE TRIGGER {_TRIGGER} BEFORE INSERT ON search_projection_seals "
    "WHEN NEW.index_kind = 'lexical' AND EXISTS ("
    "SELECT 1 FROM search_chunks AS chunk WHERE chunk.manifest_id = NEW.manifest_id "
    "AND ((SELECT COUNT(*) FROM search_lexical_chunks AS lexical "
    "WHERE lexical.chunk_id = chunk.chunk_id) <> 1 "
    "OR NOT EXISTS (SELECT 1 FROM search_lexical_chunks AS lexical "
    "WHERE lexical.chunk_id = chunk.chunk_id AND lexical.text = chunk.text))) "
    "BEGIN SELECT RAISE(ABORT, 'lexical projection seal requires exact FTS rows'); END"
)
_BULK_SQL = (
    f"CREATE TRIGGER {_TRIGGER} BEFORE INSERT ON search_projection_seals "
    "WHEN NEW.index_kind = 'lexical' AND EXISTS ("
    "SELECT 1 FROM search_chunks AS chunk LEFT JOIN ("
    "SELECT chunk_id, COUNT(*) AS row_count, MIN(text) AS min_text, "
    "MAX(text) AS max_text FROM search_lexical_chunks GROUP BY chunk_id"
    ") AS lexical ON lexical.chunk_id = chunk.chunk_id "
    "WHERE chunk.manifest_id = NEW.manifest_id "
    "AND (lexical.row_count IS NULL OR lexical.row_count <> 1 "
    "OR lexical.min_text IS NOT chunk.text OR lexical.max_text IS NOT chunk.text)) "
    "BEGIN SELECT RAISE(ABORT, 'lexical projection seal requires exact FTS rows'); END"
)


def _normalized(sql: str) -> str:
    return " ".join(
        sql.replace("CREATE TRIGGER IF NOT EXISTS ", "CREATE TRIGGER ", 1)
        .strip()
        .removesuffix(";")
        .split()
    )


def _replace(expected: str, desired: str) -> None:
    bind = op.get_bind()
    row = bind.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (_TRIGGER,)
    ).fetchone()
    if row is None:
        raise RuntimeError("lexical coverage trigger is missing; refusing replacement")
    current = _normalized(str(row[0]))
    if current == _normalized(desired):
        return
    if current != _normalized(expected):
        raise RuntimeError("lexical coverage trigger differs from its migration owner")
    with bind.begin_nested():
        op.execute(f"DROP TRIGGER {_TRIGGER}")
        op.execute(desired)


def upgrade() -> None:
    _replace(_LEGACY_SQL, _BULK_SQL)


def downgrade() -> None:
    _replace(_BULK_SQL, _LEGACY_SQL)
