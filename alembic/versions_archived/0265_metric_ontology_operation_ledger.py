"""Add atomic metric-ontology population operation evidence.

Revision ID: 0265_metric_ontology_operation_ledger
Revises: 0264_document_processing_operation_ledger
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0265_metric_ontology_operation_ledger"
down_revision: str | None = "0264_document_processing_operation_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEDGER_TABLE = "metric_ontology_operation_ledger"


def _acquire_writer_lock(bind: sa.Connection) -> None:
    """Reserve the writer before testing whether immutable evidence is empty."""

    bind.exec_driver_sql(
        "UPDATE metric_ontology_operation_ledger SET operation_id=operation_id WHERE 0"
    )


def upgrade() -> None:
    op.create_table(
        _LEDGER_TABLE,
        sa.Column("operation_id", sa.String(96), primary_key=True),
        sa.Column("idempotency_key", sa.String(96), nullable=False, unique=True),
        sa.Column("database_instance_id", sa.String(64), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("result_sha256", sa.String(64), nullable=False),
        sa.Column("receipt_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("receipt_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["database_instance_id"],
            ["database_runtime_identity.database_instance_id"],
            name="fk_metric_ontology_operation_database_instance",
        ),
        sa.CheckConstraint(
            "length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_metric_ontology_op_request_sha",
        ),
        sa.CheckConstraint(
            "length(result_sha256)=64 AND result_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_metric_ontology_op_result_sha",
        ),
        sa.CheckConstraint(
            "length(receipt_sha256)=64 AND receipt_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_metric_ontology_op_receipt_sha",
        ),
        sa.CheckConstraint(
            "idempotency_key=operation_id "
            "AND length(operation_id)=90 "
            "AND substr(operation_id,1,26)='metric-ontology-operation:' "
            "AND substr(operation_id,27) NOT GLOB '*[^0-9a-f]*'",
            name="ck_metric_ontology_op_identity_shape",
        ),
        sa.CheckConstraint(
            "json_valid(receipt_json) "
            "AND json_type(receipt_json,'$.schema_version')='text' "
            "AND json_type(receipt_json,'$.operation_id')='text' "
            "AND json_type(receipt_json,'$.database_instance_id')='text' "
            "AND json_type(receipt_json,'$.request_sha256')='text' "
            "AND json_type(receipt_json,'$.result_sha256')='text' "
            "AND json_type(receipt_json,'$.receipt_sha256')='text' "
            "AND json_extract(receipt_json,'$.schema_version') IS "
            "'metric-ontology-operation-receipt/v1' "
            "AND json_extract(receipt_json,'$.operation_id') IS operation_id "
            "AND json_extract(receipt_json,'$.database_instance_id') IS database_instance_id "
            "AND json_extract(receipt_json,'$.request_sha256') IS request_sha256 "
            "AND json_extract(receipt_json,'$.result_sha256') IS result_sha256 "
            "AND json_extract(receipt_json,'$.receipt_sha256') IS receipt_sha256",
            name="ck_metric_ontology_op_receipt_shape",
        ),
    )
    op.execute(
        "CREATE TRIGGER trg_metric_ontology_operation_immutable_update "
        f"BEFORE UPDATE ON {_LEDGER_TABLE} BEGIN "
        "SELECT RAISE(ABORT,'metric ontology operation ledger is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_metric_ontology_operation_immutable_delete "
        f"BEFORE DELETE ON {_LEDGER_TABLE} BEGIN "
        "SELECT RAISE(ABORT,'metric ontology operation ledger is immutable'); END"
    )


def downgrade() -> None:
    bind = op.get_bind()
    _acquire_writer_lock(bind)
    if (
        bind.execute(sa.text("SELECT 1 FROM metric_ontology_operation_ledger LIMIT 1")).first()
        is not None
    ):
        raise RuntimeError("0265 downgrade is forward-only after ontology evidence exists")
    op.execute("DROP TRIGGER IF EXISTS trg_metric_ontology_operation_immutable_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_metric_ontology_operation_immutable_update")
    op.drop_table(_LEDGER_TABLE)
