"""Add the append-only operations journal and correlation indexes.

Revision ID: 0011_add_operations_journal
Revises: 0010_add_rehearsal_io_indexes
Create Date: 2026-08-13
"""

from __future__ import annotations

from alembic import op

revision = "0011_add_operations_journal"
down_revision = "0010_add_rehearsal_io_indexes"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    rows = op.get_bind().exec_driver_sql(f"PRAGMA table_info({table})").fetchall()  # nosec B608 -- internal fixed table names
    return {str(row[1]) for row in rows}


def _normalize_sql(value: str) -> str:
    return " ".join(value.replace("IF NOT EXISTS", "").split()).rstrip(";").casefold()


def _require_table_contract(table: str, expected_columns: set[str], expected_sql: str) -> None:
    observed = _columns(table)
    if observed != expected_columns:
        raise RuntimeError(
            f"existing {table} shape does not match migration 0011: "
            f"expected={sorted(expected_columns)!r} observed={sorted(observed)!r}"
        )
    row = (
        op.get_bind()
        .exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
        .fetchone()
    )
    actual_sql = "" if row is None or row[0] is None else str(row[0])
    if _normalize_sql(actual_sql) != _normalize_sql(expected_sql):
        raise RuntimeError(f"existing {table} constraints do not match migration 0011")


def _require_index(table: str, name: str, columns: tuple[str, ...], expected_sql: str) -> None:
    indexes = {
        str(row[1]): (bool(row[2]), bool(row[4]))
        for row in op.get_bind().exec_driver_sql(f"PRAGMA index_list({table})").fetchall()  # nosec B608 -- internal fixed table names
    }
    observed = tuple(
        str(row[2])
        for row in op.get_bind().exec_driver_sql(f"PRAGMA index_info({name})").fetchall()  # nosec B608 -- internal fixed index names
    )
    row = (
        op.get_bind()
        .exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,))
        .fetchone()
    )
    actual_sql = "" if row is None or row[0] is None else str(row[0])
    if (
        indexes.get(name) != (False, False)
        or observed != columns
        or _normalize_sql(actual_sql) != _normalize_sql(expected_sql)
    ):
        raise RuntimeError(f"existing index {name} does not match migration 0011")


def _require_operation_fk(table: str) -> None:
    info = {
        str(row[1]): (str(row[2]).upper(), bool(row[3]), bool(row[5]))
        for row in op.get_bind().exec_driver_sql(f"PRAGMA table_info({table})").fetchall()  # nosec B608 -- internal fixed table names
    }
    matching = [
        row
        for row in op.get_bind().exec_driver_sql(f"PRAGMA foreign_key_list({table})").fetchall()  # nosec B608 -- internal fixed table names
        if str(row[3]) == "operation_id"
    ]
    if info.get("operation_id") != ("TEXT", False, False) or len(matching) != 1:
        raise RuntimeError(f"existing {table}.operation_id does not match migration 0011")
    foreign_key = matching[0]
    contract = tuple(str(foreign_key[index]).upper() for index in (2, 4, 5, 6, 7))
    if contract != ("OPERATION_REQUESTS", "OPERATION_ID", "NO ACTION", "NO ACTION", "NONE"):
        raise RuntimeError(f"existing {table}.operation_id foreign key is invalid")


def _require_trigger(name: str, expected_sql: str) -> None:
    row = (
        op.get_bind()
        .exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,))
        .fetchone()
    )
    actual_sql = "" if row is None or row[0] is None else str(row[0])
    if _normalize_sql(actual_sql) != _normalize_sql(expected_sql):
        raise RuntimeError(f"existing trigger {name} does not match migration 0011")


