"""Admit third-party aggregator providers on transcript acquisition receipts.

Revision ID: 0071_admit_aggregator_transcript_providers
Revises: 0070_source_coverage_document_version_index
"""

from __future__ import annotations

from alembic import op

revision = "0071_admit_aggregator_transcript_providers"
down_revision = "0070_source_coverage_document_version_index"
branch_labels = None
depends_on = None

_OLD_CHECK = "CHECK(provider = 'issuer_ir')"
_NEW_CHECK = "CHECK(provider IN ('issuer_ir', 'roic', 'stockanalysis', 'tickertrends'))"


def _rebuild(*, reverse: bool = False) -> None:
    connection = op.get_bind()
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError("transcript receipt rebuild requires the standard Alembic connection")
    if (
        reverse
        and connection.exec_driver_sql(
            "SELECT 1 FROM transcript_acquisition_receipts WHERE provider != 'issuer_ir' LIMIT 1"
        ).fetchone()
        is not None
    ):
        raise RuntimeError("cannot downgrade while aggregator transcript receipts exist")
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
    connection.exec_driver_sql("SAVEPOINT transcript_receipt_provider_schema")
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
        connection.exec_driver_sql("RELEASE SAVEPOINT transcript_receipt_provider_schema")
    except Exception:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT transcript_receipt_provider_schema")
        connection.exec_driver_sql("RELEASE SAVEPOINT transcript_receipt_provider_schema")
        raise


def upgrade() -> None:
    _rebuild()


def downgrade() -> None:
    _rebuild(reverse=True)
