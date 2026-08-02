"""Add the self-FK lookup index required for bounded fact retention.

Revision ID: 0270_financial_facts_supersedes_index
Revises: 0269_latest_governed_population_receipt_v2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

revision: str = "0270_financial_facts_supersedes_index"
down_revision: str | None = "0269_latest_governed_population_receipt_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "financial_facts"
_COLUMN = "supersedes_id"
_INDEX = "ix_0270_financial_facts_supersedes_id"


def _has_self_fk(bind: Connection) -> bool:
    groups: dict[int, list[Sequence[object]]] = {}
    for row in bind.exec_driver_sql(f"PRAGMA foreign_key_list('{_TABLE}')").fetchall():
        groups.setdefault(int(row[0]), []).append(row)
    candidates = [rows for rows in groups.values() if any(str(row[3]) == _COLUMN for row in rows)]
    if len(candidates) != 1 or len(candidates[0]) != 1:
        return False
    row = candidates[0][0]
    return (
        int(row[1]) == 0
        and str(row[2]) == _TABLE
        and str(row[3]) == _COLUMN
        and str(row[4]) == "id"
        and str(row[5]).upper() == "NO ACTION"
        and str(row[6]).upper() == "NO ACTION"
        and str(row[7]).upper() == "NONE"
    )


def _source_contract_is_exact(bind: Connection) -> bool:
    table = bind.execute(
        sa.text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:table_name"),
        {"table_name": _TABLE},
    ).first()
    if table is None:
        return False

    columns = {
        str(row[1]) for row in bind.exec_driver_sql(f"PRAGMA table_info('{_TABLE}')").fetchall()
    }
    if _COLUMN not in columns:
        return False
    return _has_self_fk(bind)


def _existing_index_owner(bind: Connection) -> object | None:
    return bind.execute(
        sa.text("SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=:index_name"),
        {"index_name": _INDEX},
    ).first()


def upgrade() -> None:
    bind = op.get_bind()
    existing = _existing_index_owner(bind)
    if existing is not None:
        raise RuntimeError("0270 migration-owned index name already exists; refusing to adopt it")
    # This repository intentionally upgrades many partial synthetic schemas
    # to head. An absent legacy fact contract is therefore not itself proof of
    # production corruption. Skip DDL for that partial schema; db_gc's apply
    # preflight remains the authoritative, fail-closed operational admission.
    if not _source_contract_is_exact(bind):
        return
    op.create_index(_INDEX, _TABLE, [_COLUMN], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    owner = _existing_index_owner(bind)
    if owner is None:
        if _source_contract_is_exact(bind):
            raise RuntimeError("0270 migration-owned index is missing during downgrade")
        return

    attributes = next(
        (
            row
            for row in bind.exec_driver_sql(f"PRAGMA index_list('{_TABLE}')").fetchall()
            if str(row[1]) == _INDEX
        ),
        None,
    )
    columns = tuple(
        str(row[2]) for row in bind.exec_driver_sql(f"PRAGMA index_info('{_INDEX}')").fetchall()
    )
    if (
        str(owner[0]) != _TABLE
        or attributes is None
        or int(attributes[2]) != 0
        or int(attributes[4]) != 0
        or columns != (_COLUMN,)
        or not _has_self_fk(bind)
    ):
        raise RuntimeError(
            "0270 migration-owned index definition drifted; refusing destructive downgrade"
        )
    op.drop_index(_INDEX, table_name=_TABLE)