def upgrade() -> None:
    request_table_sql = """
        CREATE TABLE IF NOT EXISTS operation_requests (
            operation_id TEXT NOT NULL PRIMARY KEY,
            idempotency_key_sha256 TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL,
            actor TEXT NOT NULL,
            job_name TEXT NOT NULL,
            trigger_kind TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            scope_json TEXT NOT NULL,
            command_sha256 TEXT NOT NULL,
            write_sets_json TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            CONSTRAINT ck_operation_request_id CHECK (
                length(operation_id)=74 AND substr(operation_id,1,10)='operation:'
                AND substr(operation_id,11) NOT GLOB '*[^0-9a-f]*'
                AND substr(operation_id,11)=idempotency_key_sha256
            ),
            CONSTRAINT ck_operation_request_hashes CHECK (
                length(idempotency_key_sha256)=64
                AND idempotency_key_sha256 NOT GLOB '*[^0-9a-f]*'
                AND length(request_sha256)=64
                AND request_sha256 NOT GLOB '*[^0-9a-f]*'
                AND length(command_sha256)=64
                AND command_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            CONSTRAINT ck_operation_request_trigger CHECK (
                trigger_kind IN ('manual','scheduled','service')
            ),
            CONSTRAINT ck_operation_request_labels CHECK (
                length(actor) BETWEEN 1 AND 128 AND actor NOT GLOB '*[^A-Za-z0-9_.:-]*'
                AND length(job_name) BETWEEN 1 AND 128
                    AND job_name NOT GLOB '*[^A-Za-z0-9_.:-]*'
                AND length(trace_id) BETWEEN 1 AND 128
                    AND trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
                AND length(stage) BETWEEN 1 AND 128 AND stage NOT GLOB '*[^A-Za-z0-9_.:-]*'
                AND lower(actor || ':' || job_name || ':' || trace_id || ':' || stage)
                    NOT GLOB '*argv*'
                AND lower(actor || ':' || job_name || ':' || trace_id || ':' || stage)
                    NOT GLOB '*env*'
                AND lower(actor || ':' || job_name || ':' || trace_id || ':' || stage)
                    NOT GLOB '*prompt*'
                AND lower(actor || ':' || job_name || ':' || trace_id || ':' || stage)
                    NOT GLOB '*response*'
                AND lower(actor || ':' || job_name || ':' || trace_id || ':' || stage)
                    NOT GLOB '*stdout*'
                AND lower(actor || ':' || job_name || ':' || trace_id || ':' || stage)
                    NOT GLOB '*stderr*'
                AND lower(actor || ':' || job_name || ':' || trace_id || ':' || stage)
                    NOT GLOB '*payload*'
                AND lower(actor || ':' || job_name || ':' || trace_id || ':' || stage)
                    NOT GLOB '*secret*'
                AND lower(actor || ':' || job_name || ':' || trace_id || ':' || stage)
                    NOT GLOB '*token*'
                AND lower(actor || ':' || job_name || ':' || trace_id || ':' || stage)
                    NOT GLOB '*credential*'
                AND lower(actor || ':' || job_name || ':' || trace_id || ':' || stage)
                    NOT GLOB '*apikey*'
                AND lower(actor || ':' || job_name || ':' || trace_id || ':' || stage)
                    NOT GLOB '*api_key*'
            ),
            CONSTRAINT ck_operation_request_scope CHECK (
                length(scope_json) BETWEEN 2 AND 1024
                AND json_valid(scope_json) AND json_type(scope_json)='object'
                AND lower(scope_json) NOT LIKE '%"argv"%'
                AND lower(scope_json) NOT LIKE '%"env"%'
                AND lower(scope_json) NOT LIKE '%"prompt"%'
                AND lower(scope_json) NOT LIKE '%"response"%'
                AND lower(scope_json) NOT LIKE '%"stdout"%'
                AND lower(scope_json) NOT LIKE '%"stderr"%'
                AND lower(scope_json) NOT LIKE '%"url"%'
                AND lower(scope_json) NOT LIKE '%"payload"%'
                AND scope_json NOT LIKE '%://%'
                AND lower(scope_json) NOT LIKE '%secret%'
                AND lower(scope_json) NOT LIKE '%access_token%'
                AND lower(scope_json) NOT LIKE '%apikey%'
                AND lower(scope_json) NOT LIKE '%api_key%'
            ),
            CONSTRAINT ck_operation_request_write_sets CHECK (
                length(write_sets_json) BETWEEN 3 AND 1024
                AND json_valid(write_sets_json) AND json_type(write_sets_json)='array'
            ),
            CONSTRAINT ck_operation_request_clock CHECK (
                length(requested_at) BETWEEN 20 AND 40 AND datetime(requested_at) IS NOT NULL
            )
        )
        """
    op.execute(request_table_sql)
    _require_table_contract(
        "operation_requests",
        {
            "operation_id",
            "idempotency_key_sha256",
            "request_sha256",
            "actor",
            "job_name",
            "trigger_kind",
            "trace_id",
            "stage",
            "scope_json",
            "command_sha256",
            "write_sets_json",
            "requested_at",
        },
        request_table_sql,
    )
    request_index_sql = (
        "CREATE INDEX IF NOT EXISTS ix_operation_requests_requested_at_operation_id "
        "ON operation_requests(requested_at,operation_id)"
    )
    op.execute(request_index_sql)
    request_validation_trigger_sql = """
        CREATE TRIGGER IF NOT EXISTS trg_operation_requests_validate_insert
        BEFORE INSERT ON operation_requests
        BEGIN
            SELECT CASE WHEN EXISTS (
                SELECT 1 FROM json_each(NEW.scope_json)
                WHERE json_each.type NOT IN ('text','integer','true','false','null')
                   OR length(json_each.key) NOT BETWEEN 1 AND 128
                   OR json_each.key GLOB '*[^A-Za-z0-9_.:-]*'
                   OR (json_each.type='text' AND (
                       length(json_each.value) NOT BETWEEN 1 AND 128
                       OR json_each.value GLOB '*[^A-Za-z0-9_.:-]*'
                   ))
            ) THEN RAISE(ABORT, 'operation request unsafe scope') END;
            SELECT CASE WHEN json_array_length(NEW.write_sets_json) < 1 OR EXISTS (
                SELECT 1 FROM json_each(NEW.write_sets_json)
                WHERE json_each.type != 'text'
                   OR length(json_each.value) NOT BETWEEN 1 AND 128
                   OR json_each.value GLOB '*[^A-Za-z0-9_.:-]*'
            ) THEN RAISE(ABORT, 'operation request unsafe write sets') END;
        END
        """
    op.execute(request_validation_trigger_sql)
    _require_trigger("trg_operation_requests_validate_insert", request_validation_trigger_sql)
    event_table_sql = """
        CREATE TABLE IF NOT EXISTS operation_events (
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
                 AND (detail_reason IS NULL OR length(detail_reason) BETWEEN 1 AND 240))
            ),
            CONSTRAINT ck_operation_event_detail_privacy CHECK (
                detail_reason IS NULL OR (
                    detail_reason NOT LIKE '%://%'
                    AND lower(detail_reason) NOT LIKE '%secret%'
                    AND lower(detail_reason) NOT LIKE '%apikey=%'
                    AND lower(detail_reason) NOT LIKE '%api_key=%'
                    AND lower(detail_reason) NOT LIKE '%access_token=%'
                    AND lower(detail_reason) NOT LIKE '%authorization: bearer%'
                )
            ),
            CONSTRAINT ck_operation_event_clock CHECK (
                length(occurred_at) BETWEEN 20 AND 40 AND datetime(occurred_at) IS NOT NULL
            )
        )
        """
    op.execute(event_table_sql)
    _require_table_contract(
        "operation_events",
        {
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
        },
        event_table_sql,
    )
    event_index_sql = (
        "CREATE INDEX IF NOT EXISTS ix_operation_events_operation_id "
        "ON operation_events(operation_id,occurred_at)"
    )
    op.execute(event_index_sql)
    for table in ("operation_requests", "operation_events"):
        update_trigger_sql = (
            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update "  # nosec B608 -- internal fixed table names
            f"BEFORE UPDATE ON {table} BEGIN "
            f"SELECT RAISE(ABORT, '{table} append-only'); END"
        )
        delete_trigger_sql = (
            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete "  # nosec B608 -- internal fixed table names
            f"BEFORE DELETE ON {table} BEGIN "
            f"SELECT RAISE(ABORT, '{table} append-only'); END"
        )
        op.execute(update_trigger_sql)
        op.execute(delete_trigger_sql)
        _require_trigger(f"trg_{table}_no_update", update_trigger_sql)
        _require_trigger(f"trg_{table}_no_delete", delete_trigger_sql)
    # SQLite supports a nullable REFERENCES column directly. Alembic's generic
    # add-column path incorrectly attempts a second unsupported ALTER for it.
    if "operation_id" not in _columns("pipeline_attempts"):
        op.execute(
            "ALTER TABLE pipeline_attempts ADD COLUMN operation_id TEXT "
            "REFERENCES operation_requests(operation_id)"
        )
    pipeline_index_sql = (
        "CREATE INDEX IF NOT EXISTS ix_pipeline_attempts_operation_id "
        "ON pipeline_attempts(operation_id)"
    )
    op.execute(pipeline_index_sql)
    if "operation_id" not in _columns("source_calls"):
        op.execute(
            "ALTER TABLE source_calls ADD COLUMN operation_id TEXT "
            "REFERENCES operation_requests(operation_id)"
        )
    source_index_sql = (
        "CREATE INDEX IF NOT EXISTS ix_source_calls_operation_id ON source_calls(operation_id)"
    )
    op.execute(source_index_sql)
    llm_index_sql = (
        "CREATE INDEX IF NOT EXISTS ix_llm_calls_trace_id_called_at "
        "ON llm_calls(trace_id,called_at)"
    )
    op.execute(llm_index_sql)
    _require_index(
        "operation_requests",
        "ix_operation_requests_requested_at_operation_id",
        ("requested_at", "operation_id"),
        request_index_sql,
    )
    _require_index(
        "operation_events",
        "ix_operation_events_operation_id",
        ("operation_id", "occurred_at"),
        event_index_sql,
    )
    _require_index(
        "pipeline_attempts",
        "ix_pipeline_attempts_operation_id",
        ("operation_id",),
        pipeline_index_sql,
    )
    _require_index(
        "source_calls",
        "ix_source_calls_operation_id",
        ("operation_id",),
        source_index_sql,
    )
    _require_index(
        "llm_calls",
        "ix_llm_calls_trace_id_called_at",
        ("trace_id", "called_at"),
        llm_index_sql,
    )
    _require_operation_fk("pipeline_attempts")
    _require_operation_fk("source_calls")


def downgrade() -> None:
    op.drop_index("ix_llm_calls_trace_id_called_at", table_name="llm_calls")
    op.drop_index("ix_source_calls_operation_id", table_name="source_calls")
    op.execute("ALTER TABLE source_calls DROP COLUMN operation_id")
    op.drop_index("ix_pipeline_attempts_operation_id", table_name="pipeline_attempts")
    op.execute("ALTER TABLE pipeline_attempts DROP COLUMN operation_id")
    op.execute("DROP TRIGGER IF EXISTS trg_operation_events_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_operation_events_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_operation_requests_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_operation_requests_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_operation_requests_validate_insert")
    op.drop_index("ix_operation_events_operation_id", table_name="operation_events")
    op.drop_table("operation_events")
    op.drop_index(
        "ix_operation_requests_requested_at_operation_id", table_name="operation_requests"
    )
    op.drop_table("operation_requests")
