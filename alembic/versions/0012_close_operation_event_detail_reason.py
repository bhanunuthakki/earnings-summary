"""Close operation-event detail reasons to a neutral persisted vocabulary.

Revision ID: 0012_close_operation_event_detail_reason
Revises: 0011_add_operations_journal
Create Date: 2026-08-14

Downgrade restores the 0011 schema contract, but it cannot restore legacy
detail text discarded by the upgrade. Neutralized values and their recomputed
v2 event hashes therefore remain neutral after downgrade.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from alembic import op

revision = "0012_close_operation_event_detail_reason"
down_revision = "0011_add_operations_journal"
branch_labels = None
depends_on = None

_BACKUP_TABLE = "operation_events_0011_backup"
_COLUMNS = (
    "event_id",
    "operation_id",
    "event_kind",
    "event_sha256",
    "occurred_at",
    "status",
    "exit_code",
    "severity",
    "detail_code",
    "detail_reason",
)
_WITHHELD = "terminal_detail_withheld"
_INDEX_SQL = (
    "CREATE INDEX ix_operation_events_operation_id ON operation_events(operation_id,occurred_at)"
)
_UPDATE_TRIGGER_SQL = (
    "CREATE TRIGGER trg_operation_events_no_update "
    "BEFORE UPDATE ON operation_events BEGIN "
    "SELECT RAISE(ABORT, 'operation_events append-only'); END"
)
_DELETE_TRIGGER_SQL = (
    "CREATE TRIGGER trg_operation_events_no_delete "
    "BEFORE DELETE ON operation_events BEGIN "
    "SELECT RAISE(ABORT, 'operation_events append-only'); END"
)
_TABLE_INFO = (
    ("event_id", "TEXT", 1, None, 1),
    ("operation_id", "TEXT", 1, None, 0),
    ("event_kind", "TEXT", 1, None, 0),
    ("event_sha256", "TEXT", 1, None, 0),
    ("occurred_at", "TEXT", 1, None, 0),
    ("status", "TEXT", 0, None, 0),
    ("exit_code", "INTEGER", 0, None, 0),
    ("severity", "TEXT", 0, None, 0),
    ("detail_code", "TEXT", 0, None, 0),
    ("detail_reason", "TEXT", 0, None, 0),
)

ContractRows = tuple[tuple[object, ...], ...]
ContractQuery = Callable[[str, tuple[object, ...]], ContractRows]


def _table_sql(*, closed: bool) -> str:
    detail_shape = (
        "detail_reason IS NULL OR detail_reason='terminal_detail_withheld'"
        if closed
        else "detail_reason IS NULL OR length(detail_reason) BETWEEN 1 AND 240"
    )
    detail_privacy = (
        "detail_reason IS NULL OR detail_reason='terminal_detail_withheld'"
        if closed
        else """
            detail_reason IS NULL OR (
                detail_reason NOT LIKE '%://%'
                AND lower(detail_reason) NOT LIKE '%secret%'
                AND lower(detail_reason) NOT LIKE '%apikey=%'
                AND lower(detail_reason) NOT LIKE '%api_key=%'
                AND lower(detail_reason) NOT LIKE '%access_token=%'
                AND lower(detail_reason) NOT LIKE '%authorization: bearer%'
            )
        """
    )
    return f"""
        CREATE TABLE operation_events (
            event_id TEXT NOT NULL PRIMARY KEY,
            operation_id TEXT NOT NULL,
            event_kind TEXT NOT NULL,
            event_sha256 TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            status TEXT,
            exit_code INTEGER,
            severity TEXT,
            detail_code TEXT,
            detail_reason TEXT,
            CONSTRAINT fk_operation_event_request FOREIGN KEY(operation_id)
                REFERENCES operation_requests(operation_id),
            CONSTRAINT uq_operation_event_kind UNIQUE(operation_id,event_kind),
            CONSTRAINT ck_operation_event_id CHECK (
                length(event_id)=80 AND substr(event_id,1,16)='operation-event:'
                AND substr(event_id,17) NOT GLOB '*[^0-9a-f]*'
            ),
            CONSTRAINT ck_operation_event_hash CHECK (
                length(event_sha256)=64 AND event_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            CONSTRAINT ck_operation_event_kind CHECK (event_kind IN ('started','terminal')),
            CONSTRAINT ck_operation_event_shape CHECK (
                (event_kind='started' AND status IS NULL AND exit_code IS NULL
                 AND severity IS NULL AND detail_code IS NULL AND detail_reason IS NULL)
                OR
                (event_kind='terminal'
                 AND status IN ('ok','degraded_corpus','partial','failed','skipped_locked',
                                'blocked_schema_drift')
                 AND typeof(exit_code)='integer'
                 AND severity IN ('info','warning','error')
                 AND detail_code='job_' || status
                 AND ({detail_shape}))
            ),
            CONSTRAINT ck_operation_event_detail_privacy CHECK ({detail_privacy}),
            CONSTRAINT ck_operation_event_clock CHECK (
                length(occurred_at) BETWEEN 20 AND 40 AND datetime(occurred_at) IS NOT NULL
            )
        )
    """


def _normalize_sql(value: str) -> str:
    return " ".join(value.replace("IF NOT EXISTS", "").split()).rstrip(";").casefold()


def _event_hash(row: tuple[object, ...], detail_reason: str) -> str:
    payload = "\n".join(
        (
            "operation-event/v2",
            str(row[1]),
            str(row[2]),
            "" if row[5] is None else str(row[5]),
            "" if row[6] is None else str(row[6]),
            "" if row[7] is None else str(row[7]),
            "" if row[8] is None else str(row[8]),
            detail_reason,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def require_operation_events_contract(query: ContractQuery, *, closed: bool) -> None:
    """Fail unless ``query`` observes the exact owned operation-events contract."""

    table_info = tuple(
        (str(row[1]), str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
        for row in query("PRAGMA table_info(operation_events)", ())
    )
    if table_info != _TABLE_INFO or tuple(row[0] for row in table_info) != _COLUMNS:
        raise RuntimeError("operation_events columns do not match migration contract")
    table_rows = query(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='operation_events'", ()
    )
    table_row = None if len(table_rows) != 1 else table_rows[0]
    table_sql = "" if table_row is None or table_row[0] is None else str(table_row[0])
    if _normalize_sql(table_sql) != _normalize_sql(_table_sql(closed=closed)):
        raise RuntimeError("operation_events constraints do not match migration contract")
    foreign_keys = query("PRAGMA foreign_key_list(operation_events)", ())
    expected_foreign_key = (
        0,
        0,
        "operation_requests",
        "operation_id",
        "operation_id",
        "NO ACTION",
        "NO ACTION",
        "NONE",
    )
    if len(foreign_keys) != 1 or tuple(foreign_keys[0]) != expected_foreign_key:
        raise RuntimeError("operation_events foreign key does not match migration contract")
    index_rows = {
        str(row[1]): (int(row[2]), str(row[3]), int(row[4]))
        for row in query("PRAGMA index_list(operation_events)", ())
    }
    if index_rows != {
        "ix_operation_events_operation_id": (0, "c", 0),
        "sqlite_autoindex_operation_events_1": (1, "pk", 0),
        "sqlite_autoindex_operation_events_2": (1, "u", 0),
    }:
        raise RuntimeError("operation_events indexes do not match migration contract")
    explicit_index_rows = query(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='ix_operation_events_operation_id'",
        (),
    )
    index_row = None if len(explicit_index_rows) != 1 else explicit_index_rows[0]
    index_sql = "" if index_row is None or index_row[0] is None else str(index_row[0])
    index_columns = tuple(
        str(row[2]) for row in query("PRAGMA index_info(ix_operation_events_operation_id)", ())
    )
    if index_columns != ("operation_id", "occurred_at") or _normalize_sql(
        index_sql
    ) != _normalize_sql(_INDEX_SQL):
        raise RuntimeError("operation_events index does not match migration contract")
    triggers = {
        str(row[0]): "" if row[1] is None else _normalize_sql(str(row[1]))
        for row in query(
            "SELECT name,sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name='operation_events'",
            (),
        )
    }
    expected_triggers = {
        "trg_operation_events_no_update": _normalize_sql(_UPDATE_TRIGGER_SQL),
        "trg_operation_events_no_delete": _normalize_sql(_DELETE_TRIGGER_SQL),
    }
    if triggers != expected_triggers:
        raise RuntimeError("operation_events triggers do not match migration contract")


def _require_contract(*, closed: bool) -> None:
    bind = op.get_bind()

    def query(sql: str, parameters: tuple[object, ...]) -> ContractRows:
        return tuple(tuple(row) for row in bind.exec_driver_sql(sql, parameters).fetchall())

    require_operation_events_contract(query, closed=closed)


def _rebuild(*, source_closed: bool, target_closed: bool) -> None:
    bind = op.get_bind()
    _require_contract(closed=source_closed)
    if (
        bind.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_BACKUP_TABLE,)
        ).fetchone()
        is not None
    ):
        raise RuntimeError(f"refusing to overwrite existing {_BACKUP_TABLE}")
    rows = bind.exec_driver_sql(
        "SELECT event_id,operation_id,event_kind,event_sha256,occurred_at,status,exit_code,"
        "severity,detail_code,detail_reason FROM operation_events ORDER BY event_id"
    ).fetchall()

    op.execute("DROP TRIGGER trg_operation_events_no_update")
    op.execute("DROP TRIGGER trg_operation_events_no_delete")
    op.execute("DROP INDEX ix_operation_events_operation_id")
    op.execute(f"ALTER TABLE operation_events RENAME TO {_BACKUP_TABLE}")  # nosec B608
    op.execute(_table_sql(closed=target_closed))
    insert_sql = (
        "INSERT INTO operation_events "
        "(event_id,operation_id,event_kind,event_sha256,occurred_at,status,exit_code,"
        "severity,detail_code,detail_reason) VALUES (?,?,?,?,?,?,?,?,?,?)"
    )
    for raw_row in rows:
        row = tuple(raw_row)
        detail_reason = None if row[9] is None else str(row[9])
        event_sha = str(row[3])
        if target_closed and detail_reason is not None:
            detail_reason = _WITHHELD
            event_sha = _event_hash(row, detail_reason)
        bind.exec_driver_sql(
            insert_sql,
            (
                row[0],
                row[1],
                row[2],
                event_sha,
                row[4],
                row[5],
                row[6],
                row[7],
                row[8],
                detail_reason,
            ),
        )
    inserted_count = bind.exec_driver_sql("SELECT COUNT(*) FROM operation_events").scalar_one()
    if inserted_count != len(rows):
        raise RuntimeError("operation_events rebuild count mismatch")
    violations = bind.exec_driver_sql("PRAGMA foreign_key_check(operation_events)").fetchall()
    if violations:
        raise RuntimeError("operation_events rebuild foreign-key check failed")
    op.execute(f"DROP TABLE {_BACKUP_TABLE}")  # nosec B608
    op.execute(_INDEX_SQL)
    op.execute(_UPDATE_TRIGGER_SQL)
    op.execute(_DELETE_TRIGGER_SQL)
    _require_contract(closed=target_closed)


def upgrade() -> None:
    _rebuild(source_closed=False, target_closed=True)


def downgrade() -> None:
    _rebuild(source_closed=True, target_closed=False)
