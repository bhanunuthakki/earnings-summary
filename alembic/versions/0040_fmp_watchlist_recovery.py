"""Allow automatic evaluation and watchlist recovery with truthful request identity.

Revision ID: 0040_fmp_watchlist_recovery
Revises: 0039_add_dcf_forecast_series
"""

from __future__ import annotations

from alembic import op

revision = "0040_fmp_watchlist_recovery"
down_revision = "0039_add_dcf_forecast_series"
branch_labels = None
depends_on = None

_OLD_ROLES = "CHECK(coverage_role IN ('portfolio','evaluation','index_member'))"
_NEW_ROLES = "CHECK(coverage_role IN ('portfolio','evaluation','watchlist','index_member'))"
_OLD_PRIORITY = "CHECK(priority IN (100,200,300))"
_NEW_PRIORITY = "CHECK(priority IN (100,150,200,300))"
_OLD_REQUEST = """CHECK(
                (coverage_role = 'evaluation' AND requested = 1
                    AND owner_request_id IS NOT NULL)
                OR coverage_role != 'evaluation'
            )"""
_NEW_REQUEST = (
    "CHECK(requested = 0 OR (owner_request_id IS NOT NULL AND length(trim(owner_request_id)) > 0))"
)


def _rebuild(*, reverse: bool = False) -> None:
    connection = op.get_bind()
    # The standard Alembic environment creates its own SQLite connection with
    # foreign keys disabled. Do not toggle this inside an existing transaction.
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError("FMP backlog rebuild requires the standard Alembic connection")
    schema = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='fmp_work_backlog'"
    ).scalar_one()
    if not isinstance(schema, str):
        raise RuntimeError("FMP backlog schema is unavailable")
    replacements = (
        (_OLD_ROLES, _NEW_ROLES),
        (_OLD_PRIORITY, _NEW_PRIORITY),
        (_OLD_REQUEST, _NEW_REQUEST),
    )
    for old, new in replacements:
        before, after = (new, old) if reverse else (old, new)
        if schema.count(before) != 1:
            raise RuntimeError("FMP backlog constraint differs from the expected predecessor")
        schema = schema.replace(before, after)
    prefix = "CREATE TABLE fmp_work_backlog"
    quoted_prefix = 'CREATE TABLE "fmp_work_backlog"'
    if schema.startswith(quoted_prefix):
        schema = schema.replace(quoted_prefix, "CREATE TABLE fmp_work_backlog_rebuild", 1)
    elif schema.startswith(prefix):
        schema = schema.replace(prefix, "CREATE TABLE fmp_work_backlog_rebuild", 1)
    else:
        raise RuntimeError("FMP backlog table declaration differs from the predecessor")
    dependents = (
        connection.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE tbl_name='fmp_work_backlog' "
            "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name"
        )
        .scalars()
        .all()
    )
    connection.exec_driver_sql("SAVEPOINT fmp_watchlist_schema")
    try:
        connection.exec_driver_sql(schema)
        connection.exec_driver_sql(
            "INSERT INTO fmp_work_backlog_rebuild SELECT * FROM fmp_work_backlog"
        )
        connection.exec_driver_sql("DROP TABLE fmp_work_backlog")
        connection.exec_driver_sql(
            "ALTER TABLE fmp_work_backlog_rebuild RENAME TO fmp_work_backlog"
        )
        for statement in dependents:
            connection.exec_driver_sql(str(statement))
        if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("FMP backlog rebuild failed foreign-key verification")
        connection.exec_driver_sql("RELEASE SAVEPOINT fmp_watchlist_schema")
    except Exception:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT fmp_watchlist_schema")
        connection.exec_driver_sql("RELEASE SAVEPOINT fmp_watchlist_schema")
        raise


def upgrade() -> None:
    _rebuild()


def downgrade() -> None:
    # Copying into the old constraints rejects retained watchlist work; the
    # savepoint restores the complete current schema and rows on that failure.
    _rebuild(reverse=True)
