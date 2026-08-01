"""Add immutable latest-governed population receipt v2 evidence.

Revision ID: 0269_latest_governed_population_receipt_v2
Revises: 0268_latest_governed_population_operation_ledger
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0269_latest_governed_population_receipt_v2"
down_revision: str | None = "0268_latest_governed_population_operation_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_V2_TABLE = "latest_governed_population_operation_ledger_v2"


def _create_v2_immutable_triggers() -> None:
    op.execute(
        "CREATE TRIGGER trg_latest_governed_population_operation_ledger_v2_immutable_update "
        "BEFORE UPDATE ON latest_governed_population_operation_ledger_v2 BEGIN "
        "SELECT RAISE(ABORT,'latest governed population ledger is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_latest_governed_population_operation_ledger_v2_immutable_delete "
        "BEFORE DELETE ON latest_governed_population_operation_ledger_v2 BEGIN "
        "SELECT RAISE(ABORT,'latest governed population ledger is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_latest_governed_population_operation_ledger_v2_identity_immutable "
        "BEFORE INSERT ON latest_governed_population_operation_ledger_v2 "
        "WHEN EXISTS (SELECT 1 FROM latest_governed_population_operation_ledger_v2 "
        "existing WHERE "
        "existing.operation_id=NEW.operation_id "
        "OR existing.idempotency_key=NEW.idempotency_key "
        "OR existing.receipt_sha256=NEW.receipt_sha256) BEGIN "
        "SELECT RAISE(ABORT,'latest governed population ledger is immutable'); END"
    )


def _create_v2_table() -> None:
    op.create_table(
        _V2_TABLE,
        sa.Column("operation_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("database_instance_id", sa.String(64), nullable=False),
        sa.Column("eligibility_artifact_sha256", sa.String(64), nullable=False),
        sa.Column("registry_artifact_sha256", sa.String(64), nullable=False),
        sa.Column("admission_sha256", sa.String(64), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("result_sha256", sa.String(64), nullable=False),
        sa.Column("receipt_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("receipt_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["database_instance_id"],
            ["database_runtime_identity.database_instance_id"],
            name="fk_latest_population_v2_database_instance",
        ),
        sa.CheckConstraint(
            "idempotency_key=operation_id "
            "AND length(operation_id)=101 "
            "AND substr(operation_id,1,37)='latest-governed-population-operation:' "
            "AND substr(operation_id,38) NOT GLOB '*[^0-9a-f]*'",
            name="ck_latest_population_v2_operation_identity",
        ),
        *(
            sa.CheckConstraint(
                f"length({column})=64 AND {column} NOT GLOB '*[^0-9a-f]*'",
                name=f"ck_latest_population_v2_{column.removesuffix('_sha256')}_sha",
            )
            for column in (
                "eligibility_artifact_sha256",
                "registry_artifact_sha256",
                "admission_sha256",
                "request_sha256",
                "result_sha256",
                "receipt_sha256",
            )
        ),
        sa.CheckConstraint(
            "json_valid(receipt_json) "
            "AND json_extract(receipt_json,'$.schema_version') IS "
            "'latest-governed-population-receipt/v2' "
            "AND json_extract(receipt_json,'$.operation_id') IS operation_id "
            "AND json_extract(receipt_json,'$.database_instance_id') IS database_instance_id "
            "AND json_extract(receipt_json,'$.eligibility_artifact_sha256') IS "
            "eligibility_artifact_sha256 "
            "AND json_extract(receipt_json,'$.registry_artifact_sha256') IS "
            "registry_artifact_sha256 "
            "AND json_extract(receipt_json,'$.admission_sha256') IS admission_sha256 "
            "AND json_extract(receipt_json,'$.request_sha256') IS request_sha256 "
            "AND json_extract(receipt_json,'$.result_sha256') IS result_sha256 "
            "AND json_extract(receipt_json,'$.receipt_sha256') IS receipt_sha256",
            name="ck_latest_population_v2_receipt_shape",
        ),
    )


def upgrade() -> None:
    _create_v2_table()
    _create_v2_immutable_triggers()
    op.execute(
        "CREATE TRIGGER trg_latest_governed_population_operation_ledger_identity_immutable "
        "BEFORE INSERT ON latest_governed_population_operation_ledger "
        "WHEN EXISTS (SELECT 1 FROM latest_governed_population_operation_ledger "
        "existing WHERE "
        "existing.operation_id=NEW.operation_id "
        "OR existing.idempotency_key=NEW.idempotency_key "
        "OR existing.receipt_sha256=NEW.receipt_sha256) BEGIN "
        "SELECT RAISE(ABORT,'latest governed population ledger is immutable'); END"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        "UPDATE latest_governed_population_operation_ledger_v2 "
        "SET operation_id=operation_id WHERE 0"
    )
    if (
        bind.execute(
            sa.text(
                "SELECT 1 FROM latest_governed_population_operation_ledger "
                "UNION ALL SELECT 1 FROM latest_governed_population_operation_ledger_v2 LIMIT 1"
            )
        ).first()
        is not None
    ):
        raise RuntimeError("0269 downgrade is forward-only after population evidence exists")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_latest_governed_population_operation_ledger_identity_immutable"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS "
        "trg_latest_governed_population_operation_ledger_v2_identity_immutable"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_latest_governed_population_operation_ledger_v2_immutable_delete"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_latest_governed_population_operation_ledger_v2_immutable_update"
    )
    op.drop_table(_V2_TABLE)
