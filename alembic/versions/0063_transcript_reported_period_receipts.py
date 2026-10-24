"""Keep historical transcript receipts and admit source-bound current receipts.

Revision ID: 0063_transcript_reported_period_receipts
Revises: 0062_reviewed_pdf_pages
"""

from __future__ import annotations

from alembic import op

revision = "0063_transcript_reported_period_receipts"
down_revision = "0062_reviewed_pdf_pages"
branch_labels = None
depends_on = None

_OLD_CHECK = """CHECK(json_extract(authorization_json,'$.schema_version') =
                'transcript-acquisition-authorization@1')"""
_NEW_CHECK = """CHECK(
                (json_extract(authorization_json,'$.schema_version') =
                    'transcript-acquisition-authorization@1'
                 AND json_extract(authorization_json,'$.request.schema_version') =
                    'transcript-acquisition-request@1')
                OR
                (json_extract(authorization_json,'$.schema_version') =
                    'transcript-acquisition-authorization@2'
                 AND json_extract(authorization_json,'$.request.schema_version') =
                    'transcript-acquisition-request@2')
            )"""


def _rebuild(*, reverse: bool = False) -> None:
    connection = op.get_bind()
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError("transcript receipt rebuild requires the standard Alembic connection")
    if (
        reverse
        and connection.exec_driver_sql(
            "SELECT 1 FROM transcript_acquisition_receipts "
            "WHERE json_extract(authorization_json,'$.schema_version')="
            "'transcript-acquisition-authorization@2' LIMIT 1"
        ).fetchone()
        is not None
    ):
        raise RuntimeError("cannot downgrade while current transcript receipts exist")
    schema = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type='table' "
        "AND name='transcript_acquisition_receipts'"
    ).scalar_one()
    if not isinstance(schema, str):
        raise RuntimeError("transcript receipt schema is unavailable")
    old, new = (_NEW_CHECK, _OLD_CHECK) if reverse else (_OLD_CHECK, _NEW_CHECK)
    if schema.count(old) != 1:
        raise RuntimeError("transcript receipt schema differs from expected predecessor")
    schema = schema.replace(old, new, 1)
    prefix = "CREATE TABLE transcript_acquisition_receipts"
    quoted_prefix = 'CREATE TABLE "transcript_acquisition_receipts"'
    if schema.startswith(quoted_prefix):
        schema = schema.replace(
            quoted_prefix, "CREATE TABLE transcript_acquisition_receipts_rebuild", 1
        )
    elif schema.startswith(prefix):
        schema = schema.replace(prefix, "CREATE TABLE transcript_acquisition_receipts_rebuild", 1)
    else:
        raise RuntimeError("transcript receipt table declaration differs from predecessor")
    dependents = (
        connection.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE tbl_name='transcript_acquisition_receipts' "
            "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name"
        )
        .scalars()
        .all()
    )
    connection.exec_driver_sql("SAVEPOINT transcript_receipt_schema")
    try:
        connection.exec_driver_sql(schema)
        connection.exec_driver_sql(
            "INSERT INTO transcript_acquisition_receipts_rebuild "
            "SELECT * FROM transcript_acquisition_receipts"
        )
        connection.exec_driver_sql("DROP TABLE transcript_acquisition_receipts")
        connection.exec_driver_sql(
            "ALTER TABLE transcript_acquisition_receipts_rebuild "
            "RENAME TO transcript_acquisition_receipts"
        )
        for statement in dependents:
            connection.exec_driver_sql(str(statement))
        if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("transcript receipt rebuild failed foreign-key verification")
        connection.exec_driver_sql("RELEASE SAVEPOINT transcript_receipt_schema")
    except Exception:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT transcript_receipt_schema")
        connection.exec_driver_sql("RELEASE SAVEPOINT transcript_receipt_schema")
        raise


def upgrade() -> None:
    _rebuild()


def downgrade() -> None:
    _rebuild(reverse=True)
